#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phenotyping B4：独立脚本复核 30 粒的像素测量结果（对应"至少抽查 10 粒"的要求）。

思路是「换一套代码把同样的东西重算一遍」，不复用 phenotyping.py 的测量函数，
避免同一个实现错两次：
    area_ref             ：连通域数像素（不画轮廓、不做边界积分）
    length_ref/width_ref ：暴力旋转卡壳求最小外接矩形（0.25° 步进扫 0~90°，不用 cv2.minAreaRect）
    perimeter_ref        ：自己把轮廓相邻点距离加起来（不用 cv2.arcLength）
另外做两条「bbox 覆盖对不对」的自动检查（只记录，不参与结论）：
    bbox_fg_ratio    ：bbox 内板端二值前景占比（太小说明框里没目标）
    outside_fg_ratio ：紧贴 bbox 外侧一圈的前景占比（密集场景里旁边就是种子，天然偏高，所以只看不用）

复核结论写 `recheck` 列；人工核查结论仍写 `human_verdict` 列。
用法：
    python src/software/phenotyping/verify_measurements.py
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
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phenotyping import preprocess_mask       # 与主流程同一套 ROI 预处理，保证口径一致

def min_area_rect_bruteforce(points, step_deg=0.25):
    """暴力旋转卡壳：按 0.25° 步进旋转点云，取面积最小的包围盒（长边, 短边）。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    best = None
    for deg in np.arange(0.0, 90.0, step_deg):
        rad = np.deg2rad(deg)
        cos, sin = np.cos(rad), np.sin(rad)
        u = pts[:, 0] * cos + pts[:, 1] * sin
        v = -pts[:, 0] * sin + pts[:, 1] * cos
        w = float(u.max() - u.min())
        h = float(v.max() - v.min())
        if best is None or w * h < best[0]:
            best = (w * h, w, h)
    _, w, h = best
    return max(w, h), min(w, h)

def perimeter_by_steps(contour):
    """自己算周长：轮廓相邻点（含首尾闭合）的欧氏距离求和。"""
    pts = contour.reshape(-1, 2).astype(np.float64)
    if len(pts) < 2:
        return 0.0
    d = np.diff(np.vstack([pts, pts[:1]]), axis=0)
    return float(np.sum(np.hypot(d[:, 0], d[:, 1])))

def fill_holes(mask):
    """把二值图里被封住的洞填掉；从加了一圈 0 边界的反色图 floodFill，剩下的白就是洞。

    加那圈 0 边界是为了让「被种子切成几块的背景」重新连成一片，
    否则贴边的那几块背景会被误当成洞填掉，面积就虚高。种皮纹理暗斑属于真洞，要填。
    """
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    flood = cv2.bitwise_not(padded)
    seed_mask = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
    cv2.floodFill(flood, seed_mask, (0, 0), 0)
    filled = cv2.bitwise_or(padded, flood)
    return filled[1:-1, 1:-1]

def largest_component_area(mask):
    """按连通域像素个数取最大的一块（先填洞再数）。"""
    filled = fill_holes(mask)
    n_cc, _, stats, _ = cv2.connectedComponentsWithStats((filled > 0).astype(np.uint8), 8)
    return int(stats[1:, cv2.CC_STAT_AREA].max()) if n_cc > 1 else 0

def recheck_one(pl_image, bbox, margin=2):
    """对一个 bbox 独立复算一遍，返回各参考值与两条覆盖检查。"""
    x1, y1, x2, y2 = [int(round(float(v))) for v in bbox]
    x1c, y1c = max(0, x1 - margin), max(0, y1 - margin)
    x2c, y2c = min(pl_image.shape[1], x2 + margin), min(pl_image.shape[0], y2 + margin)
    roi = pl_image[y1c:y2c, x1c:x2c]
    if roi.size == 0:
        return None
    raw = np.where(roi > 0, 255, 0).astype(np.uint8)
    mask = preprocess_mask(raw)
    area_raw = largest_component_area(raw)
    area_ref = largest_component_area(mask)
    if area_ref == 0:
        return None
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contour = max(contours, key=cv2.contourArea)
    length_ref, width_ref = min_area_rect_bruteforce(cv2.convexHull(contour))
    perimeter_ref = perimeter_by_steps(contour)
    inner = int(np.count_nonzero(pl_image[y1:y2, x1:x2]))
    bbox_fg_ratio = float(inner) / max(1, (y2 - y1) * (x2 - x1))
    pad = 3
    oy1, oy2 = max(0, y1 - pad), min(pl_image.shape[0], y2 + pad)
    ox1, ox2 = max(0, x1 - pad), min(pl_image.shape[1], x2 + pad)
    outer_total = int(np.count_nonzero(pl_image[oy1:oy2, ox1:ox2]))
    outer_ring = max(1, (oy2 - oy1) * (ox2 - ox1) - max(0, y2 - y1) * max(0, x2 - x1))
    outside_fg_ratio = max(0.0, float(outer_total - inner)) / outer_ring
    return {"area_ref": area_ref, "area_raw": area_raw,
            "length_ref": round(length_ref, 2), "width_ref": round(width_ref, 2),
            "perimeter_ref": round(perimeter_ref, 2),
            "bbox_fg_ratio": round(bbox_fg_ratio, 3),
            "outside_fg_ratio": round(outside_fg_ratio, 3)}

def main():
    ap = argparse.ArgumentParser(description="phenotyping B4 独立脚本复核")
    ap.add_argument("--manual-csv", default="data/evidence/phenotyping/manual_check_30seeds.csv")
    ap.add_argument("--out-csv", default="data/evidence/phenotyping/recheck_30seeds.csv")
    ap.add_argument("--pl-dir", default="hardware/runtime/image_run01_pc")
    ap.add_argument("--pl-pattern", default="pl_gray_%06d.png")
    args = ap.parse_args()

    pl_dir = Path(args.pl_dir)
    cache = {}

    def pl_of(frame_id):
        if frame_id not in cache:
            img = cv2.imread(str(pl_dir / (args.pl_pattern % frame_id)), cv2.IMREAD_UNCHANGED)
            if img is not None and img.ndim == 3:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            cache[frame_id] = img
        return cache[frame_id]

    rows = list(csv.DictReader(open(args.manual_csv, encoding="utf-8")))
    fields = ["frame_id", "seed_id", "area_px", "area_ref", "area_raw", "area_diff_pct",
              "length_px", "length_ref", "length_diff_px",
              "width_px", "width_ref", "width_diff_px",
              "perimeter_px", "perimeter_ref", "perimeter_diff_pct",
              "bbox_fg_ratio", "outside_fg_ratio", "recheck", "human_verdict"]
    out_path = Path(args.out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    diffs = {"area": [], "length": [], "width": [], "perimeter": []}
    flags = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            frame_id = int(row["frame_id"])
            ref = recheck_one(pl_of(frame_id), row["bbox_xyxy_src"].split())
            if ref is None:
                writer.writerow({"frame_id": frame_id, "seed_id": row["seed_id"],
                                 "recheck": "无法复核（ROI 内没有前景）",
                                 "human_verdict": row.get("human_verdict", "")})
                continue
            area = float(row["area_px"] or 0)
            length = float(row["length_px"] or 0)
            width = float(row["width_px"] or 0)
            perimeter = float(row["perimeter_px"] or 0)
            d_area = abs(area - ref["area_ref"]) / area * 100 if area else 0.0
            d_len = abs(length - ref["length_ref"])
            d_wid = abs(width - ref["width_ref"])
            d_per = abs(perimeter - ref["perimeter_ref"]) / perimeter * 100 if perimeter else 0.0
            diffs["area"].append(d_area)
            diffs["length"].append(d_len)
            diffs["width"].append(d_wid)
            diffs["perimeter"].append(d_per)
            note = []
            if d_area > 10:
                note.append("面积差%.1f%%" % d_area)
            if d_len > 3:
                note.append("长差%.1fpx" % d_len)
            if d_wid > 3:
                note.append("宽差%.1fpx" % d_wid)
            if ref["bbox_fg_ratio"] < 0.10:
                note.append("框内前景只占%.0f%%" % (ref["bbox_fg_ratio"] * 100))
            if note:
                flags += 1
            writer.writerow({"frame_id": frame_id, "seed_id": row["seed_id"],
                             "area_px": row["area_px"], "area_ref": ref["area_ref"],
                             "area_raw": ref["area_raw"], "area_diff_pct": round(d_area, 2),
                             "length_px": row["length_px"], "length_ref": ref["length_ref"],
                             "length_diff_px": round(d_len, 2),
                             "width_px": row["width_px"], "width_ref": ref["width_ref"],
                             "width_diff_px": round(d_wid, 2),
                             "perimeter_px": row["perimeter_px"],
                             "perimeter_ref": ref["perimeter_ref"],
                             "perimeter_diff_pct": round(d_per, 2),
                             "bbox_fg_ratio": ref["bbox_fg_ratio"],
                             "outside_fg_ratio": ref["outside_fg_ratio"],
                             "recheck": "一致" if not note else "; ".join(note),
                             "human_verdict": row.get("human_verdict", "")})
    print("复核 %d 粒 ->" % len(rows), out_path)
    for key, values in diffs.items():
        if not values:
            continue
        values = sorted(values)
        print("  %-9s 中位差 %.2f  最大差 %.2f" % (key, values[len(values) // 2], values[-1]))
    print("  需要人工再看一眼的：%d 粒" % flags)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
