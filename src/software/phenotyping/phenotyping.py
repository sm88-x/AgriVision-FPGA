#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phenotyping B2/B3/B5：单粒种子表型测量与结构化输出。

职责划分（任务书 B3）：**YOLO 负责"目标在哪里"，本模块负责"目标多大、什么形状"。**

六项指标与定义（口径固定，写进报告，避免各人各算）
    ROI        ：YOLO bbox 经 roi_mapping.bbox_to_src() 反映射回原图坐标后，
                 在**板端 PL 二值结果**上裁出的区域（不是拿彩色原图算形状）。
    主区域      ：PL(V4 边缘+形态学) 的二值结果里，种子本体是"外边缘围起来的一整块"，
                 内部还有纹理造成的暗斑（孔洞）——所以按**外轮廓**取区域、
                 按**填充孔洞后的像素数**复算面积，内部暗斑不影响面积/长宽。
                 若 ROI 里有 ≥2 个区域、且次区域 ≥ 主区域 30% → 记为 MERGED（粘连）。
    area_px    ：cv2.contourArea（像素个数）
    perimeter_px：cv2.arcLength(closed=True)
    length_px / width_px：cv2.minAreaRect 的 (w, h) 取长边为 Length、短边为 Width
                 （用旋转外接矩形，避免水平 bbox 受种子朝向影响）
    aspect_ratio：Length / Width（恒 ≥ 1）
    circularity ：4πA / P²（P = 0 时返回 0.0）
    单位        ：**未做毫米标定，一律 px 与 px²**，不许写成 mm / mm²。

status 字段（B3 异常处理）：
    OK               ：ROI 里一块完整区域，正常测出来
    MERGED           ：ROI 里出现 ≥2 个显著区域（次区域 ≥ 主区域 30%）→ 疑似两粒粘连 / 被纹理劈开
    BROKEN_CONTOUR   ：目标被画面边界截断 / 轮廓只剩碎片（主区域 < min_area）/
                       轮廓破碎（圆形度 < 0.10 —— 边界呈锯齿状，周长是等面积光滑轮廓的 2 倍以上）
    INVALID          ：ROI 里一个前景轮廓都没有
    每个目标另带 `status_reason`，用一句话写清为什么不是 OK，便于 Qt 详情卡和报告直接引用。
    另附 border_contact_ratio（轮廓贴 ROI 边界的比例）作为"bbox 偏紧"的人工核查提示，不参与 status 判定。

用法（用 B 的模型 + 板端 PL 二值结果跑一遍并出交付物）：
    python phenotyping.py --frames-dir <raw_bgr 目录> --pl-dir <pl_gray 目录> \
        [--model-root <含 src/software/ai/inference/infer_seed.py 与 data/models/best.pt 的目录，默认仓库根>] \
        --out-json data/evidence/phenotyping/objects.json \
        --out-csv data/evidence/phenotyping/measurements.csv \
        --manual-csv data/evidence/phenotyping/manual_check_30seeds.csv \
        --sheet-out data/evidence/phenotyping/manual_check_30seeds_sheet.png \
        --annotated-dir phenotyping/annotated \
        --failure-dir phenotyping/failure_cases
"""
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
import importlib.util
import json
from pathlib import Path

import cv2
import numpy as np

import roi_mapping as rm

REPO_ROOT = PROJECT_ROOT
DEFAULT_MIN_AREA = 30          # 小于这个像素数当噪点
MERGED_RATIO = 0.30            # 次轮廓 ≥ 主轮廓 30% → 疑似粘连
CLOSE_CONTACT_RATIO = 0.30     # 轮廓 ≥30% 的点贴着 ROI 边界 → 提示"bbox 可能偏紧/截断"（只提示，不改 status）
AREA_DIFF_ALERT = 8.0          # 两种面积算法差 >8% → 提示人工核对
MIN_CIRCULARITY = 0.10         # 圆形度 < 0.10 → 判"轮廓破碎"（光滑种子在 0.4~0.8，细长的也在 0.6 以上）
SATURATION_RATIO = 0.90        # ROI 内白色占比 ≥ 0.90 → 板端二值结果糊成一块，形状不可用

def preprocess_mask(binary):
    """ROI 掩膜的固定预处理：5×5 椭圆核 开 + 闭。

    板端 PL 输出（边缘+形态学）会带种皮纹理造成的毛刺和小暗斑，
    开运算去掉毛刺、闭运算补上小暗斑，让外轮廓更接近种子真实边界（不改变整体尺度）。
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel)

# ---------------------------------------------------------------- 测量
def extract_regions(binary):
    """把 ROI 里的「种子区域」逐个提出来，按像素数从大到小。

    板端 PL（V4: RGB2Gray→Gaussian→Edge→Morphology）的二值结果里，种子是"外边缘围起来的一整块"，
    内部还会因为种皮纹理出现暗斑（孔洞）。所以按**外轮廓**（RETR_EXTERNAL）取区域，
    像素数按**填充孔洞后**统计（孔洞不影响面积），这样 texture 斑点不会把一粒种子劈成两块。
    返回 (regions, mode)，regions = [(contour, 填充后像素个数), ...]。
    """
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    regions = []
    for contour in contours:
        filled = np.zeros_like(binary)
        cv2.drawContours(filled, [contour], -1, 255, cv2.FILLED)
        regions.append((contour, int(cv2.countNonZero(filled))))
    regions.sort(key=lambda item: -item[1])
    return regions, "external"

def measure_roi(mask_roi, bbox_in_roi, min_area=DEFAULT_MIN_AREA, border_px=2):
    """在 ROI 二值图上测量一粒种子。mask_roi 为 0/255 或 0/1 二值图。"""
    if mask_roi is None or mask_roi.size == 0:
        return None
    binary = preprocess_mask(np.where(mask_roi > 0, 255, 0).astype(np.uint8))
    regions, mode = extract_regions(binary)
    if not regions:
        return {"area_px": 0.0, "perimeter_px": 0.0, "length_px": 0.0, "width_px": 0.0,
                "aspect_ratio": 0.0, "circularity": 0.0, "status": "INVALID",
                "status_reason": "ROI 内一个前景轮廓都没有",
                "area_px_cross": 0.0, "area_diff_pct": 0.0,
                "border_contact_ratio": 0.0,
                "region_count": 0, "contour_selection": "ROI 内没有可用轮廓"}
    contour, region_pixels = regions[0]
    area = float(cv2.contourArea(contour))
    # 轮廓贴着 ROI 边界的比例：高说明 bbox 偏紧/可能切到种子 —— 只作为人工核查提示，不直接改 status
    h, w = binary.shape[:2]
    xs, ys = contour[:, 0, 0], contour[:, 0, 1]
    on_border = np.count_nonzero((xs <= border_px) | (ys <= border_px)
                                 | (xs >= w - 1 - border_px) | (ys >= h - 1 - border_px))
    border_contact = float(on_border) / max(1, len(xs))
    big = [px for _, px in regions
           if px >= MERGED_RATIO * region_pixels and px >= min_area]
    perimeter = float(cv2.arcLength(contour, True))
    (_, _), (rw, rh), _ = cv2.minAreaRect(contour)
    length, width = max(rw, rh), min(rw, rh)
    aspect = float(length / width) if width > 0 else 0.0
    circularity = float(4.0 * np.pi * area / (perimeter ** 2)) if perimeter > 0 else 0.0
    circularity = min(circularity, 1.0)
    # 交叉核对：同一个区域按"数像素"再算一遍面积（与 cv2.contourArea 的边界积分相互独立）
    area_cross = float(region_pixels)
    area_diff = abs(area - area_cross) / area * 100.0 if area > 0 else 0.0
    status, reason = "OK", ""
    if region_pixels < min_area:
        status, reason = "BROKEN_CONTOUR", "主区域小于 %d px²，轮廓只剩碎片" % min_area
    elif len(big) >= 2:
        status, reason = "MERGED", "ROI 内 %d 块显著区域（次块 ≥ 主块 %.0f%%），疑似粘连" % (len(big), MERGED_RATIO * 100)
    elif circularity < MIN_CIRCULARITY:
        # 圆形度低到 0.10 以下：周长是等面积光滑轮廓的 2 倍以上，说明板端二值图这块是锯齿/破碎的
        status, reason = "BROKEN_CONTOUR", "轮廓破碎（圆形度 %.3f < %.2f，边界呈锯齿状）" % (circularity, MIN_CIRCULARITY)
    return {
        "area_px": round(float(area), 2),
        "area_px_cross": round(area_cross, 2),
        "area_diff_pct": round(area_diff, 2),
        "perimeter_px": round(perimeter, 2),
        "length_px": round(float(length), 2),
        "width_px": round(float(width), 2),
        "aspect_ratio": round(aspect, 3),
        "circularity": round(circularity, 3),
        "status": status,
        "status_reason": reason,
        "region_count": len(regions),
        "region_mode": mode,
        "border_contact_ratio": round(border_contact, 3),
        "contour_selection": "ROI 掩膜(PL 二值结果)开+闭后取外轮廓；主区域=最大的一块；≥2 块且次块≥主块%.0f%% 记为 MERGED"
                             % (MERGED_RATIO * 100,),
    }

def analyze_frame(image_bgr, pl_binary, detections, frame_id, margin=2,
                  min_area=DEFAULT_MIN_AREA):
    """一帧：YOLO bbox（原图坐标）→ ROI → 在 PL 二值图上测量。"""
    params = rm.letterbox_params(image_bgr.shape[1], image_bgr.shape[0],
                                 pl_binary.shape[1], pl_binary.shape[0])
    img_h, img_w = image_bgr.shape[:2]
    objects, annotated = [], image_bgr.copy()
    for det in rm.assign_ids(detections):
        bbox_pl = det["bbox_xyxy"]                     # 模型输出 = letterbox 坐标
        bbox_src = rm.bbox_to_src(bbox_pl, params)     # ← B1：反算回原图坐标
        roi, (x0, y0) = rm.crop_roi(pl_binary, bbox_src, margin=margin)
        metrics = measure_roi(roi, None, min_area=min_area) or {}
        # bbox 顶到图像边界 → 这粒种子被画面切掉了，别当成正常目标
        truncated = bool(bbox_src[0] <= 1.0 or bbox_src[1] <= 1.0
                         or bbox_src[2] >= img_w - 1.0 or bbox_src[3] >= img_h - 1.0)
        # bbox 范围内板端结果几乎全白 → 二值结果在这个目标上糊成一整块，
        # 轮廓其实就是 bbox 矩形，测出来的面积/长宽等于框的尺寸（目标越小，板端形态学膨胀占比越大，越容易这样）
        bx1, by1 = max(0, int(round(bbox_src[0]))), max(0, int(round(bbox_src[1])))
        bx2, by2 = min(img_w, int(round(bbox_src[2]))), min(img_h, int(round(bbox_src[3])))
        bbox_region = pl_binary[by1:by2, bx1:bx2]
        bbox_white = (float(np.count_nonzero(bbox_region)) / float(bbox_region.size)
                      if bbox_region.size else 0.0)
        metrics["bbox_white_ratio"] = round(bbox_white, 3)
        if bbox_white >= SATURATION_RATIO and metrics.get("status") in ("OK", "MERGED"):
            metrics["status"] = "BROKEN_CONTOUR"
            metrics["status_reason"] = ("板端二值结果在 bbox 内饱和（白色占比 %.0f%%），"
                                       "轮廓糊成一块，面积/长宽等于 bbox 尺寸" % (bbox_white * 100))
        if truncated and metrics.get("status") in ("OK", "MERGED"):
            metrics["status"] = "BROKEN_CONTOUR"
            metrics["status_reason"] = "目标被画面边界截断（bbox 顶到图像边缘，边缘内侧确有前景）"
        item = {
            "id": det["index"],
            "seed_id": det["seed_id"],
            "bbox_xyxy_src": bbox_src,
            "bbox_xyxy_model": [round(float(v), 1) for v in bbox_pl],
            "confidence": round(float(det["confidence"]), 4),
            "class_name": det.get("class_name", "seed"),
            "truncated_by_image": truncated,
            **metrics,
            "unit": {"length": "px", "area": "px^2"},
        }
        objects.append(item)

        # 标注图：bbox + Seed ID + 关键表型值
        x1, y1, x2, y2 = [int(round(v)) for v in bbox_src]
        color = (60, 160, 70) if item["status"] == "OK" else (30, 140, 230)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        label = "%s A=%.0f L=%.0f W=%.0f" % (item["seed_id"], item.get("area_px", 0),
                                             item.get("length_px", 0), item.get("width_px", 0))
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.rectangle(annotated, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(annotated, label, (x1 + 3, max(th + 2, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return {"frame_id": int(frame_id), "seed_count": len(objects),
            "objects": objects, "annotated": annotated,
            "letterbox": {"src": [params.src_w, params.src_h],
                          "dst": [params.dst_w, params.dst_h],
                          "scale": round(params.scale, 4),
                          "pad": [params.pad_x, params.pad_y]}}

# ---------------------------------------------------------------- 小工具
def _crop_image(img, bbox, margin=0):
    """按原图坐标裁一块（越界自动夹紧）。"""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    x1, y1 = max(0, x1 - margin), max(0, y1 - margin)
    x2, y2 = min(w, x2 + margin), min(h, y2 + margin)
    if x2 <= x1 or y2 <= y1:
        return None
    return img[y1:y2, x1:x2]

def write_check_sheet(picked, roi_store, out_png, cols=5, cell_w=200, img_h=96,
                      max_cells=40):
    """把核查用的 30 粒拼成一张图：每格上半是彩色 ROI，下半是板端 PL 二值 ROI。

    给人眼用：逐格看 bbox 有没有框住整粒种子、轮廓贴不贴合、长宽方向合不合理。
    只看前 max_cells 格（异常案例的完整清单在 CSV / failure_cases 里）。
    """
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    picked = picked[:max_cells]
    rows_n = (len(picked) + cols - 1) // cols
    label_h, cell_h = 40, img_h * 2 + 40
    sheet = np.full((rows_n * cell_h, cols * cell_w, 3), 255, np.uint8)
    for i, row in enumerate(picked):
        r, c = divmod(i, cols)
        y0, x0 = r * cell_h, c * cell_w
        pair = roi_store.get((row["frame_id"], row["seed_id"]))
        if pair:
            for j, crop in enumerate(pair):
                if crop is None:
                    continue
                tile = crop if crop.ndim == 3 else cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
                tile = cv2.resize(tile, (cell_w - 8, img_h), interpolation=cv2.INTER_AREA)
                sheet[y0 + j * img_h + 4:y0 + (j + 1) * img_h + 4,
                      x0 + 4:x0 + cell_w - 4] = tile
        cv2.rectangle(sheet, (x0, y0), (x0 + cell_w - 1, y0 + cell_h - 1), (190, 190, 190), 1)
        cv2.putText(sheet, "%s  f%d" % (row["seed_id"], row["frame_id"]),
                    (x0 + 6, y0 + img_h * 2 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                    (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(sheet, "A=%.0f L=%.0f W=%.0f c=%.2f %s"
                    % (row.get("area_px") or 0, row.get("length_px") or 0,
                       row.get("width_px") or 0, row.get("circularity") or 0,
                       row.get("status", "")),
                    (x0 + 6, y0 + img_h * 2 + 33), cv2.FONT_HERSHEY_SIMPLEX, 0.40,
                    (0, 0, 0), 1, cv2.LINE_AA)
    cv2.imwrite(str(out_png), sheet)
    print("人工核查拼图（%d 粒，上=彩色 ROI / 下=PL 二值 ROI）->" % len(picked), out_png)
    return out_png

def write_failure_case(pair, obj, out_path, target_w=320):
    """失败案例留档图：上半 = 彩色 ROI，下半 = 板端 PL 二值 ROI，下面写 ID 与关键数值。

    只截目标附近的一小块（不放整帧），既够看清问题，也不把无关画面带进仓库。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tiles = []
    for crop in pair:
        if crop is None:
            continue
        is_binary = crop.ndim == 2
        tile = crop if crop.ndim == 3 else cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
        scale = target_w / max(1, tile.shape[1])
        tiles.append(cv2.resize(tile, (target_w, max(1, int(round(tile.shape[0] * scale)))),
                                interpolation=cv2.INTER_NEAREST if is_binary else cv2.INTER_AREA))
    if not tiles:
        return None
    label_h = 42
    body = np.full((sum(t.shape[0] for t in tiles) + 8 * (len(tiles) - 1) + label_h,
                    target_w, 3), 255, np.uint8)
    y = 0
    for i, tile in enumerate(tiles):
        body[y:y + tile.shape[0]] = tile
        y += tile.shape[0] + 8
    lines = ["frame %d  %s  conf=%.3f  status=%s"
             % (obj["frame_id"], obj["seed_id"], obj.get("confidence", 0), obj.get("status", "")),
             "A=%.0f px2  L=%.1f  W=%.1f  AR=%.2f  circ=%.2f  regions=%s"
             % (obj.get("area_px") or 0, obj.get("length_px") or 0, obj.get("width_px") or 0,
                obj.get("aspect_ratio") or 0, obj.get("circularity") or 0,
                obj.get("region_count", "-"))]
    for i, text in enumerate(lines):
        cv2.putText(body, text, (6, y + 16 + i * 17), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                    (0, 0, 0), 1, cv2.LINE_AA)
    cv2.imwrite(str(out_path), body)
    return out_path

# ---------------------------------------------------------------- 载入检测器
def load_detections_csv(csv_path):
    """从已有的 measurements.csv 读回检测框，跳过 YOLO。

    两台机器的推理环境（ultralytics/torch 版本）不同时，同一份权重会差出几个框；
    用这个开关可以「沿用别人的检测结果、在本地把测量重算一遍」，保证测量口径一致。
    """
    by_frame = {}
    with open(csv_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("bbox_xyxy_model"):
                continue
            by_frame.setdefault(int(row["frame_id"]), []).append({
                "id": int(row["id"]) - 1,
                "bbox_xyxy": [float(v) for v in row["bbox_xyxy_model"].split()],
                "confidence": float(row["confidence"]),
                "class_name": row.get("class_name") or "seed",
            })
    return by_frame

def load_detector(model_root, conf=0.25):
    root = Path(model_root)
    spec = importlib.util.spec_from_file_location(
        "infer_seed", str(root / "src/software/ai/inference/infer_seed.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SeedDetector(weights=root / "data/models/best.pt", conf=conf)

# ---------------------------------------------------------------- 跑数据集
def run_dataset(frames_dir, pl_dir, model_root, out_json, out_csv, manual_csv,
                annotated_dir, failure_dir, conf=0.25, min_area=DEFAULT_MIN_AREA,
                max_frames=0, sheet_out=None, frames_filter="", detections_csv=""):
    frames_dir, pl_dir = Path(frames_dir), Path(pl_dir)
    out_json, out_csv, manual_csv = Path(out_json), Path(out_csv), Path(manual_csv)
    annotated_dir, failure_dir = Path(annotated_dir), Path(failure_dir)
    frames = sorted(frames_dir.glob("raw_bgr_*.png")) or sorted(frames_dir.glob("*.png"))
    if frames_filter:
        wanted = {int(v) for v in str(frames_filter).replace(" ", "").split(",") if v != ""}
        frames = [f for f in frames
                  if int(f.stem.replace("raw_bgr_", "")) in wanted]
    if max_frames:
        frames = frames[:max_frames]
    if detections_csv:
        dets_by_frame = load_detections_csv(detections_csv)
        detector = None
        print("用 %s 里已有的检测框，不跑 YOLO" % detections_csv)
    else:
        dets_by_frame = None
        detector = load_detector(model_root, conf=conf)
    annotated_dir.mkdir(parents=True, exist_ok=True)

    all_rows, per_frame, failures, roi_store = [], [], [], {}
    for index, frame_path in enumerate(frames):
        stem = frame_path.stem.replace("raw_bgr_", "")
        frame_id = int(stem)            # 帧号取自文件名，必须和 pl_gray/原图的编号一致（对齐键不能是循环序号）
        pl_path = pl_dir / ("pl_gray_%s.png" % stem)
        if not pl_path.exists():
            candidates = sorted(pl_dir.glob("pl_gray_*.png"))
            if index < len(candidates):
                pl_path = candidates[index]
            else:
                continue
        image = cv2.imread(str(frame_path))
        pl_image = cv2.imread(str(pl_path), cv2.IMREAD_UNCHANGED)
        if image is None or pl_image is None:
            continue
        if pl_image.ndim == 3:
            pl_image = cv2.cvtColor(pl_image, cv2.COLOR_BGR2GRAY)
        if dets_by_frame is not None:
            dets = dets_by_frame.get(frame_id, [])
        else:
            dets = detector.detect_seeds(image, frame_id=frame_id)
        result = analyze_frame(image, pl_image, dets, frame_id=frame_id, min_area=min_area)
        per_frame.append({"frame_id": result["frame_id"], "seed_count": result["seed_count"],
                          "letterbox": result["letterbox"]})
        out_png = annotated_dir / ("frame_%03d_annotated.png" % result["frame_id"])
        cv2.imwrite(str(out_png), result["annotated"])
        frame_rows = []
        for obj in result["objects"]:
            row = {"frame_id": result["frame_id"], **obj}
            row.pop("contour_selection", None)
            all_rows.append(row)
            frame_rows.append(row)
            roi_store[(result["frame_id"], obj["seed_id"])] = (
                _crop_image(image, obj["bbox_xyxy_src"], margin=6),
                _crop_image(pl_image, obj["bbox_xyxy_src"], margin=6))
            if obj["status"] != "OK":
                failures.append((result, obj, out_png))
        per_frame[-1]["objects"] = frame_rows

    # ---- 结构化输出 ----
    json_payload = {"schema": "agrivision.phenotyping.objects.v1",
                    "units": {"length": "px", "area": "px^2", "note": "未做毫米标定"},
                    "detector": {"weights": "data/models/best.pt", "conf": conf},
                    "frames": per_frame,
                    "objects": all_rows}
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    fields = ["frame_id", "seed_id", "id", "confidence", "bbox_xyxy_src", "area_px",
              "area_px_cross", "area_diff_pct", "perimeter_px", "length_px", "width_px",
              "aspect_ratio", "circularity", "status", "status_reason", "region_count",
              "border_contact_ratio", "bbox_white_ratio", "truncated_by_image", "class_name",
              "bbox_xyxy_model"]
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            out = dict(row)
            out["bbox_xyxy_src"] = " ".join(str(v) for v in row["bbox_xyxy_src"])
            out["bbox_xyxy_model"] = " ".join(str(v) for v in row["bbox_xyxy_model"])
            writer.writerow(out)

    # ---- 人工核查表（≥30 粒；带独立交叉核对结论）----
    manual = build_manual_check(all_rows, manual_csv)
    if sheet_out:
        write_check_sheet(manual["picked"], roi_store, sheet_out)

    # ---- 失败案例（最多留 6 张；先留 MERGED / INVALID 这类"形状问题"，再留被画面截断的）----
    failure_dir.mkdir(parents=True, exist_ok=True)
    order_key = {"MERGED": 0, "INVALID": 1, "BROKEN_CONTOUR": 2}
    failures.sort(key=lambda item: order_key.get(item[1]["status"], 3))
    # 再按帧轮转，避免留下的 6 张全挤在同一帧
    by_frame_fail = {}
    for item in failures:
        by_frame_fail.setdefault(item[0]["frame_id"], []).append(item)
    spread, round_index = [], 0
    while len(spread) < len(failures) and round_index < 200:
        added = False
        for fid in sorted(by_frame_fail):
            if round_index < len(by_frame_fail[fid]):
                spread.append(by_frame_fail[fid][round_index])
                added = True
        if not added:
            break
        round_index += 1
    failures = spread
    for order, (result, obj, out_png) in enumerate(failures[:6], start=1):
        obj_with_frame = dict(obj, frame_id=result["frame_id"])
        name = "f%03d_frame%03d_%s_%s.png" % (order, result["frame_id"],
                                             obj["seed_id"].replace(" ", "").replace("#", ""),
                                             obj["status"])
        write_failure_case(roi_store.get((result["frame_id"], obj["seed_id"])),
                           obj_with_frame, failure_dir / name)

    print("帧数 %d，目标 %d 个（OK %d / 异常 %d）"
          % (len(per_frame), len(all_rows),
             sum(1 for r in all_rows if r["status"] == "OK"), len(failures)))
    print("JSON ->", out_json, "| CSV ->", out_csv, "| 核查表 ->", manual_csv)
    print("标注图 ->", annotated_dir, "| 失败案例 ->", failure_dir)
    return json_payload, manual

def build_manual_check(rows, manual_csv, target=30):
    """人工核查表：跨帧均匀抽样 30 粒 + 独立方法交叉核对。

    ⚠️ 口径说明（报告里照抄）：表里的 `area_px_cross` / `area_diff_pct` 是**程序化交叉核对**——
    `area_px` 用 `cv2.contourArea`（边界积分），`area_px_cross` 用连通域像素计数（与轮廓追踪
    完全独立的实现），两者差得远就说明轮廓提取可疑。
    但**真正的人工核查必须有人看图**：`manual_check_30seeds_sheet.png` 把 30 粒的彩色 ROI 与
    PL 二值 ROI 拼成一张图，逐格核对「bbox 是否框住整粒种子 / 轮廓是否贴合 / 长宽方向是否合理」，
    结论填在 CSV 的 `human_verdict` 列（OK / 异常说明）。
    这 30 粒只从 `status == OK` 的目标里挑（截断/粘连的目标没法用肉眼看框）；异常目标不删，
    逐条留在 `measurements.csv`，代表案例图存在 `failure_cases/`。
    """
    by_frame = {}
    for row in rows:
        by_frame.setdefault(row["frame_id"], []).append(row)
    for fid in by_frame:
        by_frame[fid].sort(key=lambda r: r["id"])
    # 已经人工看过、填了 human_verdict 的样本原样保留（重跑不换样本，省得人要再看一遍）
    out_path = Path(manual_csv)
    previous = {}
    if out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            for old in csv.DictReader(fh):
                if old.get("human_verdict"):
                    previous[(str(old.get("frame_id")), old.get("seed_id"))] = old["human_verdict"]
    picked, seen = [], set()
    for row in rows:
        key = (str(row["frame_id"]), row["seed_id"])
        if key in previous:
            picked.append(row)
            seen.add(key)
    # 取样：每帧等分取几粒（30 粒 ÷ 10 帧 = 每帧 3 粒），帧内也按序号等间隔挑，
    # 这样 30 粒既覆盖所有帧，也覆盖每帧的画面不同位置（大小/朝向/密度都不一样）。
    # 只从 status=OK 的里挑 —— 人眼要核对"框得准不准、长宽方向对不对"，被画面截断的目标核不了。
    # 异常目标不会因此被删：逐条留在 measurements.csv 的 status 列，代表案例图在 failure_cases/。
    frame_ids = sorted(by_frame)
    per_frame = -(-target // max(1, len(frame_ids)))            # 向上取整
    for fid in frame_ids:
        healthy = [r for r in by_frame[fid]
                   if r["status"] == "OK" and (str(r["frame_id"]), r["seed_id"]) not in seen]
        if not healthy:
            continue
        if len(healthy) <= per_frame:
            for r in healthy:
                picked.append(r)
                seen.add((str(r["frame_id"]), r["seed_id"]))
            continue
        for pos in np.linspace(0, len(healthy) - 1, per_frame).round().astype(int):
            r = healthy[int(pos)]
            if (str(r["frame_id"]), r["seed_id"]) not in seen:
                picked.append(r)
                seen.add((str(r["frame_id"]), r["seed_id"]))
    picked = picked[:target]
    # 某些帧的 OK 目标不够 30÷帧数 那么多（本批第 7 帧只有 5 个），按帧顺序补足 30 粒
    if len(picked) < target:
        for fid in frame_ids:
            if len(picked) >= target:
                break
            for row in by_frame[fid]:
                if len(picked) >= target:
                    break
                key = (str(row["frame_id"]), row["seed_id"])
                if row["status"] == "OK" and key not in seen:
                    picked.append(row)
                    seen.add(key)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["frame_id", "seed_id", "bbox_xyxy_src", "area_px", "area_px_cross",
              "area_diff_pct", "length_px", "width_px", "perimeter_px", "aspect_ratio",
              "circularity", "status", "fill_ratio", "border_contact_ratio",
              "needs_human", "human_verdict", "note"]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        needs_count = 0
        for row in picked:
            area = float(row.get("area_px") or 0)
            length = float(row.get("length_px") or 0)
            width = float(row.get("width_px") or 0)
            rect_area = length * width
            fill = (area / rect_area) if rect_area > 0 else 0.0        # 轮廓面积 / 旋转外接矩形面积
            diff = float(row.get("area_diff_pct") or 0.0)
            contact = float(row.get("border_contact_ratio") or 0.0)
            needs = "YES" if (row["status"] != "OK" or diff > AREA_DIFF_ALERT
                              or fill < 0.4 or fill > 1.05) else "no"
            needs_count += 1 if needs == "YES" else 0
            note = []
            if row["status"] != "OK":
                note.append("status=%s" % row["status"])
            if diff > AREA_DIFF_ALERT:
                note.append("两种面积算法差%.1f%%" % diff)
            if contact > 0.8:
                note.append("轮廓%.0f%%贴着ROI边界(bbox偏紧?)" % (contact * 100))
            if fill < 0.4:
                note.append("填充率低(%.2f)" % fill)
            if row.get("aspect_ratio", 0) > 3:
                note.append("细长(AR=%.2f)" % row["aspect_ratio"])
            writer.writerow({"frame_id": row["frame_id"], "seed_id": row["seed_id"],
                             "bbox_xyxy_src": " ".join(str(v) for v in row["bbox_xyxy_src"]),
                             "area_px": row["area_px"],
                             "area_px_cross": row.get("area_px_cross", 0.0),
                             "area_diff_pct": round(diff, 2),
                             "length_px": row["length_px"], "width_px": row["width_px"],
                             "perimeter_px": row["perimeter_px"],
                             "aspect_ratio": row["aspect_ratio"],
                             "circularity": row["circularity"], "status": row["status"],
                             "fill_ratio": round(fill, 3),
                             "border_contact_ratio": row.get("border_contact_ratio", 0.0),
                             "needs_human": needs,
                             "human_verdict": previous.get((str(row["frame_id"]), row["seed_id"]), ""),
                             "note": "; ".join(note)})
    print("人工核查表 %d 行（其中需重点人看 %d 行）->" % (len(picked), needs_count), out_path)
    return {"rows": len(picked), "picked": picked}

def main():
    ap = argparse.ArgumentParser(description="phenotyping B：单粒种子表型测量")
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--pl-dir", required=True)
    ap.add_argument("--model-root", default=str(REPO_ROOT),
                    help="含 src/software/ai/inference/infer_seed.py 与 data/models/best.pt 的目录（默认仓库根）")
    ap.add_argument("--out-json", default="outputs/data/evidence/phenotyping/objects.json")
    ap.add_argument("--out-csv", default="outputs/data/evidence/phenotyping/measurements.csv")
    ap.add_argument("--manual-csv", default="outputs/data/evidence/phenotyping/manual_check_30seeds.csv")
    ap.add_argument("--annotated-dir", default="outputs/phenotyping/annotated")
    ap.add_argument("--failure-dir", default="outputs/phenotyping/failure_cases")
    ap.add_argument("--sheet-out", default="outputs/data/evidence/phenotyping/manual_check_30seeds_sheet.png")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--min-area", type=int, default=DEFAULT_MIN_AREA)
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--frames", default="",
                    help="只跑指定的帧序号，逗号分隔（如 5,6,7,8,9）；空=全跑")
    ap.add_argument("--detections-csv", default="",
                    help="从已有 measurements.csv 读检测框、跳过 YOLO（换机器重算测量/出图用）")
    args = ap.parse_args()
    run_dataset(args.frames_dir, args.pl_dir, args.model_root, args.out_json,
                args.out_csv, args.manual_csv, args.annotated_dir, args.failure_dir,
                conf=args.conf, min_area=args.min_area, max_frames=args.max_frames,
                sheet_out=args.sheet_out, frames_filter=args.frames,
                detections_csv=args.detections_csv)

if __name__ == "__main__":
    main()
