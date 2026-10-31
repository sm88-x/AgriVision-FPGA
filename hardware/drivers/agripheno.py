
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

# coding: utf-8

# In[1]:

import io
import time
from pathlib import Path

import numpy as np
import cv2
from PIL import Image, ImageOps

def decode_image(content):
    with Image.open(io.BytesIO(content)) as source:
        mode = source.mode
        if hasattr(ImageOps, "exif_transpose"):
            image = ImageOps.exif_transpose(source)
        else:
            image = source.copy()

        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        rgb = Image.alpha_composite(background, rgba).convert("RGB")
        return np.array(rgb), mode

def prepare_image(rgb, width, height):
    """等比例缩放并补白，返回内容区域坐标，测量单位为处理图像像素。"""
    sh, sw = rgb.shape[:2]
    scale = min(width / sw, height / sh)
    nw = max(1, min(width, int(round(sw * scale))))
    nh = max(1, min(height, int(round(sh * scale))))

    resized = cv2.resize(
        rgb, (nw, nh),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    )

    left = (width - nw) // 2
    top = (height - nh) // 2

    canvas = np.full((height, width, 3), 255, np.uint8)
    canvas[top:top + nh, left:left + nw] = resized

    return canvas, (left, top, nw, nh), scale

def to_gray(rgb):
    f = rgb.astype(np.float64)
    return np.rint(
        0.299 * f[:, :, 0]
        + 0.587 * f[:, :, 1]
        + 0.114 * f[:, :, 2]
    ).astype(np.uint8)

def gradient_magnitude(gray, algorithm):
    """与现有 HLS 保持一致：|Gx|+|Gy|，此处先不饱和。"""
    g = gray.astype(np.int32)
    weight = {"Sobel": 2, "Prewitt": 1}[algorithm]

    gx = (
        -g[:-2, :-2] + g[:-2, 2:]
        -weight * g[1:-1, :-2] + weight * g[1:-1, 2:]
        -g[2:, :-2] + g[2:, 2:]
    )
    gy = (
        -g[:-2, :-2] - weight * g[:-2, 1:-1] - g[:-2, 2:]
        +g[2:, :-2] + weight * g[2:, 1:-1] + g[2:, 2:]
    )

    magnitude = np.zeros(gray.shape, np.int32)
    magnitude[1:-1, 1:-1] = np.abs(gx) + np.abs(gy)
    return magnitude

def edge_reference(gray, algorithm, threshold):
    magnitude = gradient_magnitude(gray, algorithm)
    if threshold > 0:
        result = np.where(magnitude >= threshold, 255, 0)
    else:
        result = np.clip(magnitude, 0, 255)
    return result.astype(np.uint8)

def png_bytes(array):
    stream = io.BytesIO()
    Image.fromarray(array).save(stream, format="PNG")
    return stream.getvalue()

# In[2]:

def largest_component(mask):
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), 8
    )
    if count <= 1:
        return np.zeros(mask.shape, np.uint8)

    index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return np.where(labels == index, 255, 0).astype(np.uint8)

def content_mask(shape, roi):
    mask = np.zeros(shape, np.uint8)
    x, y, w, h = roi
    mask[y:y+h, x:x+w] = 255
    return mask

def preprocess(rgb, application, profile, roi):
    """返回实际送入 PS/FPGA 的灰度图，以及有效分析区域。"""
    gray = to_gray(rgb)
    valid = content_mask(gray.shape, roi)

    if application == "Microscopy":
        # 检测大面积亮视野。检测不可靠时保留完整内容区域。
        _, bright = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        field = largest_component(cv2.bitwise_and(bright, valid))
        field_contours = cv2.findContours(
            field, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )[-2]

        if field_contours:
            contour = max(field_contours, key=cv2.contourArea)
            hull = cv2.convexHull(contour)
            filled = np.zeros_like(gray)
            cv2.drawContours(filled, [hull], -1, 255, cv2.FILLED)
            ratio = np.count_nonzero(filled) / max(
                1, np.count_nonzero(valid)
            )
            if 0.25 < ratio < 0.95:
                valid = cv2.bitwise_and(filled, valid)

        # 排除视野边缘；尺寸随当前处理图像调整。
        margin = max(3, int(round(min(gray.shape) * 0.015)))
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2*margin+1, 2*margin+1)
        )
        valid = cv2.erode(valid, kernel)

        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        gray = cv2.GaussianBlur(gray, (3, 3), 0.8)

    elif application == "Plant Phenotyping":
        # 对两个后端使用完全相同的轻度去噪输入。
        gray = cv2.GaussianBlur(gray, (3, 3), 0.65)

    return gray, valid

def green_leaf_mask(rgb, roi):
    """绿色叶片区域估计：颜色约束用于抑制灰色阴影。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    f = rgb.astype(np.float32)
    red, green, blue = f[:, :, 0], f[:, :, 1], f[:, :, 2]

    # 归一化超绿指标；绿色低饱和叶片也可保留。
    excess = (2*green - red - blue) / (red + green + blue + 1.0)

    mask = (
        (hsv[:, :, 0] >= 22)
        & (hsv[:, :, 0] <= 100)
        & (hsv[:, :, 1] >= 22)
        & (hsv[:, :, 2] >= 20)
        & (excess > 0.045)
    ).astype(np.uint8) * 255

    mask = cv2.bitwise_and(mask, content_mask(mask.shape, roi))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask

def extract_candidates(edge, roi, close_size=3):
    binary = np.where(edge > 0, 255, 0).astype(np.uint8)
    if close_size > 1:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (close_size, close_size)
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    contours = cv2.findContours(
        binary.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )[-2]

    x0, y0, rw, rh = roi
    floor = max(4.0, rw * rh * 0.0001)
    valid = []
    rejected = 0

    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < floor:
            continue

        x, y, w, h = cv2.boundingRect(contour)
        if (
            x <= x0 + 1 or y <= y0 + 1
            or x+w >= x0+rw-1 or y+h >= y0+rh-1
        ):
            rejected += 1
            continue

        if area < rw * rh * 0.90:
            valid.append(contour)

    return valid, binary, rejected

def estimate_min_area(contours, roi):
    _, _, w, h = roi
    floor = max(8.0, w*h*0.0008)
    areas = np.sort(np.array(
        [cv2.contourArea(c) for c in contours], dtype=float
    ))
    areas = areas[areas > 0]

    if len(areas) == 0:
        return floor

    if len(areas) > 1:
        ratios = areas[1:] / np.maximum(areas[:-1], 1)
        index = int(np.argmax(ratios))
        if ratios[index] >= 5:
            return max(
                floor, float(np.sqrt(areas[index]*areas[index+1]))
            )

    return max(floor, float(np.median(areas)*0.08))

def choose_parameters(gray, algorithm, roi, application, profile,
                      rgb, valid):
    magnitude = gradient_magnitude(gray, algorithm)
    positive = magnitude[(valid > 0) & (magnitude > 0)]

    if positive.size == 0:
        return {
            "threshold": 40, "min_area": 100.0,
            "note": "无有效梯度，请检查图片",
        }

    if application == "Microscopy":
        # 更偏向保留强边缘，避免整片橙色覆盖。
        threshold = int(np.clip(np.percentile(positive, 72), 5, 255))
        return {
            "threshold": threshold,
            "min_area": 0.0,
            "note": "显微增强 + 视野边界屏蔽；只展示边缘，不统计细胞",
        }

    if application == "Plant Phenotyping" and profile == "Green leaves":
        mask = green_leaf_mask(rgb, roi)
        contours, _, _ = extract_candidates(mask, roi, close_size=1)

        boundary = cv2.morphologyEx(
            mask, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)
        )
        near_boundary = magnitude[
            (boundary > 0) & (valid > 0) & (magnitude > 0)
        ]
        values = near_boundary if near_boundary.size else positive
        threshold = int(np.clip(np.percentile(values, 35), 5, 255))

        return {
            "threshold": threshold,
            "min_area": estimate_min_area(contours, roi),
            "note": "绿色区域约束 + 当前后端边缘支持检查；不适用黄叶或绿色背景",
        }

    # 种子保留候选阈值比较。
    base = float(np.percentile(positive, 45))
    choices = sorted(set(
        int(np.clip(round(base*f), 5, 255))
        for f in [0.45, 0.65, 0.85, 1.0, 1.25]
    ))
    trials = []

    for threshold in choices:
        edge = np.where(magnitude >= threshold, 255, 0).astype(np.uint8)
        contours, _, _ = extract_candidates(edge, roi)
        area_min = estimate_min_area(contours, roi)
        kept = [c for c in contours if cv2.contourArea(c) >= area_min]
        solidity = []

        for contour in kept:
            hull_area = cv2.contourArea(cv2.convexHull(contour))
            solidity.append(
                cv2.contourArea(contour)/hull_area if hull_area else 0
            )

        trials.append({
            "threshold": threshold,
            "min_area": area_min,
            "count": len(kept),
            "area": sum(cv2.contourArea(c) for c in kept),
            "solidity": float(np.mean(solidity)) if solidity else 0,
        })

    maximum = max([t["area"] for t in trials] + [1.0])
    for i, trial in enumerate(trials):
        neighbors = trials[max(0, i-1):min(len(trials), i+2)]
        stability = np.mean([
            1/(1+abs(trial["count"]-n["count"])) for n in neighbors
        ])
        trial["score"] = (
            0.55*trial["area"]/maximum
            + 0.30*trial["solidity"] + 0.15*stability
        ) if trial["count"] else -1

    best = max(trials, key=lambda t: t["score"])
    return {
        "threshold": best["threshold"],
        "min_area": best["min_area"],
        "note": "分离种子的启发式估计；大小差异明显时请检查小目标是否漏检",
    }

def analyze_phenotype(rgb, edge, roi, min_area, profile):
    unsupported = 0

    if profile == "Green leaves":
        region = green_leaf_mask(rgb, roi)
        contours, clean, rejected = extract_candidates(
            region, roi, close_size=1
        )

        # 颜色区域的边界须有实际后端边缘支持。
        support = cv2.dilate(
            edge, np.ones((5, 5), np.uint8), iterations=1
        )
        accepted = []
        for contour in contours:
            boundary = np.zeros(edge.shape, np.uint8)
            cv2.drawContours(boundary, [contour], -1, 255, 1)
            pixels = boundary > 0
            coverage = float(np.mean(support[pixels] > 0))
            if coverage >= 0.30:
                accepted.append(contour)
            else:
                unsupported += 1
        contours = accepted

    else:
        contours, clean, rejected = extract_candidates(edge, roi)

    contours = [c for c in contours if cv2.contourArea(c) >= min_area]
    contours.sort(key=lambda c: (
        cv2.boundingRect(c)[1], cv2.boundingRect(c)[0]
    ))

    annotated = rgb.copy()
    objects = []

    for contour in contours:
        rect = cv2.minAreaRect(contour)
        a, b = rect[1]
        if min(a, b) <= 0:
            continue

        object_id = len(objects)+1
        box_float = cv2.boxPoints(rect)
        box = np.rint(box_float).astype(np.int32)
        x, y, w, h = cv2.boundingRect(contour)

        cv2.drawContours(annotated, [contour], -1, (0, 235, 170), 1)
        cv2.polylines(annotated, [box], True, (255, 180, 60), 1)

        for color, thickness in [((0, 0, 0), 3), ((255, 255, 255), 1)]:
            cv2.putText(
                annotated, str(object_id), (x, max(15, y-4)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, thickness
            )

        objects.append({
            "id": object_id,
            "area": float(cv2.contourArea(contour)),
            "length": float(max(a, b)),
            "width": float(min(a, b)),
            "perimeter": float(cv2.arcLength(contour, True)),
            "bbox": [int(x), int(y), int(w), int(h)],
            "rotated_box": box_float.tolist(),
        })

    result = {
        "count": len(objects),
        "objects": objects,
        "rejected_border": rejected,
        "unsupported_regions": unsupported,
        "method": (
            "ps_green_region_with_backend_edge_support"
            if profile == "Green leaves" else "backend_edge_contours"
        ),
    }
    for key in ["area", "length", "width", "perimeter"]:
        result["avg_"+key] = (
            float(np.mean([o[key] for o in objects])) if objects else 0.0
        )
    result["units"] = {
        "area": "pixel²", "length": "pixel",
        "width": "pixel", "perimeter": "pixel",
    }
    return result, annotated, clean

# In[3]:

class AgriEngine:
    def __init__(self):
        self.driver = None
        self.inp = None
        self.out = None
        self.shape = None
        self.fault = False
        self.verified = False

    def connect(self, bitfile):
        from edge_morph_driver import SobelParamDriver
        self.driver = SobelParamDriver(bitfile=str(bitfile))
        self.fault = False
        self.verified = False

    def fpga(self, gray, algorithm, threshold):
        if self.driver is None:
            raise RuntimeError("尚未加载 Overlay")
        if self.fault:
            raise RuntimeError("上次 FPGA 调用异常，请恢复板端后重新初始化")

        from pynq import allocate
        h, w = gray.shape

        try:
            if self.shape != gray.shape:
                if self.inp is not None:
                    self.inp.freebuffer()
                    self.inp = None
                if self.out is not None:
                    self.out.freebuffer()
                    self.out = None

                self.shape = None
                self.inp = allocate(shape=(h*w,), dtype=np.uint8)
                self.out = allocate(shape=(h*w,), dtype=np.uint8)
                self.shape = gray.shape

            self.inp[:] = gray.reshape(-1)
            self.inp.flush()
            seconds = self.driver.run_frame(
                self.inp, self.out,
                width=w, height=h,
                mode={"Sobel": 0, "Prewitt": 1}[algorithm],
                threshold=int(threshold),
                timeout=5.0,
            )
            self.out.invalidate()
            result = np.array(self.out, copy=True).reshape(h, w)

        except BaseException:
            self.fault = True
            self.verified = False
            raise

        self.verified = True
        return result, float(seconds*1000)

    def analyze(
        self, source_rgb, size, algorithm, backend,
        application, automatic=True, threshold=40,
        min_area=1000, profile="Seeds",
    ):
        started = time.perf_counter()
        w, h = size
        rgb, roi, scale = prepare_image(source_rgb, w, h)

        prep_start = time.perf_counter()
        gray, valid = preprocess(rgb, application, profile, roi)
        preprocess_ms = (time.perf_counter()-prep_start)*1000

        parameter_start = time.perf_counter()
        if automatic:
            parameters = choose_parameters(
                gray, algorithm, roi, application, profile, rgb, valid
            )
        else:
            parameters = {
                "threshold": int(threshold),
                "min_area": float(min_area),
                "note": "手动参数；预处理和所选分析方法仍然生效",
            }
        parameter_ms = (time.perf_counter()-parameter_start)*1000
        used_threshold = parameters["threshold"]

        # Golden 与 FPGA 使用完全相同的预处理灰度输入。
        edge_reference(gray, algorithm, used_threshold)
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            reference = edge_reference(gray, algorithm, used_threshold)
            samples.append((time.perf_counter()-t0)*1000)

        ps_ms = float(np.median(samples))
        fpga_ms = None
        golden = None
        different_pixels = None

        if backend == "FPGA":
            raw_edge, fpga_ms = self.fpga(gray, algorithm, used_threshold)
            different_pixels = int(np.count_nonzero(raw_edge != reference))
            golden = different_pixels == 0
            if not golden:
                raise RuntimeError(
                    "Golden FAIL：{} 个像素不同，停止分析".format(
                        different_pixels
                    )
                )
        else:
            raw_edge = reference

        # 原始后端输出保留；有效区域限制只用于应用后处理。
        binary = (
            raw_edge.copy() if used_threshold > 0
            else np.where(raw_edge >= 40, 255, 0).astype(np.uint8)
        )
        binary[valid == 0] = 0

        phenotype = None
        clean = None

        if application == "Plant Phenotyping":
            phenotype, annotated, clean = analyze_phenotype(
                rgb, binary, roi, parameters["min_area"], profile
            )
            display_edge = raw_edge
        else:
            annotated = rgb.copy()
            selected = binary > 0

            # 半透明叠加，避免实色覆盖显微细节。
            color = np.array([0, 235, 220], dtype=np.float32)
            annotated[selected] = np.rint(
                0.65*rgb[selected].astype(np.float32)+0.35*color
            ).astype(np.uint8)

            display_edge = binary if application == "Microscopy" else raw_edge

        return {
            "original_rgb": rgb,
            "input_gray": gray,
            "edge_result": raw_edge,
            "display_edge": display_edge,
            "annotated_rgb": annotated,
            "clean_edges": clean,
            "phenotype_result": phenotype,
            "parameters": parameters,
            "ps_ms": ps_ms,
            "ps_samples_ms": samples,
            "fpga_ms": fpga_ms,
            "golden": golden,
            "different_pixels": different_pixels,
            "preprocess_ms": preprocess_ms,
            "parameter_ms": parameter_ms,
            "analysis_ms": (time.perf_counter()-started)*1000,
            "scale": scale,
            "size": [w, h],
            "backend": backend,
            "algorithm": algorithm,
            "application": application,
            "profile": profile,
        }

