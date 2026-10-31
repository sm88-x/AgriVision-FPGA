#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""segmentation 三档分级规则解释器。

规则来自 `data/config/grading_rules_v1.json`。分级只读取产出方提供的单粒形态字段，
不按 GT 反推，也不修改原始 JSON。
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
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_RULES = ROOT / "data/config/grading_rules_v1.json"

def load_rules(path=DEFAULT_RULES):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def grade_object(obj, species, rules):
    """按面积三分位返回 A/B/C；非 OK 实例保留原因并标为不确定。"""
    status = str(obj.get("status", ""))
    area = obj.get("area_px")
    rule = rules.get("species", {}).get(species)
    if rule is None:
        return {
            "grade": "C",
            "grade_reason": "unknown_species",
            "uncertain": True,
            "status": status,
        }
    if area is None:
        return {
            "grade": "C",
            "grade_reason": "missing_area",
            "uncertain": True,
            "status": status,
        }
    area = float(area)
    if area >= float(rule["A_min_area_px"]):
        grade = "A"
    elif area >= float(rule["B_min_area_px"]):
        grade = "B"
    else:
        grade = "C"
    return {
        "grade": grade,
        "grade_reason": "area_tertile",
        "uncertain": status != "OK",
        "status": status,
    }

def _flatten(document, source):
    rows = []
    image_id = document.get("image_id") or Path(source).stem
    species = document.get("species")
    frames = document.get("frames")
    groups = frames if isinstance(frames, list) else [document]
    for frame in groups:
        frame_species = frame.get("species") or species or ""
        frame_id = frame.get("frame_id", document.get("frame_id", ""))
        frame_image = frame.get("image_id") or image_id
        objects = frame.get("objects")
        if not isinstance(objects, list):
            continue
        for obj in objects:
            rows.append({
                "source": str(source),
                "image_id": frame_image,
                "frame_id": frame_id,
                "species": frame_species,
                "object_id": obj.get("id", ""),
                "seed_id": obj.get("seed_id", ""),
                "status": obj.get("status", ""),
                "area_px": obj.get("area_px", ""),
                "length_px": obj.get("length_px", ""),
                "width_px": obj.get("width_px", ""),
                "circularity": obj.get("circularity", ""),
                "concavity": obj.get("concavity", ""),
            })
    return rows

def read_objects(path, rules, pattern=None):
    p = Path(path)
    paths = sorted(p.rglob(pattern or "*.json")) if p.is_dir() else [p]
    rows = []
    for source in paths:
        try:
            doc = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        for row in _flatten(doc, source):
            row.update(grade_object(row, row["species"], rules))
            rows.append(row)
    return rows

def main():
    ap = argparse.ArgumentParser(description="对单粒 JSON 运行 segmentation 三档分级")
    ap.add_argument("--input", required=True, help="单粒结果 JSON 或目录")
    ap.add_argument("--rules", default=str(DEFAULT_RULES))
    ap.add_argument("--pattern", default="*.json",
                    help="目录输入时使用的文件名模式")
    ap.add_argument("--out", help="输出 CSV；省略时打印前20行")
    args = ap.parse_args()
    rules = load_rules(args.rules)
    rows = read_objects(args.input, rules, args.pattern)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                                    ["source", "species", "grade"])
            writer.writeheader()
            writer.writerows(rows)
        print("graded %d objects -> %s" % (len(rows), out))
    else:
        for row in rows[:20]:
            print(row)
    return 0 if rows else 2

if __name__ == "__main__":
    raise SystemExit(main())
