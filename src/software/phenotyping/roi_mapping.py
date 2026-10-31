#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phenotyping B1：YOLO bbox → ROI 坐标规范（同帧对齐 + 反映射 + 唯一 ID）。

三条硬规则（对应 phenotyping 任务书 B1）：
1. YOLO 输出统一为 **[x1, y1, x2, y2]**，坐标系 = **送进模型的那张原图**（彩色，未缩放）。
2. 网络/板端尺寸与显示尺寸之间的映射走 **letterbox**（等比例缩放 + 居中填充），
   反映射必须用 `bbox_to_src()` 反算回原图坐标，**禁止直接拿缩放后的 bbox 去裁原图**。
3. 每个目标在一个 frame 内给唯一 ID：`Seed #001`、`Seed #002`…（按 上→下、左→右 排序），
   bbox / FPGA 结果 / 原图三者的对齐键是同一个 `frame_id`。
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

from dataclasses import dataclass

@dataclass
class LetterboxParams:
    src_w: int
    src_h: int
    dst_w: int
    dst_h: int
    scale: float
    new_w: int
    new_h: int
    pad_x: int
    pad_y: int

def letterbox_params(src_w, src_h, dst_w, dst_h):
    """与 run_pipeline / pc_client 同一口径：等比例缩放后居中填充到目标尺寸。"""
    src_w, src_h = int(src_w), int(src_h)
    scale = min(dst_w / src_w, dst_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    pad_x = (dst_w - new_w) // 2
    pad_y = (dst_h - new_h) // 2
    return LetterboxParams(src_w, src_h, dst_w, dst_h, scale, new_w, new_h, pad_x, pad_y)

def bbox_to_src(bbox, params, clamp=True):
    """把 letterbox 坐标下的 bbox 反算回原图坐标（B1 的核心，别省的这一步）。"""
    x1, y1, x2, y2 = [float(v) for v in bbox]
    p = params
    out = [
        (x1 - p.pad_x) / p.scale,
        (y1 - p.pad_y) / p.scale,
        (x2 - p.pad_x) / p.scale,
        (y2 - p.pad_y) / p.scale,
    ]
    if clamp:
        out[0] = max(0.0, min(out[0], p.src_w))
        out[1] = max(0.0, min(out[1], p.src_h))
        out[2] = max(0.0, min(out[2], p.src_w))
        out[3] = max(0.0, min(out[3], p.src_h))
    return [round(v, 2) for v in out]

def bbox_to_letterbox(bbox, params):
    """原图坐标 bbox → letterbox 坐标（给"画在缩放图上"的场景用，方向与上一条相反）。"""
    x1, y1, x2, y2 = [float(v) for v in bbox]
    p = params
    return [round(x1 * p.scale + p.pad_x, 2), round(y1 * p.scale + p.pad_y, 2),
            round(x2 * p.scale + p.pad_x, 2), round(y2 * p.scale + p.pad_y, 2)]

def crop_roi(image, bbox, margin=0):
    """按原图坐标裁 ROI；margin 为向外扩展的像素数。返回 (roi, (x0, y0))。"""
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    x1 = max(0, x1 - margin)
    y1 = max(0, y1 - margin)
    x2 = min(image.shape[1], x2 + margin)
    y2 = min(image.shape[0], y2 + margin)
    if x2 <= x1 or y2 <= y1:
        return None, (x1, y1)
    return image[y1:y2, x1:x2], (x1, y1)

def assign_ids(detections):
    """按 上→下、左→右 排序并给唯一 seed_id（Seed #001 起，从 1 编号）。"""
    ordered = sorted(detections,
                     key=lambda d: (round(float(d["bbox_xyxy"][1]), 1),
                                    float(d["bbox_xyxy"][0])))
    result = []
    for index, det in enumerate(ordered, start=1):
        item = dict(det)
        item["index"] = index
        item["seed_id"] = "Seed #%03d" % index
        result.append(item)
    return result

def frame_key(frame_id):
    """bbox / FPGA 结果 / 原图的统一对齐键。"""
    return int(frame_id)
