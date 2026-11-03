#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""segmentation 现场运行时：同一输入生成 YOLO / 固定分水岭 / 自适应三路结果。

Qt 在实时模式会周期性把最新帧写成 PNG，再调用本脚本。脚本沿用
`data/evidence/segmentation_comparison/frozen.json` 的模型和先验，不修改指标，也不按 GT 反推数量。

示例：
    python -B src/app/comparison_runtime.py \
        --image work/latest.png --species soybean --out work/comparison.json \
        --pheno-out work/phenotyping.json
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src").is_dir())
for _layout_rel in ("src", "src/app", "src/adapters",
                    "src/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import argparse
import copy
import json
import uuid
import sys
from datetime import datetime
from pathlib import Path

import cv2

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src/algorithm"))
sys.path.insert(0, str(ROOT / "src/algorithm/adaptive"))

import adaptive_split_v2 as avatar                 # noqa: E402
import seed_separation as sep                      # noqa: E402

FROZEN = ROOT / "data/evidence/segmentation_comparison/frozen.json"
WEIGHTS = ROOT / "data/models/best.pt"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
PRIOR_ALIAS = {"other": "soybean", "peanut": "soybean", "pea": "mungbean"}

def load_frozen():
    return json.loads(FROZEN.read_text(encoding="utf-8"))

def iter_images(image, directory):
    if image:
        p = Path(image)
        if not p.is_file():
            raise FileNotFoundError(p)
        return [p]
    p = Path(directory)
    if not p.is_dir():
        raise NotADirectoryError(p)
    paths = sorted(q for q in p.rglob("*") if q.suffix.lower() in IMAGE_EXTS)
    if not paths:
        raise FileNotFoundError("目录中没有图片: %s" % p)
    return paths

def normalized_bgr(path):
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("图片读取失败: %s" % path)
    if image.shape[:2] != (480, 640):
        image = cv2.resize(image, (640, 480), interpolation=cv2.INTER_AREA)
    return image

def object_for_ui(obj):
    bbox = obj.get("bbox_xyxy_src") or obj.get("bbox") or []
    contour = obj.get("contour_xy_src") or obj.get("contour") or []
    parent = obj.get("source_detection_ids") or []
    return {
        "id": obj.get("id"),
        "seed_id": obj.get("seed_id", ""),
        "bbox": bbox,
        "contour": contour,
        "confidence": obj.get("confidence"),
        "status": obj.get("status", ""),
        "status_reason": obj.get("status_reason", ""),
        "area_px": obj.get("area_px"),
        "parent_yolo_id": parent[0] if parent else None,
    }

def mode_for_ui(result):
    return {
        "count": int(result["final_count"]),
        "abs_error": int(result["absolute_error"])
        if result.get("absolute_error") is not None else None,
        "objects": [object_for_ui(o) for o in result.get("objects", [])],
    }

def timing_for_ui(result):
    t = result.get("timings_ms", {})
    return {
        "yolo": t.get("yolo_infer_ms") or t.get("detector_wall_ms"),
        "fixed": t.get("separation_ms") if result.get("mode") == "baseline_1" else None,
        "adaptive": t.get("separation_ms") if result.get("mode") == "proposed_v2" else None,
        "total": t.get("total_ms"),
        "total_yolo": t.get("total_ms") if result.get("mode") == "baseline_0" else None,
        "total_fixed": t.get("total_ms") if result.get("mode") == "baseline_1" else None,
        "total_adaptive": t.get("total_ms") if result.get("mode") == "proposed_v2" else None,
        "fpga_dma": t.get("fpga_dma_ms"),
    }

def phenotyping_document(frame_id, image_path, species, seed_count, objects, detector):
    mapped = []
    for o in objects:
        item = object_for_ui(o)
        item.update({
            "bbox_xyxy_src": item["bbox"],
            "contour_xy_src": item["contour"],
            "length_px": o.get("length_px"),
            "width_px": o.get("width_px"),
            "perimeter_px": o.get("perimeter_px"),
            "aspect_ratio": o.get("aspect_ratio"),
            "circularity": o.get("circularity"),
        })
        mapped.append(item)
    frame = {
        "frame_id": frame_id,
        "image_id": image_path.stem,
        "image": str(image_path),
        "seed_count": seed_count,
        "species": species,
        "objects": mapped,
    }
    return {
        "schema": "agrivision.phenotyping.v1",
        "units": {"length": "px", "width": "px",
                  "perimeter": "px", "area": "px^2"},
        "detector": detector,
        "frames": [frame],
        "objects": mapped,
    }

def run(args):
    if not args.image and not args.dir:
        raise ValueError("必须提供 --image 或 --dir")
    if args.image and args.dir:
        raise ValueError("--image 与 --dir 不能同时使用")
    if not args.out and not args.pheno_out:
        raise ValueError("至少需要 --out 或 --pheno-out")

    if args.dir and args.gt is not None:
        raise ValueError("Batch 不能使用一个统一 GT")
    frozen = load_frozen()
    prior_species = PRIOR_ALIAS.get(args.species, args.species)
    if prior_species not in sep.SPECIES:
        raise ValueError("不支持的品种: %s" % args.species)

    base_conf = float(frozen["detector"]["conf"])
    v2_conf = float(avatar.DETECTOR_CONF_V2.get(prior_species, base_conf))
    engine_base = avatar.SeedSeparatorV2(copy.deepcopy(frozen), weights=WEIGHTS)
    if abs(v2_conf - base_conf) < 1e-9:
        engine_v2 = engine_base
    else:
        frozen_v2 = copy.deepcopy(frozen)
        frozen_v2["detector"]["conf"] = v2_conf
        engine_v2 = avatar.SeedSeparatorV2(frozen_v2, weights=WEIGHTS)

    images = iter_images(args.image, args.dir)
    if args.dir and any(p.name.startswith(("soybean_","mungbean_","redbean_")) for p in images):
        images = [p for p in images if p.name.startswith(prior_species+"_")]
        if not images:raise ValueError("目录中没有所选品种的图片")
    image_dir = Path(args.pheno_out or args.out).resolve().parent
    image_dir.mkdir(parents=True, exist_ok=True)
    session = uuid.uuid4().hex[:12]
    comparison_rows = []
    pheno_frames = []
    detector_info = {
        "name": "YOLO11n best.pt",
        "conf": base_conf,
        "adaptive_conf": v2_conf,
        "iou": float(frozen["detector"]["iou"]),
        "imgsz": int(frozen["detector"]["imgsz"]),
        "device": str(frozen["detector"]["device"]),
    }

    for index, path in enumerate(images):
        image = normalized_bgr(path)
        # Save the exact working image: coordinates must never be drawn on a guessed old file.
        work_path = image_dir / ("input_%s_%06d.png" % (session,index))
        if not cv2.imwrite(str(work_path), image):
            raise OSError("无法保存本次分析输入图像")
        prediction_base = engine_base.predict(image, frame_id=index, species=prior_species)
        prediction_v2 = (
            prediction_base if engine_v2 is engine_base
            else engine_v2.predict(image, frame_id=index, species=prior_species)
        )
        yolo = engine_base.detect_and_separate(
            image, "baseline_0", prior_species, session_id="live",
            frame_id=index, image_id=path.stem,
            gt_count=args.gt if args.image else None,
            prediction=prediction_base)
        fixed = engine_base.detect_and_separate(
            image, "baseline_1", prior_species, session_id="live",
            frame_id=index, image_id=path.stem,
            gt_count=args.gt if args.image else None,
            prediction=prediction_base)
        adaptive = engine_v2.detect_and_separate(
            image, "proposed_v2", prior_species, session_id="live",
            frame_id=index, image_id=path.stem,
            gt_count=args.gt if args.image else None,
            prediction=prediction_v2)

        modes = {
            "yolo": mode_for_ui(yolo),
            "fixed": mode_for_ui(fixed),
            "adaptive": mode_for_ui(adaptive),
        }
        timing = {
            "yolo": yolo["timings_ms"].get("yolo_infer_ms"),
            "fixed": fixed["timings_ms"].get("separation_ms"),
            "adaptive": adaptive["timings_ms"].get("separation_ms"),
            "total": adaptive["timings_ms"].get("total_ms"),
            "total_yolo": yolo["timings_ms"].get("total_ms"),
            "total_fixed": fixed["timings_ms"].get("total_ms"),
            "total_adaptive": adaptive["timings_ms"].get("total_ms"),
        }
        fail_note = ""
        if args.gt is not None and adaptive.get("absolute_error") not in (None, 0):
            fail_note = "adaptive_abs_error=%d" % adaptive["absolute_error"]
        is_fail = bool(fail_note) or adaptive.get("status") == "UNRESOLVED"
        comparison_rows.append({
            "image_id": path.stem,
            "file": str(work_path),
            "source_file": str(path),
            "scene": {"hardware":False,"simulated":False,"receipt_note":"本机算法路径，FPGA 未参与"},
            "species": args.species,
            "contact_type": "",
            "split": "live",
            "notes": "runtime frame %d" % index,
            "fail_note": fail_note,
            "gt_source": "operator" if args.gt is not None else "",
            "status": "fail" if is_fail else "pass",
            "gt_count": args.gt if args.gt is not None else None,
            "timings_ms": timing,
            "modes": modes,
        })
        frame = phenotyping_document(
            index, work_path, args.species, adaptive["final_count"],
            adaptive.get("objects", []), detector_info)["frames"][0]
        frame.update({"image_file":str(work_path), "fpga_file":"", "source_file":str(path),
                      "width":image.shape[1], "height":image.shape[0],
                      "scene":{"hardware":False,"simulated":False,
                               "receipt_note":"本机算法路径，FPGA 未参与"}})
        pheno_frames.append(frame)
        print("%s: YOLO=%d fixed=%d adaptive=%d"
              % (path.name, yolo["final_count"], fixed["final_count"],
                 adaptive["final_count"]), flush=True)

    if args.out:
        doc = {
            "schema": "agrivision.segmentation.comparison.v1",
            "source": "live_runtime",
            "source_note": "同一输入帧运行 YOLO、固定分水岭与自适应分离；数字来自本次算法输出。",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "mode_labels": {
                "yolo": "YOLO 检测框",
                "fixed": "固定分水岭",
                "adaptive": "自适应分离",
            },
            "detector": detector_info,
            "chain_timings_ms": {},
            "images": comparison_rows,
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print("comparison -> %s" % out, flush=True)

    if args.pheno_out:
        ph = {
            "schema": "agrivision.phenotyping.v1",
            "units": {"length": "px", "width": "px",
                      "perimeter": "px", "area": "px^2"},
            "detector": detector_info,
            "frames": pheno_frames,
            "objects": [o for f in pheno_frames for o in f["objects"]],
        }
        out = Path(args.pheno_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(ph, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print("phenotyping -> %s" % out, flush=True)
    return 0

def main():
    ap = argparse.ArgumentParser(description="segmentation 现场三路粘连分离对比")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--image")
    src.add_argument("--dir")
    ap.add_argument("--species", required=True,
                    choices=["soybean", "mungbean", "redbean",
                             "other", "peanut", "pea"])
    ap.add_argument("--gt", type=int, default=None)
    ap.add_argument("--out")
    ap.add_argument("--pheno-out")
    ap.add_argument("--board", default=None,
                    help="预留参数；当前运行时未接入板端预处理")
    args = ap.parse_args()
    if args.board:
        raise SystemExit("当前运行时是 PC 算法链路，不接受 --board")
    return run(args)

if __name__ == "__main__":
    raise SystemExit(main())
