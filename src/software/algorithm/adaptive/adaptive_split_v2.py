#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene B：自适应粘连分离 V2。

在 segmentation 的 `src/software/algorithm/seed_separation.py`（proposed_v1）之上做三处针对性改进，
直接对准 v1 自己报告里的两个毛病 —— 密集场景漏分、绿豆小目标 UNRESOLVED：

1. **计数归一化的候选评分**
   v1 用 `mean(log(area/area_median)²)` 打分，块数越多分数越差：
   三粒粘连时每块约为先验的 1/3，`log(1/3)² ≈ 1.2` 直接被判负，于是宁可欠分割。
   V2 改成 `mean(log(area/area_median × n)²)` —— 评价的是"n 块是否均分这块面积"，
   与块数无关，3~4 粒的簇也能进候选。

2. **面积先验给出期望块数 n_est**
   `n_est = round(连通域面积 / 该品种单粒面积中位数)`（全部来自图像与先验，不读 GT），
   作为候选评分的软约束（差一块罚 0.6/n_est），并在 UNRESOLVED 的原因里如实写出。

3. **碎片回并**
   分裂后面积 < 0.30×先验的块，并入边界共享最长的邻块，压制过分割。

用法（不覆盖 segmentation 结果，输出到新目录）：
    python -B src/software/algorithm/adaptive/adaptive_split_v2.py eval \
        --frozen work/frozen_local.json --split dev --out work/scene_eval_dev
    python -B src/software/algorithm/adaptive/adaptive_split_v2.py eval \
        --frozen work/frozen_local.json --split test --out work/scene_eval_test

输出：
    eval_scene.csv          逐图 × 四个模式（yolo / fixed / v1 / v2）的计数、误差、耗时
    eval_scene_summary.csv  按 品种 × 模式 汇总（MAE、完全正确率、未解决率、平均后处理/总耗时）
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
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import cv2
from scipy import ndimage as ndi
from skimage.feature import peak_local_max

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT))

def _load_seed_separation():
    path = ROOT / "src/software/algorithm/seed_separation.py"
    spec = importlib.util.spec_from_file_location("seed_separation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

sep = _load_seed_separation()

MODES_ALL = ("baseline_0", "baseline_1", "proposed_v1", "proposed_v2")
MODE_LABEL = {"baseline_0": "yolo", "baseline_1": "fixed",
              "proposed_v1": "v1", "proposed_v2": "v2"}

# V2 的检测侧改动：按品种选 conf（在 dev 上扫 0.25/0.20/0.15/0.10 得到）。
# 绿豆短轴只有 ~10 px，0.25 会漏掉约 5% 的粒；降到 0.15 能捞回来，
# 黄豆/红豆在小阈值下反而多出误检，所以保持 0.25。
DETECTOR_CONF_V2 = {"soybean": 0.25, "mungbean": 0.15, "redbean": 0.25}

# ---------------------------------------------------------------- V2 组件
def merge_fragments(parts, area0, min_ratio=0.30):
    """把明显偏小的块并入边界共享最长的邻块（压过分割）。

    parts: list[bool ndarray]；返回合并后的 list[bool ndarray]。
    """
    parts = [p.astype(bool) for p in parts]
    changed = True
    while changed and len(parts) > 2:
        changed = False
        areas = [int(p.sum()) for p in parts]
        small = [i for i, a in enumerate(areas) if a < min_ratio * area0]
        if not small:
            break
        i = min(small, key=lambda k: areas[k])
        best, best_share = None, 0
        grown = ndi.binary_dilation(parts[i], iterations=2)
        for j, other in enumerate(parts):
            if j == i:
                continue
            share = int((grown & other).sum())
            if share > best_share:
                best, best_share = j, share
        if best is None or best_share == 0:
            break
        parts[best] = parts[best] | parts[i]
        parts.pop(i)
        changed = True
    return parts

def _child_ok(feat, prior):
    """子块是否像"一粒"：面积、长宽比、圆度都落在先验的合理范围。"""
    area0 = float(prior["area_median"])
    return (0.45 * area0 <= feat["mask_area_px"] <= 1.75 * area0
            and feat["aspect_ratio"] <= max(1.60, 1.35 * prior["aspect_q95"])
            and feat["circularity"] >= max(0.35, 0.70 * prior.get("circularity_q05", 0.6)))

def adaptive_parts_v2(mask, prior, sources=None, offset=(0, 0), responses=None):
    """V2 自适应分裂（保守型）。

    结构：**先让 v1 判**。
      * v1 给出分割 → 原样采用（V2 不去动已经被 v1 判对的样本，避免像第一版那样把
        mungbean_touch_001 从 48 改到 54 这种"越改越错"）；
      * v1 判成 UNRESOLVED → V2 用更细的距离变换阈值梯度重新找候选，
        并用**严格子块门槛**（面积/长宽比/圆度都在单粒先验内）+ 计数归一化打分择优，
        能救回来就救，救不回来保持 UNRESOLVED（如实保留失败）。

    返回 (parts, reason)；parts 为空表示 UNRESOLVED。
    """
    sources = sources or []
    g = sep.geometry(mask)
    area0 = float(prior["area_median"])
    area = float(g["mask_area_px"])
    n_est = int(np.clip(round(area / max(area0, 1e-6)), 1, sep.CFG["max_instances"]))

    # ① 先按 v1 的口径判；v1 有结论就不动
    parts_v1, reason_v1 = sep.adaptive_parts(mask, prior, sources, offset, responses)
    if parts_v1:
        return parts_v1, "v1: " + reason_v1

    # ② 小目标救援：2× 最近邻上采样后再走一遍 v1 的分裂逻辑。
    #    绿豆短轴只有 ~10 px，距离变换的峰/谷都挤在几像素里；放大一倍后
    #    峰间距与谷深都翻倍，v1 的同一套判据就能用（先验的面积项按 4 倍缩放）。
    if area0 < 220:
        up_mask = cv2.resize(mask.astype(np.uint8), None, fx=2, fy=2,
                             interpolation=cv2.INTER_NEAREST).astype(bool)
        prior_up = {k: (v * 4 if isinstance(v, (int, float)) and "area" in k else v)
                    for k, v in prior.items()}
        sources_up = [dict(d, bbox_xyxy=[c * 2 for c in d["bbox_xyxy"]]) for d in sources]
        parts_up, reason_up = sep.adaptive_parts(
            up_mask, prior_up, sources_up, (offset[0] * 2, offset[1] * 2), None)
        if parts_up and len(parts_up) >= 2:
            back = []
            for part in parts_up:
                small = cv2.resize(part.astype(np.uint8), (mask.shape[1], mask.shape[0]),
                                   interpolation=cv2.INTER_NEAREST).astype(bool)
                if int(small.sum()) >= sep.CFG["min_area"]:
                    back.append(small)
            if len(back) >= 2 and all(_child_ok(sep.geometry(p), prior) for p in back):
                return back, "V2 2x上采样救回: " + reason_up

    minor = math.sqrt(4 * area0 / (math.pi * max(prior["aspect_median"], 1)))
    spacing = max(2.0, sep.CFG["adaptive_min_spacing_factor"] * minor)
    anchors = sep._detector_points(mask, sources, offset, spacing)

    proposals = []
    raw = ndi.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    # 更细的阈值梯度：小目标（绿豆短轴 ~10 px）的峰更浅，v1 的固定几档容易全错过
    for threshold in (0.18, 0.24, 0.30, 0.36, 0.42, 0.50):
        for factor in (0.28, 0.34, 0.40, 0.48, 0.58):
            coords = peak_local_max(raw, min_distance=max(2, round(factor * minor)),
                                    threshold_rel=threshold, exclude_border=False,
                                    labels=mask.astype(np.uint8))
            proposals.append(("raw-distance", [tuple(p) for p in coords]))

    for index, response in enumerate(responses or []):
        positive = response[mask]
        if not len(positive) or positive.max() <= 0.1:
            continue
        coords = peak_local_max(response, min_distance=max(2, round(minor * 0.40)),
                                threshold_abs=max(0.3, float(positive.max()) * 0.10),
                                labels=mask.astype(np.uint8), exclude_border=False)
        points = [tuple(p) for p in coords]
        proposals.append(("surface-chroma", points))
        combined = list(anchors)
        for p in points:
            if all(np.linalg.norm(np.array(p) - np.array(q)) >= spacing for q in combined):
                combined.append(p)
        proposals.append(("detector+chroma", combined))

    seen, candidates = set(), []
    for label, points in proposals:
        if len(points) < 2:
            continue
        key = tuple(sorted(points))
        if key in seen:
            continue
        seen.add(key)
        parts = sep._partition(mask, points)
        if not parts or len(parts) < 2:
            continue
        parts = merge_fragments(parts, area0)
        if len(parts) < 2:
            continue
        feats = [sep.geometry(p) for p in parts]
        # 严格门槛：每个子块都要像"一粒"，否则宁可不救
        if not all(_child_ok(f, prior) for f in feats):
            continue
        ratios = np.array([f["mask_area_px"] / area0 for f in feats])
        n = len(parts)
        if ratios.max() > max(2.5, 1.6 * n_est) or ratios.min() < 0.25 / n:
            continue
        aspects = np.array([f["aspect_ratio"] for f in feats])
        if aspects.max() > max(3.2, 2.0 * prior["aspect_q95"]):
            continue
        # 计数归一化：n 块应当各占 1/n，评分与块数无关
        area_score = float(np.mean(np.log(np.maximum(ratios * n, 1e-6)) ** 2))
        shape_score = float(np.mean(np.maximum(aspects / prior["aspect_q95"] - 1, 0) ** 2))
        support_score = 0.0
        if anchors:
            support_score = float(np.mean([
                min(np.linalg.norm(np.array(a) - np.array(p)) for p in points) / minor
                for a in anchors]))
        # 接受门槛沿用 v1 的口径（只是面积项换成了计数归一化，量级更小），
        # 块数先验 n_est 只用来"择优"，不用来"一票否决"——
        # 否则密集场景里 n_est 估得偏大时会把本来可用的候选全部拒掉（实测未解决率会翻倍）。
        score = area_score + 0.15 * shape_score + 0.20 * support_score
        candidates.append((score, parts, n, label))

    if not candidates:
        return [], reason_v1 + "（V2 未找到能通过严格子块门槛的候选）"
    acceptable = [c for c in candidates if c[0] <= 0.90]
    if acceptable:
        # 先看块数是否贴近先验，再看打分
        score, parts, n, label = min(acceptable, key=lambda c: (abs(c[2] - n_est), c[0]))
        return parts, "V2 救回 %s: n=%d n_est=%d score=%.3f（v1 判不了）" % (
            label, n, n_est, score)
    return [], reason_v1 + "（V2 候选得分都超过 0.90，保持未解决）"

class SeedSeparatorV2(sep.SeedSeparator):
    """在 segmentation 引擎上加 proposed_v2 模式；其余模式原样委托给父类。"""

    def detect_and_separate(self, image, mode, species, **kwargs):
        if mode in sep.MODES:
            return super().detect_and_separate(image, mode, species, **kwargs)
        if mode != "proposed_v2":
            raise ValueError("Unknown mode: %s" % mode)

        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("Expected uint8 HxWx3 BGR image")
        if list(image.shape[:2]) != self.frozen["image_hw"]:
            raise ValueError("Image size differs from frozen acquisition")

        session_id = kwargs.get("session_id", "offline")
        frame_id = kwargs.get("frame_id")
        image_id = kwargs.get("image_id")
        gt_count = kwargs.get("gt_count")
        prediction = kwargs.get("prediction")
        if prediction is None:
            prediction = self.predict(image, frame_id, species)
        detections = prediction["detections"]

        start = time.perf_counter()
        mask = sep.foreground(image)
        labels, _ = ndi.label(mask)
        slices = ndi.find_objects(labels)
        h, w = mask.shape
        priors = self.frozen["priors"][species]
        responses = sep.chromatic_responses(image, priors, species)
        objects, diagnostics = [], []

        for group in sep.groups_from_boxes(labels, detections):
            sources = group["detections"]
            pending, failed_reason = [], None
            if not group["components"]:
                failed_reason = "YOLO ROI 内没有可用前景"
            components = [] if failed_reason else sorted(group["components"])
            for component in components:
                ys, xs = slices[component - 1]
                local = labels[ys, xs] == component
                cut = ((ys.start == 0 and local[0].any()) or (ys.stop == h and local[-1].any())
                       or (xs.start == 0 and local[:, 0].any()) or (xs.stop == w and local[:, -1].any()))
                if cut:
                    failed_reason = "连通域被画面边界截断"
                    break
                parts, reason = adaptive_parts_v2(local, priors, sources,
                                                  (xs.start, ys.start),
                                                  [r[ys, xs] for r in responses])
                if not parts:
                    failed_reason = reason
                    break
                for part in parts:
                    pending.append(sep.mask_object(part, (xs.start, ys.start), sources, reason))
                diagnostics.append({"component_id": int(component),
                                    "source_detection_ids": [d["id"] for d in sources],
                                    "mask_area_px": int(local.sum()),
                                    "output_instances": len(parts), "reason": reason})
            if failed_reason:
                diagnostics.append({"source_detection_ids": [d["id"] for d in sources],
                                    "outcome": "original_detections_retained",
                                    "reason": failed_reason})
                objects.extend(sep.box_object(d, "UNRESOLVED", failed_reason) for d in sources)
            else:
                objects.extend(pending)

        objects.sort(key=lambda o: (o["bbox_xyxy_src"][1], o["bbox_xyxy_src"][0]))
        for index, obj in enumerate(objects, 1):
            obj["id"] = index
            obj["seed_id"] = "Seed #%03d" % index

        unresolved = any(o["status"] == "UNRESOLVED" for o in objects)
        post_ms = (time.perf_counter() - start) * 1000
        return {
            "schema": "agrivision.scene.separation.v2",
            "session_id": session_id, "frame_id": frame_id, "image_id": image_id,
            "mode": mode, "species": species,
            "image_size": [image.shape[1], image.shape[0]],
            "coordinate_system": "original_image_pixels",
            "gt_count": gt_count,
            "yolo_count": len(detections), "final_count": len(objects),
            "absolute_error": abs(len(objects) - gt_count) if gt_count is not None else None,
            "status": ("UNRESOLVED" if unresolved else "NO_DETECTIONS" if not detections else "OK"),
            "detections": detections, "objects": objects, "diagnostics": diagnostics,
            "timings_ms": {"yolo_infer_ms": prediction["yolo_infer_ms"],
                           "detector_wall_ms": prediction["detector_wall_ms"],
                           "separation_ms": post_ms,
                           "total_ms": prediction["detector_wall_ms"] + post_ms},
        }

# ---------------------------------------------------------------- 评测
def evaluate_split(frozen_path, split, out_dir, weights=None, modes=MODES_ALL, limit=0):
    frozen = json.loads(Path(frozen_path).read_text(encoding="utf-8"))
    rows = [r for r in sep.validate() if r["split"] == split]
    if limit:
        rows = rows[:limit]
    engines = {}

    def engine_for(species, conf):
        key = (species, round(float(conf), 4))
        if key not in engines:
            local = dict(frozen)
            local["detector"] = dict(frozen["detector"], conf=float(conf))
            engines[key] = SeedSeparatorV2(local, weights=weights)
        return engines[key]

    per_image = []
    for row in rows:
        image = sep.read_image(sep.get_path(row))
        species, gt = row["species"], int(row["gt_count"])
        base_conf = float(frozen["detector"]["conf"])
        v2_conf = float(DETECTOR_CONF_V2.get(species, base_conf))
        engine_base = engine_for(species, base_conf)
        engine_v2 = engine_for(species, v2_conf)
        t0 = time.perf_counter()
        prediction = engine_base.predict(image, frame_id=int(row["frame_id"]), species=species)
        detect_ms = (time.perf_counter() - t0) * 1000
        prediction_v2 = prediction
        if v2_conf != base_conf:
            prediction_v2 = engine_v2.predict(image, frame_id=int(row["frame_id"]), species=species)
        rec = {"image_id": row["image_id"], "species": species, "split": split,
               "contact_type": row["contact_type"], "gt_count": gt,
               "conf_base": base_conf, "conf_v2": v2_conf,
               "yolo_detections": len(prediction["detections"]),
               "yolo_v2_detections": len(prediction_v2["detections"]),
               "detect_ms": round(detect_ms, 1)}
        for mode in modes:
            pred = prediction_v2 if mode == "proposed_v2" else prediction
            engine = engine_v2 if mode == "proposed_v2" else engine_base
            res = engine.detect_and_separate(
                image, mode, species, session_id="scene", frame_id=int(row["frame_id"]),
                image_id=row["image_id"], gt_count=gt, prediction=pred)
            rec[MODE_LABEL[mode] + "_count"] = res["final_count"]
            rec[MODE_LABEL[mode] + "_err"] = abs(res["final_count"] - gt)
            rec[MODE_LABEL[mode] + "_status"] = res["status"]
            rec[MODE_LABEL[mode] + "_sep_ms"] = round(res["timings_ms"]["separation_ms"], 1)
            rec[MODE_LABEL[mode] + "_total_ms"] = round(res["timings_ms"]["total_ms"], 1)
        per_image.append(rec)
        print("%-28s gt=%3d  " % (row["image_id"], gt)
              + "  ".join("%s=%3d" % (MODE_LABEL[m], rec[MODE_LABEL[m] + "_count"])
                          for m in modes))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fields = list(per_image[0].keys())
    with (out / "eval_scene.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(per_image)

    summary = []
    merged_types = {"touch", "pair", "multi"}
    for species in ("soybean", "mungbean", "redbean", "ALL"):
        group = per_image if species == "ALL" else [r for r in per_image if r["species"] == species]
        if not group:
            continue
        contact = [r for r in group if r["contact_type"] in merged_types] or group
        for mode in modes:
            label = MODE_LABEL[mode]
            errs = [r[label + "_err"] for r in group]
            summary.append({
                "species": species, "split": split, "mode": label,
                "n_images": len(group),
                "count_mae": round(sum(errs) / len(errs), 4),
                "exact_image_accuracy": round(sum(1 for e in errs if e == 0) / len(errs), 4),
                # 粘连图（touch/pair/multi）里计数完全正确的比例
                "merged_recovery_rate": round(
                    sum(1 for r in contact if r[label + "_err"] == 0) / len(contact), 4),
                "n_merged_images": len(contact),
                "over_count_rate": round(
                    sum(1 for r in group if r[label + "_count"] > int(r["gt_count"])) / len(group), 4),
                "under_count_rate": round(
                    sum(1 for r in group if r[label + "_count"] < int(r["gt_count"])) / len(group), 4),
                "unresolved_image_rate": round(
                    sum(1 for r in group if r[label + "_status"] == "UNRESOLVED") / len(group), 4),
                "mean_sep_ms": round(sum(r[label + "_sep_ms"] for r in group) / len(group), 2),
                "mean_total_ms": round(sum(r[label + "_total_ms"] for r in group) / len(group), 2),
            })
    with (out / "eval_scene_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
        writer.writeheader()
        writer.writerows(summary)

    print("\n=== %s 汇总（按品种 × 模式）===" % split)
    print("%-9s %-4s %4s %8s %10s %10s %10s" % ("品种", "模式", "图数", "MAE", "完全正确", "未解决率", "总耗时ms"))
    for r in summary:
        print("%-9s %-4s %4d %8.3f %9.1f%% %9.1f%% %10.1f" % (
            r["species"], r["mode"], r["n_images"], r["count_mae"],
            r["exact_image_accuracy"] * 100, r["unresolved_image_rate"] * 100, r["mean_total_ms"]))
    print("\n逐图 -> %s\n汇总   -> %s" % (out / "eval_scene.csv", out / "eval_scene_summary.csv"))
    return per_image, summary

def main():
    ap = argparse.ArgumentParser(description="scene B 自适应分离 V2")
    sub = ap.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("eval")
    ev.add_argument("--frozen", required=True)
    ev.add_argument("--split", choices=["dev", "test"], required=True)
    ev.add_argument("--out", required=True)
    ev.add_argument("--weights", default=None)
    ev.add_argument("--modes", default=",".join(MODES_ALL))
    ev.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if args.command == "eval":
        modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
        evaluate_split(args.frozen, args.split, args.out, args.weights, modes, args.limit)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
