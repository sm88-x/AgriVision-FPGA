from __future__ import annotations
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import argparse
import csv
import hashlib
import importlib.metadata as metadata
import json
import math
import platform
import sys
import time
import threading
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import scipy
import skimage
from scipy import ndimage as ndi
from skimage.feature import peak_local_max
from skimage.morphology import h_maxima
import shutil
from datetime import datetime
from skimage.segmentation import watershed

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT))

SPECIES = ("soybean", "mungbean", "redbean")
MODES = ("baseline_0", "baseline_1", "proposed_v1")
MODE_NAMES = ("yolo", "fixed", "adaptive")

# Selected on the development split only. All three modes share the same
# detector profile for a given input species; species is supplied by the caller.
SPECIES_DETECTOR_PROFILES = {
    'soybean': {'imgsz': 960, 'iou': 0.55},
    'mungbean': {'imgsz': 960, 'iou': 0.70},
    'redbean': {'imgsz': 960, 'iou': 0.55},
}

PLAN = [
    ("soybean", "sparse", 6, 4),
    ("soybean", "pair", 6, 4),
    ("soybean", "multi", 6, 4),
    ("mungbean", "sparse", 3, 2),
    ("mungbean", "touch", 8, 7),
    ("redbean", "sparse", 3, 2),
    ("redbean", "touch", 8, 7),
]

FIELDS = [
    "frame_id", "image_id", "image_path", "species", "gt_count",
    "contact_type", "lighting", "capture_notes", "scene_id",
    "split", "checked_by", "reviewed_by", "prior_ok",
]

# Tune on DEV only. Any change requires a new frozen version.
CFG = {
    "min_area": 25,
    "component_overlap": 0.25,
    "fixed_distance": 5,
    "fixed_threshold": 0.18,
    "suspect_area_ratio": 1.55,
    "concavity_threshold": 0.10,
    "max_instances": 120,
    "adaptive_prominence_factors": [0.02, 0.05, 0.10],
    "adaptive_min_spacing_factor": 0.32,
    # 固定工作区域，归一化坐标：[左、上、右、下]
    # 当前相机画面底部有支架，先将其排除。
    "work_roi": [0.04, 0.04, 0.96, 0.88],

    # 平滑背景拟合
    "bg_sample_step": 6,
    "bg_fit_iterations": 5,
    "bg_inlier_fraction": 0.75,

    # 降低亮度差的权重，减少阴影影响
    "lightness_weight": 0.25,
    "min_color_difference": 8.0,

    # 明显异常时停止，避免错误掩膜进入先验
    "max_foreground_fraction": 0.35,
}

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def env_info():
    result = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "opencv": cv2.__version__,
        "scipy": scipy.__version__,
        "scikit_image": skimage.__version__,
    }
    for name in ("torch", "ultralytics"):
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = None
    return result

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

def write_csv(path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def read_image(path):
    image = cv2.imdecode(
        np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR
    )
    if image is None:
        raise ValueError(f"Cannot read image: {path}")
    return image

def save_png(path, image):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"Cannot encode PNG: {path}")
    data.tofile(str(path))

def get_path(row):
    return (ROOT / row["image_path"]).resolve()

def manifest_path():
    return ROOT / "data/datasets/seeds/dataset_manifest.csv"

def init_dataset():
    target = manifest_path()
    if target.exists():
        raise FileExistsError(target)

    rows = []
    for species, contact, ndev, ntest in PLAN:
        for i in range(ndev + ntest):
            iid = f"{species}_{contact}_{i + 1:03d}"
            rows.append({
                "frame_id": len(rows) + 1,
                "image_id": iid,
                "image_path": f"data/datasets/seeds/raw/{iid}.png",
                "species": species,
                "gt_count": "",
                "contact_type": contact,
                "lighting": "",
                "capture_notes": "",
                "scene_id": iid,
                "split": "dev" if i < ndev else "test",
                "checked_by": "",
                "reviewed_by": "",
                "prior_ok": "no",
            })

    write_csv(target, rows, FIELDS)
    (ROOT / "data/datasets/seeds/raw").mkdir(parents=True, exist_ok=True)
    print("Created:", target)
    print("70 images planned: dev=40, test=30")

def capture(camera, width, height):
    cap = cv2.VideoCapture(camera)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if not cap.isOpened():
        raise RuntimeError("Cannot open camera")

    try:
        for row in read_csv(manifest_path()):
            target = get_path(row)
            if target.exists():
                continue

            print(row["image_id"], row["contact_type"], row["split"])
            print("Change seed layout. SPACE saves; Q quits.")

            while True:
                ok, image = cap.read()
                if not ok:
                    raise RuntimeError("Camera read failed")
                if image.shape[:2] != (height, width):
                    raise ValueError(
                        f"Camera returned {image.shape[1]}x{image.shape[0]}, "
                        f"requested {width}x{height}"
                    )

                preview = image.copy()
                cv2.putText(
                    preview, f"{row['image_id']} SPACE=save Q=quit",
                    (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (0, 0, 255), 1,
                )
                cv2.imshow("segmentation capture", preview)
                key = cv2.waitKey(20) & 0xFF
                if key in (ord("q"), 27):
                    return
                if key == 32:
                    save_png(target, image)
                    break
    finally:
        cap.release()
        cv2.destroyAllWindows()

def validate():
    rows = read_csv(manifest_path())
    required = set(FIELDS) - {'capture_notes'}
    missing = required-set(rows[0] if rows else [])
    if missing:
        raise ValueError('CSV missing columns: '+', '.join(sorted(missing)))
    if len(rows) != 70:
        raise ValueError("Expected 70 manifest rows")

    expected = Counter({
        (s, c): nd + nt for s, c, nd, nt in PLAN
    })
    actual = Counter((r["species"], r["contact_type"]) for r in rows)
    if actual != expected:
        raise ValueError(f"Wrong species/contact counts: {actual}")

    if Counter(r["split"] for r in rows) != Counter(dev=40, test=30):
        raise ValueError("Expected dev=40, test=30")

    ids, fids, hashes, sizes, scenes = set(), set(), set(), set(), {}
    for r in rows:
        iid = r["image_id"]
        if not iid or iid in ids:
            raise ValueError("Missing or duplicate image_id")
        ids.add(iid)

        fid = int(r["frame_id"])
        if fid < 0 or fid in fids:
            raise ValueError("Invalid or duplicate frame_id")
        fids.add(fid)

        if int(r["gt_count"]) <= 0:
            raise ValueError(f"Invalid GT: {iid}")

        for key in ("lighting", "scene_id", "checked_by"):
            if not r[key].strip():
                raise ValueError(f"{iid}: missing {key}")

        if r["contact_type"] != "sparse":
            if (
                not r["reviewed_by"].strip()
                or r["reviewed_by"] == r["checked_by"]
            ):
                raise ValueError(f"{iid}: needs a second human reviewer")

        previous = scenes.setdefault(r["scene_id"], r["split"])
        if previous != r["split"]:
            raise ValueError("Same layout appears in both dev and test")

        if r["prior_ok"] not in ("yes", "no"):
            raise ValueError("prior_ok must be yes or no")
        if r["prior_ok"] == "yes":
            if r["split"] != "dev" or r["contact_type"] != "sparse":
                raise ValueError("Priors must use dev sparse images")

        path = get_path(r)
        digest = sha256(path)
        if digest in hashes:
            raise ValueError(f"Duplicate image content: {path}")
        hashes.add(digest)
        sizes.add(read_image(path).shape[:2])

    if len(sizes) != 1:
        raise ValueError("Use one resolution for pixel-area priors")
    return rows

def foreground(image):
    """
    固定工作区域内：
    平滑背景拟合 -> 光照校正后的颜色差 -> 阈值分割。

    返回与原图同尺寸的 bool 掩膜，保留原图坐标。
    参数只能在开发集上调整。
    """
    if (
        image is None
        or image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
    ):
        raise ValueError("foreground 需要 uint8 BGR 彩色图")

    h, w = image.shape[:2]
    left, top, right, bottom = CFG["work_roi"]

    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("work_roi 配置不合法")

    x1 = int(round(left * w))
    y1 = int(round(top * h))
    x2 = int(round(right * w))
    y2 = int(round(bottom * h))

    roi = image[y1:y2, x1:x2]
    rh, rw = roi.shape[:2]
    if min(rh, rw) < 20:
        raise ValueError("工作区域过小")

    smooth = cv2.GaussianBlur(roi, (3, 3), 0)
    lab = cv2.cvtColor(smooth, cv2.COLOR_BGR2LAB).astype(np.float32)

    # 二次平滑曲面：
    # 1、x、y、x²、xy、y²。
    # 分别拟合 Lab 三个通道的背景变化。
    yy, xx = np.mgrid[
        -1:1:complex(rh),
        -1:1:complex(rw)
    ]
    design = np.stack(
        [
            np.ones_like(xx),
            xx,
            yy,
            xx * xx,
            xx * yy,
            yy * yy,
        ],
        axis=-1,
    )

    step = int(CFG["bg_sample_step"])
    if step < 1:
        raise ValueError("bg_sample_step 必须大于0")

    sample_x = design[::step, ::step].reshape(-1, 6)
    sample_lab = lab[::step, ::step].reshape(-1, 3)

    inliers = np.ones(len(sample_x), dtype=bool)
    lightness_weight = float(CFG["lightness_weight"])
    fraction = float(CFG["bg_inlier_fraction"])

    if not 0 < fraction < 1:
        raise ValueError("bg_inlier_fraction 必须在0和1之间")

    # 迭代减少种子像素对背景模型的影响。
    for _ in range(int(CFG["bg_fit_iterations"])):
        if np.count_nonzero(inliers) < 12:
            raise ValueError("可用于背景拟合的像素不足")

        coefficients = np.linalg.lstsq(
            sample_x[inliers],
            sample_lab[inliers],
            rcond=None,
        )[0]

        residual = sample_lab - sample_x @ coefficients
        error = np.sqrt(
            lightness_weight * residual[:, 0] ** 2
            + residual[:, 1] ** 2
            + residual[:, 2] ** 2
        )

        cutoff = np.quantile(error, fraction)
        inliers = error <= cutoff

    background = design @ coefficients
    residual = lab - background

    score = np.sqrt(
        lightness_weight * residual[:, :, 0] ** 2
        + residual[:, :, 1] ** 2
        + residual[:, :, 2] ** 2
    )
    score = np.clip(score, 0, 255).astype(np.uint8)

    otsu_threshold, _ = cv2.threshold(
        score,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    threshold = max(
        float(otsu_threshold),
        float(CFG["min_color_difference"]),
    )

    local_mask = (score > threshold).astype(np.uint8)

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (3, 3)
    )
    local_mask = cv2.morphologyEx(
        local_mask, cv2.MORPH_OPEN, kernel
    )

    # 先去小噪点，再填种子内部小孔。
    labels, _ = ndi.label(local_mask)
    areas = np.bincount(labels.ravel())

    keep = areas >= int(CFG["min_area"])
    keep[0] = False
    local_mask = ndi.binary_fill_holes(keep[labels])

    if float(local_mask.mean()) > CFG["max_foreground_fraction"]:
        raise ValueError(
            "前景占比异常：请检查工作区域、背景和光照，"
            "不要使用当前掩膜生成先验"
        )

    # 还原成原图尺寸，不改变坐标。
    full_mask = np.zeros((h, w), dtype=bool)
    full_mask[y1:y2, x1:x2] = local_mask
    return full_mask

def geometry(mask):
    contours, _ = cv2.findContours(
        mask.astype(np.uint8),
        cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )
    if not contours:
        raise ValueError("Empty mask")

    contour = max(contours, key=cv2.contourArea)
    (_, _), (a, b), _ = cv2.minAreaRect(contour)
    area = float(cv2.contourArea(contour))
    perimeter = float(cv2.arcLength(contour, True))
    hull_area = float(cv2.contourArea(cv2.convexHull(contour)))

    return {
        "mask_area_px": int(mask.sum()),
        "area_px": area,
        "length_px": float(max(a, b)),
        "width_px": float(min(a, b)),
        "perimeter_px": perimeter,
        "aspect_ratio": float(max(a, b) / max(min(a, b), 1.0)),
        "circularity": float(
            4 * math.pi * area / max(perimeter * perimeter, 1.0)
        ),
        "concavity": float(max(0, 1 - area / max(hull_area, 1.0))),
        "contour": contour,
    }

def border_contact(mask):
    return bool(
        mask[0].any() or mask[-1].any()
        or mask[:, 0].any() or mask[:, -1].any()
    )

def inspect_dev():
    try:
        for row in read_csv(manifest_path()):
            if row["split"] != "dev":
                continue

            image = read_image(get_path(row))
            mask = foreground(image)
            labels, n = ndi.label(mask)
            preview = image.copy()
            h, w = image.shape[:2]
            left, top, right, bottom = CFG["work_roi"]

            cv2.rectangle(
                preview,
                (int(round(left * w)), int(round(top * h))),
                (int(round(right * w)), int(round(bottom * h))),
                (255, 0, 0),
                1,
            )

            for k in range(1, n + 1):
                contour = geometry(labels == k)["contour"]
                cv2.drawContours(preview, [contour], -1, (0, 0, 255), 2)
                x, y, _, _ = cv2.boundingRect(contour)
                cv2.putText(
                    preview, str(k), (x, max(15, y)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1,
                )

            cv2.putText(
                preview, f"{row['image_id']} components={n}",
                (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (0, 0, 255), 1,
            )
            binary = cv2.cvtColor(
                mask.astype(np.uint8) * 255, cv2.COLOR_GRAY2BGR
            )
            cv2.imshow(
                "DEV masks: any key next; Q quit",
                np.hstack([preview, binary]),
            )
            if cv2.waitKey(0) & 0xFF in (ord("q"), 27):
                break
    finally:
        cv2.destroyAllWindows()

def build_priors(rows):
    samples = {s: [] for s in SPECIES}

    for r in rows:
        if r["split"] != "dev" or r["prior_ok"] != "yes":
            continue

        mask = foreground(read_image(get_path(r)))
        labels, count = ndi.label(mask)
        if count != int(r["gt_count"]):
            raise ValueError(
                f"{r['image_id']}: mask components={count}, "
                f"GT={r['gt_count']}; review prior_ok"
            )

        for k in range(1, count + 1):
            component = labels == k
            g = geometry(component)
            if border_contact(component) or g["width_px"] < 3:
                raise ValueError(f"Invalid prior instance: {r['image_id']}")
            samples[r["species"]].append(g)

    priors = {}
    for species, values in samples.items():
        if len(values) < 10:
            raise ValueError(f"{species}: need >=10 approved single seeds")

        areas = np.array([v["mask_area_px"] for v in values], float)
        aspects = np.array([v["aspect_ratio"] for v in values], float)
        median = float(np.median(areas))
        priors[species] = {
            "n": len(values),
            "area_median": median,
            "area_mad": float(np.median(np.abs(areas - median))),
            "aspect_median": float(np.median(aspects)),
            "aspect_q05": float(np.quantile(aspects, 0.05)),
            "aspect_q95": float(np.quantile(aspects, 0.95)),
            "area_q01": float(np.quantile(areas, 0.01)),
            "area_q99": float(np.quantile(areas, 0.99)),
            "concavity_q95": float(np.quantile([v['concavity'] for v in values], .95)),
            "circularity_q05": float(np.quantile([v['circularity'] for v in values], .05)),
        }
    return priors

def _peak_points(mask, spacing, prominence, minimum_height):
    distance = ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    distance = ndi.gaussian_filter(distance, 0.6) * mask
    maxima = h_maxima(distance, max(0.05, float(prominence)))
    components, n = ndi.label(maxima & mask)
    points = []
    for k in range(1, n + 1):
        coords = np.argwhere(components == k)
        values = distance[tuple(coords.T)]
        center = coords.mean(axis=0)
        ties = coords[values >= values.max() - 1e-6]
        point = ties[np.argmin(np.sum((ties - center) ** 2, axis=1))]
        if distance[tuple(point)] >= minimum_height:
            points.append(tuple(map(int, point)))
    points.sort(key=lambda p: (-distance[p], p))
    selected = []
    for p in points:
        if all(np.linalg.norm(np.array(p)-np.array(q)) >= spacing for q in selected):
            selected.append(p)
    return selected, distance

def _partition(mask, points):
    if not points or len(points) > CFG['max_instances']:
        return []
    markers = np.zeros(mask.shape, np.int32)
    for index, point in enumerate(points, 1):
        markers[point] = index
    distance = ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    labels = watershed(-distance, markers, mask=mask)
    return [labels == k for k in range(1, len(points)+1)]

def _detector_points(mask, sources, offset, spacing):
    coords = np.argwhere(mask)
    points = []
    for det in sorted(sources, key=lambda d: -d['confidence']):
        x1, y1, x2, y2 = det['bbox_xyxy']
        point = np.array([(y1+y2)/2-offset[1], (x1+x2)/2-offset[0]])
        if point[0] < -2 or point[1] < -2 or point[0] > mask.shape[0]+1 or point[1] > mask.shape[1]+1:
            continue
        nearest = coords[np.argmin(np.sum((coords-point)**2, axis=1))]
        if np.linalg.norm(nearest-point) > max(3, min(x2-x1, y2-y1)*0.6):
            continue
        p = tuple(map(int, nearest))
        if all(np.linalg.norm(nearest-np.array(q)) >= spacing for q in points):
            points.append(p)
    return points

def watershed_parts(mask, threshold, spacing):
    # Fixed pixel spacing/prominence. No species prior is used here.
    points, _ = _peak_points(mask, spacing, 0.8, max(1.0, threshold*spacing))
    return _partition(mask, points), len(points)

def chromatic_responses(image, prior, species):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    # Green seed surface is green in a*, red is red in a*, yellow is yellow in b*.
    feature = -lab[:,:,1] if species == 'mungbean' else lab[:,:,2] if species == 'soybean' else lab[:,:,1]
    minor = math.sqrt(4*prior['area_median']/(math.pi*max(prior['aspect_median'],1)))
    return [-ndi.gaussian_laplace(feature, max(1,minor*scale))*max(1,minor*scale)**2 for scale in (.22,.30)]

def adaptive_parts(mask, prior, sources=None, offset=(0, 0), responses=None):
    sources = sources or []
    g = geometry(mask)
    area0 = prior['area_median']
    # Calibrated large, round single seeds must not be mistaken for touching pairs.
    if (g['mask_area_px'] <= prior.get('area_q99',area0*1.8)*1.08
        and g['concavity'] <= max(.06,prior.get('concavity_q95',.04)*1.2)
        and g['aspect_ratio'] <= prior['aspect_q95']*1.15
        and g['circularity'] >= max(.45,prior.get('circularity_q05',.6)*.85)):
        return [mask], 'Calibrated single-seed size, convexity and circularity'
    # Mild contour concavity alone is insufficient to split an ordinary single seed.
    if g['mask_area_px'] <= 1.45*area0 and g['aspect_ratio'] <= 1.5*prior['aspect_q95']:
        return [mask], 'Accepted by single-seed area/aspect geometry'
    minor = math.sqrt(4*area0/(math.pi*max(prior['aspect_median'], 1)))
    spacing = max(2.0, CFG['adaptive_min_spacing_factor']*minor)
    anchors = _detector_points(mask, sources, offset, spacing)
    initial, _ = _peak_points(mask, spacing, minor*0.14, max(1, minor*0.18))
    suspicious = (g['mask_area_px'] > 1.55*area0 or
                  (g['concavity'] > .10 and len(initial) > 1) or
                  g['aspect_ratio'] > 1.5*prior['aspect_q95'])
    if not suspicious:
        return [mask], 'Accepted by single-seed geometry'
    candidates = []
    proposals = []
    for factor in CFG['adaptive_prominence_factors']:
        peaks, _ = _peak_points(mask, spacing, minor*factor, max(1, minor*.18))
        combined = list(anchors)
        for p in peaks:
            if all(np.linalg.norm(np.array(p)-np.array(q)) >= spacing for q in combined):
                combined.append(p)
        proposals.extend([('distance', peaks, factor), ('detector+distance', combined, factor)])
    # Also retain raw distance maxima: smoothing can erase shallow seed valleys.
    raw_distance = ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    for threshold in (0.28, 0.38, 0.50, 0.62):
        for factor in (0.35, 0.50):
            coords = peak_local_max(raw_distance, min_distance=max(2, round(factor*minor)),
                                    threshold_rel=threshold, exclude_border=False,
                                    labels=mask.astype(np.uint8))
            proposals.append(('raw-distance', [tuple(p) for p in coords], threshold))
    for index,response in enumerate(responses or []):
        positive = response[mask]
        if not len(positive) or positive.max() <= .1:
            continue
        coords = peak_local_max(response, min_distance=max(2,round(minor*.40)),
                                threshold_abs=max(.3,float(positive.max())*.10),
                                labels=mask.astype(np.uint8),exclude_border=False)
        points = [tuple(p) for p in coords]
        proposals.append(('surface-chroma',points,index))
        combined = list(anchors)
        for p in points:
            if all(np.linalg.norm(np.array(p)-np.array(q)) >= spacing for q in combined):
                combined.append(p)
        proposals.append(('detector+chroma',combined,index))
    for label, points, factor in proposals:
            if len(points) < 2:
                continue
            parts = _partition(mask, points)
            # Merge tiny watershed fragments by removing their unsupported markers.
            if parts:
                retained = [p for p, part in zip(points, parts) if part.sum() >= .28*area0]
                if len(retained) != len(points) and len(retained) >= 2:
                    parts = _partition(mask, retained)
                    points = retained
            if len(parts) < 2:
                continue
            features = [geometry(p) for p in parts]
            ratios = np.array([p['mask_area_px']/area0 for p in features])
            if ratios.max() > 2.5 or ratios.min() < .25:
                continue
            aspects = np.array([p['aspect_ratio'] for p in features])
            if aspects.max() > max(3.2, 2.0*prior['aspect_q95']):
                continue
            # All evidence is image-derived. GT is never read here.
            area_score = np.mean(np.log(ratios)**2)
            shape_score = np.mean(np.maximum(aspects/prior['aspect_q95']-1, 0)**2)
            support_score = 0.0
            if anchors:
                support_score = np.mean([min(np.linalg.norm(np.array(a)-np.array(p)) for p in points)/minor for a in anchors])
            score = float(area_score + .15*shape_score + .20*support_score)
            candidates.append((score, parts, factor, label))
    if not candidates:
        return [], 'UNRESOLVED: no candidate passes child geometry/area checks'
    score, parts, factor, label = min(candidates, key=lambda c: c[0])
    if score > 1.0:
        return [], f'UNRESOLVED: candidate score too high ({score:.3f})'
    return parts, f'Adaptive {label}; prominence={factor}; score={score:.3f}; candidates={len(candidates)}'

def groups_from_boxes(labels, detections):
    """Connected groups in the box-to-foreground-component graph."""
    h, w = labels.shape
    areas = np.bincount(labels.ravel())
    links = []

    for det in detections:
        box = det["bbox_xyxy"]
        x1 = int(np.clip(math.floor(box[0]), 0, w))
        y1 = int(np.clip(math.floor(box[1]), 0, h))
        x2 = int(np.clip(math.ceil(box[2]), 0, w))
        y2 = int(np.clip(math.ceil(box[3]), 0, h))
        ids, counts = np.unique(labels[y1:y2, x1:x2], return_counts=True)
        box_area = max((x2 - x1) * (y2 - y1), 1)

        links.append({
            int(k) for k, count in zip(ids, counts)
            if k > 0 and count >= CFG["min_area"]
            and count / max(1, min(areas[k], box_area))
            >= CFG["component_overlap"]
        })

    parent = list(range(len(detections)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner = {}
    for i, components in enumerate(links):
        for component in components:
            if component in owner:
                parent[find(i)] = find(owner[component])
            else:
                owner[component] = i

    groups = {}
    for i, det in enumerate(detections):
        group = groups.setdefault(
            find(i), {"detections": [], "components": set()}
        )
        group["detections"].append(det)
        group["components"].update(links[i])
    return list(groups.values())

def box_object(det, status, reason):
    return {
        "bbox_xyxy_src": list(det["bbox_xyxy"]),
        "contour_xy_src": [],
        "confidence": det["confidence"],
        "source_detection_ids": [det["id"]],
        "status": status,
        "status_reason": reason,
        "mask_area_px": None,
        "area_px": None,
        "length_px": None,
        "width_px": None,
        "perimeter_px": None,
        "aspect_ratio": None,
        "circularity": None,
        "concavity": None,
    }

def mask_object(mask, offset, sources, reason):
    g = geometry(mask)
    contour = g.pop("contour").reshape(-1, 2).astype(np.int32)
    contour += np.array(offset, np.int32)
    x, y, w, h = cv2.boundingRect(contour.reshape(-1, 1, 2))

    return {
        **g,
        "bbox_xyxy_src": [x, y, x + w, y + h],
        "contour_xy_src": contour.tolist(),
        "confidence": max(d["confidence"] for d in sources),
        "source_detection_ids": [d["id"] for d in sources],
        "status": "OK",
        "status_reason": reason,
    }

class SeedSeparator:
    def __init__(self, frozen, weights=None):
        from ai.inference.infer_seed import SeedDetector

        self.frozen = frozen
        self._predict_lock = threading.Lock()
        weights = Path(weights or ROOT / "data/models/best.pt")
        if sha256(weights) != frozen["weights_sha256"]:
            raise ValueError("Weight hash differs from frozen version")

        opts = frozen["detector"]
        self.detector = SeedDetector(
            weights=weights,
            conf=opts["conf"],
            imgsz=opts["imgsz"],
            device=opts["device"],
            iou=opts["iou"],
        )

        names = list(self.detector.model.names.values())
        if len(names) != 1 or str(names[0]).lower() != "seed":
            raise ValueError(f"Expected single-class seed model: {names}")

        h, w = frozen["image_hw"]
        self.predict(np.full((h, w, 3), 255, np.uint8), frame_id=-1)

    @staticmethod
    def in_work_roi(det, shape):
        h, w = shape[:2]
        left, top, right, bottom = CFG['work_roi']
        x1, y1, x2, y2 = det['bbox_xyxy']
        cx, cy = (x1+x2)/2, (y1+y2)/2
        overlap = max(0, min(x2,right*w)-max(x1,left*w))*max(0,min(y2,bottom*h)-max(y1,top*h))
        return left*w <= cx < right*w and top*h <= cy < bottom*h and overlap/max((x2-x1)*(y2-y1),1) >= .5

    def predict(self, image, frame_id=None, species=None):
        with self._predict_lock:
            profile = dict(self.frozen['detector'])
            if species is not None:
                if species not in SPECIES:
                    raise ValueError('Unknown species')
                profile.update(self.frozen.get('species_detector_profiles',{}).get(species,{}))
            self.detector.imgsz = profile['imgsz']
            self.detector.iou = profile['iou']
            self.detector.conf = profile['conf']
            prediction = self._predict_locked(image, frame_id)
            prediction['species'] = species
            prediction['effective_parameters']['imgsz'] = profile['imgsz']
            return prediction

    def _predict_locked(self, image, frame_id=None):
        start = time.perf_counter()
        detections = self.detector.detect_seeds(image, frame_id=frame_id)

        # Read model-reported inference timing when available.
        infer_ms = None
        try:
            speed = self.detector.model.predictor.results[0].speed
            value = speed.get("inference")
            if value is not None:
                infer_ms = float(value)
        except (AttributeError, IndexError, TypeError):
            pass

        return {
            "detections": [d for d in detections if self.in_work_roi(d, image.shape)],
            "raw_detections": detections,
            "effective_parameters": {
                "iou": float(self.detector.model.predictor.args.iou),
                "conf": float(self.detector.model.predictor.args.conf),
            "device": str(self.detector.model.predictor.device),
            },
            "yolo_infer_ms": infer_ms,
            "detector_wall_ms": (time.perf_counter() - start) * 1000,
        }

    def detect_and_separate(
        self, image, mode, species, *,
        session_id="offline", frame_id=None, image_id=None,
        gt_count=None, prediction=None
    ):
        if mode not in MODES or species not in SPECIES:
            raise ValueError("Invalid mode or species")
        if (
            image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != 3
        ):
            raise ValueError("Expected uint8 HxWx3 BGR image")
        if list(image.shape[:2]) != self.frozen["image_hw"]:
            raise ValueError("Image size differs from frozen acquisition")

        if prediction is None:
            prediction = self.predict(image, frame_id, species)
        if prediction.get('species') not in (None, species):
            raise ValueError('Prediction species does not match frame species')
        detections = prediction["detections"]

        start = time.perf_counter()
        objects = []
        diagnostics = []

        if mode == "baseline_0":
            # Preserve the current detector's boxes and count.
            objects = [
                box_object(d, "OK", "Original YOLO detection")
                for d in detections
            ]
        else:
            mask = foreground(image)
            labels, _ = ndi.label(mask)
            slices = ndi.find_objects(labels)
            h, w = mask.shape
            responses = chromatic_responses(image,self.frozen['priors'][species],species) if mode == 'proposed_v1' else []

            for group in groups_from_boxes(labels, detections):
                sources = group["detections"]
                pending = []
                failed_reason = None

                if not group["components"]:
                    failed_reason = "No usable foreground in YOLO ROI"

                for component in sorted(group["components"]):
                    ys, xs = slices[component - 1]
                    local = labels[ys, xs] == component

                    cut = (
                        (ys.start == 0 and local[0].any())
                        or (ys.stop == h and local[-1].any())
                        or (xs.start == 0 and local[:, 0].any())
                        or (xs.stop == w and local[:, -1].any())
                    )
                    if cut:
                        failed_reason = "Component cut by image boundary"
                        break

                    if mode == "baseline_1":
                        parts, npeaks = watershed_parts(
                            local,
                            CFG["fixed_threshold"],
                            CFG["fixed_distance"],
                        )
                        g = geometry(local)
                        if not parts or (
                            npeaks == 1
                            and g["concavity"] > CFG["concavity_threshold"]
                        ):
                            failed_reason = "Fixed watershed unresolved"
                            break
                        reason = f"Fixed watershed; peaks={npeaks}"
                    else:
                        parts, reason = adaptive_parts(
                            local, self.frozen["priors"][species], sources, (xs.start, ys.start),
                            [r[ys,xs] for r in responses]
                        )
                        if not parts:
                            failed_reason = reason
                            break

                    for part in parts:
                        pending.append(mask_object(
                            part, (xs.start, ys.start), sources, reason
                        ))

                    diagnostics.append({
                        'component_id': int(component),
                        'source_detection_ids': [d['id'] for d in sources],
                        'mask_area_px': int(local.sum()),
                        'output_instances': len(parts),
                        'reason': reason,
                    })

                if not failed_reason and mode == 'baseline_1' and sources:
                    typical = np.median([max(2, min(d['bbox_xyxy'][2]-d['bbox_xyxy'][0], d['bbox_xyxy'][3]-d['bbox_xyxy'][1])) for d in sources])
                    centers = []
                    for d in sorted(sources, key=lambda d: -d['confidence']):
                        x1,y1,x2,y2 = d['bbox_xyxy']; p = np.array([(x1+x2)/2,(y1+y2)/2])
                        if all(np.linalg.norm(p-q) >= typical*.5 for q in centers):
                            centers.append(p)
                    if len(pending) < .5*len(centers):
                        failed_reason = 'UNRESOLVED: fixed split discards most distinct detector centers'

                if failed_reason:
                    diagnostics.append({
                        'source_detection_ids': [d['id'] for d in sources],
                        'outcome': 'original_detections_retained',
                        'reason': failed_reason,
                    })
                    # Keep original detections for the entire unresolved group.
                    objects.extend(
                        box_object(d, "UNRESOLVED", failed_reason)
                        for d in sources
                    )
                else:
                    objects.extend(pending)

        objects.sort(key=lambda o: (
            o["bbox_xyxy_src"][1], o["bbox_xyxy_src"][0]
        ))
        for index, obj in enumerate(objects, 1):
            obj["id"] = index
            obj["seed_id"] = f"Seed #{index:03d}"

        unresolved = any(o["status"] == "UNRESOLVED" for o in objects)
        result = {
            "schema": "agrivision.segmentation.separation.v5",
            "evaluation_scope": "same working ROI for all three methods",
            "yolo_raw_count": len(prediction.get("raw_detections", detections)),
            "effective_detector_parameters": prediction.get("effective_parameters"),
            "session_id": session_id,
            "frame_id": frame_id,
            "image_id": image_id,
            "mode": mode,
            "species": species,
            "image_size": [image.shape[1], image.shape[0]],
            "coordinate_system": "original_image_pixels",
            "input_format": "BGR_uint8",
            "gt_count": gt_count,
            "yolo_count": len(detections),
            "final_count": len(objects),
            "absolute_error": (
                abs(len(objects) - gt_count)
                if gt_count is not None else None
            ),
            "status": (
                "UNRESOLVED" if unresolved
                else "NO_DETECTIONS" if not detections else "OK"
            ),
            "detections": detections,
            "raw_detections": prediction.get('raw_detections',detections) if mode == 'baseline_0' else None,
            "objects": objects,
            "diagnostics": diagnostics,
        }
        post_ms = (time.perf_counter() - start) * 1000
        result["timings_ms"] = {
            "yolo_infer_ms": prediction["yolo_infer_ms"],
            "detector_wall_ms": prediction["detector_wall_ms"],
            "separation_ms": post_ms,
            "total_ms": prediction["detector_wall_ms"] + post_ms,
            "fpga_dma_ms": None,
        }
        return result

def archive_existing(path, overwrite):
    path = Path(path).resolve()
    if not path.exists():
        return
    if not overwrite:
        raise FileExistsError(f'{path} exists; use --overwrite to archive and replace')
    allowed = (ROOT / 'data/evidence/segmentation_comparison').resolve()
    if path == allowed or not path.is_relative_to(allowed) or path.is_symlink():
        raise ValueError('Overwrite is restricted to a child of results/segmentation')
    target = allowed / '_history' / (path.name+'_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    target.parent.mkdir(parents=True, exist_ok=True)
    path.rename(target)
    print('Archived old output:', target)

def freeze(args):
    rows = validate()
    weights = args.weights.resolve()
    env = env_info()
    if not env["torch"] or not env["ultralytics"]:
        raise RuntimeError("YOLO dependencies are missing")

    frozen = {
        "source_sha256": sha256(Path(__file__)),
        "adapter_sha256": sha256(ROOT / "src/software/ai/inference/infer_seed.py"),
        "weights_sha256": sha256(weights),
        "manifest_sha256": sha256(manifest_path()),
        "environment": env,
        "image_hw": list(read_image(get_path(rows[0])).shape[:2]),
        "image_sha256": {
            r["image_id"]: sha256(get_path(r)) for r in rows
        },
        "algorithm_version": "segmentation-v3-calibrated-shape-and-surface-markers",
        "species_detector_profiles": SPECIES_DETECTOR_PROFILES,
        "detector": {
            "conf": args.conf,
            "iou": args.iou,
            "imgsz": args.imgsz,
            "device": args.device,
        },
        "config": CFG,
        "priors": build_priors(rows),
    }
    archive_existing(args.out, args.overwrite)
    write_json(args.out, frozen)
    print(json.dumps(frozen["priors"], ensure_ascii=False, indent=2))
    print("Frozen:", args.out)

def verify_frozen(path, weights):
    frozen = json.loads(path.read_text(encoding="utf-8"))
    checks = [
        (Path(__file__), frozen["source_sha256"]),
        (ROOT / "src/software/ai/inference/infer_seed.py", frozen["adapter_sha256"]),
        (weights, frozen["weights_sha256"]),
        (manifest_path(), frozen["manifest_sha256"]),
    ]
    for file, expected in checks:
        if sha256(file) != expected:
            raise ValueError(f"Frozen file changed: {file}")

    if CFG != frozen["config"] or env_info() != frozen["environment"]:
        raise ValueError("Configuration or environment changed after freeze")

    rows = validate()
    for r in rows:
        if sha256(get_path(r)) != frozen["image_sha256"][r["image_id"]]:
            raise ValueError(f"Frozen image changed: {r['image_id']}")
    return frozen, rows

def render_panel(image, result):
    canvas = image.copy()
    h, w = canvas.shape[:2]
    left, top, right, bottom = CFG['work_roi']
    cv2.rectangle(canvas, (round(left*w), round(top*h)), (round(right*w), round(bottom*h)), (255,0,0), 1)
    for obj in result["objects"]:
        i = obj["id"]
        color = (
            (0, 165, 255) if obj["status"] == "UNRESOLVED"
            else (60 + i * 53 % 180, 60 + i * 97 % 180, 60 + i * 137 % 180)
        )
        x1, y1, x2, y2 = [int(round(v)) for v in obj["bbox_xyxy_src"]]
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)

        if obj["contour_xy_src"]:
            cv2.polylines(
                canvas,
                [np.array(obj["contour_xy_src"], np.int32).reshape(-1, 1, 2)],
                True, color, 2,
            )
        cv2.putText(
            canvas, str(i), (x1, max(15, y1 - 4)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1,
        )

    width = 640
    height = round(canvas.shape[0] * width / canvas.shape[1])
    canvas = cv2.resize(canvas, (width, height))
    canvas = cv2.copyMakeBorder(
        canvas, 72, 0, 0, 0, cv2.BORDER_CONSTANT,
        value=(255, 255, 255),
    )
    lines = [
        f"{result['mode']} GT={result['gt_count']} "
        f"N={result['final_count']} AE={result['absolute_error']}",
        f"{result['status']} "
        f"post={result['timings_ms']['separation_ms']:.2f} ms",
    ]
    for i, text in enumerate(lines):
        cv2.putText(
            canvas, text, (8, 24 + 26 * i),
            cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 0), 1,
        )
    return canvas

def evaluate(args):
    frozen, rows = verify_frozen(args.frozen, args.weights)
    rows = [r for r in rows if r["split"] == args.split]
    engine = SeedSeparator(frozen, args.weights)
    archive_existing(args.out, args.overwrite)
    args.out.mkdir(parents=True)
    comparison, failures, all_results = [], [], []

    for row in rows:
        iid = row["image_id"]
        image = read_image(get_path(row))
        fid = int(row["frame_id"])
        prediction = engine.predict(image, fid, row['species'])
        # Reproducibility data lives in the baseline JSON; no duplicate cache.

        results = [
            engine.detect_and_separate(
                image, mode, row["species"],
                session_id=f"segmentation_{args.split}",
                frame_id=fid,
                image_id=iid,
                gt_count=int(row["gt_count"]),
                prediction=prediction,
            )
            for mode in MODES
        ]

        record = {
            "image_id": iid,
            "frame_id": fid,
            "species": row["species"],
            "split": row["split"],
            "contact_type": row["contact_type"],
            "gt_count": int(row["gt_count"]),
            "yolo_infer_ms": prediction["yolo_infer_ms"],
            "detector_wall_ms": prediction["detector_wall_ms"],
        }
        for name, result in zip(MODE_NAMES, results):
            result["source_image"] = row["image_path"]
            result["split"] = args.split
            write_json(
                args.out / "json" / f"{iid}_{result['mode']}.json",
                result,
            )
            all_results.append(result)
            record[f"{name}_count"] = result["final_count"]
            record[f"{name}_abs_error"] = result["absolute_error"]
            record[f"{name}_post_ms"] = result["timings_ms"]["separation_ms"]
            record[f"{name}_total_ms"] = result["timings_ms"]["total_ms"]
            record[f"{name}_status"] = result["status"]

        failed = any(
            r["absolute_error"] != 0 or r["status"] != "OK"
            for r in results
        )
        record['count_error_modes'] = '|'.join(name for name,r in zip(MODE_NAMES,results) if r['absolute_error'] != 0)
        record['review_modes'] = '|'.join(name for name,r in zip(MODE_NAMES,results) if r['status'] != 'OK')
        record["status"] = "FAILURE" if failed else "OK"
        comparison.append(record)

        triptych = np.hstack([render_panel(image, r) for r in results])
        save_png(args.out / "triptych" / f"{iid}.png", triptych)
        if failed:
            save_png(args.out / "failures" / f"{iid}.png", triptych)
            failures.append({
                **record,
                "manual_failure_type": "",
                "manual_notes": "",
            })
        print(iid, "GT=", row["gt_count"],
              "counts=", [r["final_count"] for r in results])

    summary = []
    for species in (*SPECIES, "ALL"):
        for mode in MODES:
            subset = [
                r for r in all_results
                if r["mode"] == mode
                and (species == "ALL" or r["species"] == species)
            ]
            if not subset:
                continue
            summary.append({
                "species": species,
                "split": args.split,
                "mode": mode,
                "n_images": len(subset),
                "count_mae": float(np.mean([
                    r["absolute_error"] for r in subset
                ])),
                "exact_image_accuracy": float(np.mean([
                    r["absolute_error"] == 0 for r in subset
                ])),
                "unresolved_image_rate": float(np.mean([
                    r["status"] == "UNRESOLVED" for r in subset
                ])),
                "mean_post_ms": float(np.mean([
                    r["timings_ms"]["separation_ms"] for r in subset
                ])),
                "mean_total_ms": float(np.mean([
                    r["timings_ms"]["total_ms"] for r in subset
                ])),
            })

    write_csv(args.out / "comparison.csv", comparison)
    write_csv(args.out / "summary.csv", summary)
    write_csv(
        args.out / "failures/index.csv", failures,
        list(comparison[0]) + ["manual_failure_type", "manual_notes"],
    )
    write_json(args.out / "run.json", {
        "frozen_sha256": sha256(args.frozen),
        "split": args.split,
        "evaluation_protocol": "repeated_evaluation_not_new_independent_test" if args.split == "test" else "development",
        "n_images": len(rows),
        "environment": env_info(),
        "timing_scope": (
            "Warm model. Detector call + per-mode postprocessing. "
            "Excludes model loading, image I/O, drawing and file saving."
        ),
    })
    print("Completed:", args.out)
    print("Review-union images (any mode incorrect or unresolved):", len(failures))
    for name in MODE_NAMES:
        print(name, 'count-error images=', sum(r[name+'_abs_error'] != 0 for r in comparison),
              'review images=', sum(r[name+'_status'] != 'OK' for r in comparison))

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("check")
    sub.add_parser("inspect")

    cap = sub.add_parser("capture")
    cap.add_argument("--camera", type=int, default=0)
    cap.add_argument("--width", type=int, default=640)
    cap.add_argument("--height", type=int, default=480)

    fr = sub.add_parser("freeze")
    fr.add_argument("--weights", type=Path, default=ROOT / "data/models/best.pt")
    fr.add_argument("--out", type=Path, required=True)
    fr.add_argument("--conf", type=float, default=0.25)
    fr.add_argument("--iou", type=float, default=0.45)
    fr.add_argument("--imgsz", type=int, default=640)
    fr.add_argument("--device", default="cpu")
    fr.add_argument("--overwrite", action="store_true")

    ev = sub.add_parser("eval")
    ev.add_argument("--weights", type=Path, default=ROOT / "data/models/best.pt")
    ev.add_argument("--frozen", type=Path, required=True)
    ev.add_argument("--split", choices=["dev", "test"], required=True)
    ev.add_argument("--out", type=Path, required=True)
    ev.add_argument("--overwrite", action="store_true")

    args = parser.parse_args()
    if args.command == "init":
        init_dataset()
    elif args.command == "capture":
        capture(args.camera, args.width, args.height)
    elif args.command == "check":
        validate()
        print("PASS: 70 images, dev=40, test=30")
    elif args.command == "inspect":
        inspect_dev()
    elif args.command == "freeze":
        freeze(args)
    elif args.command == "eval":
        evaluate(args)

if __name__ == "__main__":
    main()
