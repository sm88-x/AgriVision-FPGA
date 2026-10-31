#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""segmentation 批量分级报告：逐粒 CSV + 按品种/等级汇总 + 审计样本。"""
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
import time
from collections import Counter, defaultdict
from pathlib import Path

from grading import DEFAULT_RULES, load_rules, read_objects

def write_csv(path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0].keys()) if rows else ["source", "species", "grade"]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def main():
    ap = argparse.ArgumentParser(description="segmentation 三档分级批量报告")
    ap.add_argument("--input", required=True, help="结果 JSON 文件或目录")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--rules", default=str(DEFAULT_RULES))
    ap.add_argument("--pattern", default="proposed_v1.json",
                    help="目录输入时只读取指定名称的结果，避免 baseline 重复计数")
    ap.add_argument("--audit-out", default="")
    ap.add_argument("--audit-per-species", type=int, default=10)
    args = ap.parse_args()

    started = time.perf_counter()
    rules = load_rules(args.rules)
    rows = read_objects(args.input, rules, args.pattern)
    elapsed = time.perf_counter() - started
    if not rows:
        raise SystemExit("没有找到可分级对象")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "objects_graded.csv", rows)

    by_species = defaultdict(list)
    for row in rows:
        by_species[row["species"]].append(row)
    summary_rows = []
    for species in sorted(by_species):
        group = by_species[species]
        counts = Counter(row["grade"] for row in group)
        summary_rows.append({
            "species": species,
            "objects": len(group),
            "grade_A": counts["A"],
            "grade_B": counts["B"],
            "grade_C": counts["C"],
            "uncertain": sum(bool(row["uncertain"]) for row in group),
            "A_ratio": round(counts["A"] / len(group), 4),
            "B_ratio": round(counts["B"] / len(group), 4),
            "C_ratio": round(counts["C"] / len(group), 4),
        })
    write_csv(out_dir / "summary.csv", summary_rows)

    audit = []
    for species in sorted(by_species):
        picked = [r for r in by_species[species] if r["status"] == "OK"
                  and r["area_px"] != ""][:args.audit_per_species]
        audit.extend(picked)
    if args.audit_out:
        write_csv(args.audit_out, audit)

    report = {
        "schema": "agrivision.segmentation.batch_report.v1",
        "rules": str(Path(args.rules).resolve()),
        "rule_status": rules.get("status"),
        "requires_task_owner_review": rules.get("requires_task_owner_review"),
        "input": str(Path(args.input).resolve()),
        "objects": len(rows),
        "species": {k: len(v) for k, v in sorted(by_species.items())},
        "grades": dict(Counter(row["grade"] for row in rows)),
        "uncertain": sum(bool(row["uncertain"]) for row in rows),
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "objects_per_second": round(len(rows) / max(elapsed, 1e-9), 1),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
