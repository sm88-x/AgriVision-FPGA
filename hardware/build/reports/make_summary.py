# -*- coding: utf-8 -*-
"""把各次正式 Benchmark 的原始 CSV 汇总成一份 summary.csv

数据源（全部是仓库里已有的正式板测记录，不手工抄写任何数字）：
    board/results/board_benchmark/r1~r4_*.csv                    统一口径四代对比（推荐）
    hardware/build/reports/sobel_v0/sobel_benchmark.csv                   早期 V0 板测（256x256）
    hardware/build/reports/sobel_v3/sobel_v3_benchmark.csv                早期 V3 板测（256x256）
    board/results/sobel_v3_param_board/..._benchmark.csv         V3 参数化核板测（三尺寸 × 双算法）

两批数据的区别写在 `batch` 列：
    2026-10-05 统一口径  用 hardware/drivers/board_benchmark.py 同一套方法测四个后端，
                         可直接横向比较（图表只用这一批）
    2026-10-03 早期口径  各版本分别用当时的脚本测的，仅作历史记录

输出：
    hardware/build/reports/summary.csv

聚合口径（按 version / image / resolution / algorithm 分组）：
    PL E2E   = 一次完整 DMA 收发 + 核处理
    PS       = 软件算法本身（读图在计时外）
    FPS      = 1000 / 中位数（用中位数而不是均值，避免个别抖动放大结论）
    Speedup  = PS 中位数 / PL E2E 中位数

注意：不同批次的板测使用了不同的等待方式（早期用 PYNQ 自带 wait()，
V3 参数化核用 DMA 完成标志轮询），跨批次比较时务必看 summary.csv 的
`batch` 列，不要把不同口径的数字直接混算。
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import csv
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 本脚本位于 build/reports/ 下，仓库根在上两层
REPO = PROJECT_ROOT
OUT = REPO / "hardware/build/reports/summary.csv"

# 后端标识 → 版本名（summary.csv 的 version 列）
BACKEND_VERSION = {"ps": "ps", "v0": "v0", "v3": "v3", "v3p": "v3_param"}

FIELDS = [
    "batch", "version", "image", "resolution", "algorithm", "runs",
    "pl_mean_ms", "pl_median_ms", "pl_min_ms", "pl_max_ms", "pl_fps_median",
    "ps_mean_ms", "ps_median_ms", "ps_fps_median",
    "speedup_ps_over_pl", "correct", "source",
]

def _stats(values):
    ms = np.asarray([v for v in values if v is not None and v != ""], np.float64)
    if ms.size == 0:
        return dict(n=0, mean=None, median=None, mn=None, mx=None, fps=None)
    med = float(np.median(ms))
    return dict(n=int(ms.size), mean=float(ms.mean()), median=med,
                mn=float(ms.min()), mx=float(ms.max()),
                fps=(1000.0 / med) if med > 0 else None)

def _round(v, nd=4):
    return None if v is None else round(v, nd)

def read_unified(path, batch):
    """统一口径板测：hardware/drivers/board_benchmark.py 的输出。

    列：backend,label,image,resolution,algorithm,threshold,run_id,ms,fps,...
    其中 ps 后端是软件计时，其余是 PL E2E，两者口径已经在脚本里统一。
    """
    groups = OrderedDict()
    for row in csv.DictReader(path.open(encoding="utf-8")):
        backend = row["backend"]
        key = (backend, row["image"], row["resolution"], row["algorithm"])
        g = groups.setdefault(key, {"pl": [], "ps": [], "correct": set(),
                                    "source": row.get("source", "-")})
        if backend == "ps":
            g["ps"].append(row.get("ms"))
        else:
            g["pl"].append(row.get("ms"))
        g["correct"].add(row.get("correct", ""))

    out = []
    for (backend, image, resolution, algo), g in groups.items():
        pl, ps = _stats(g["pl"]), _stats(g["ps"])
        main = pl if pl["median"] is not None else ps      # 软件后端只有 ps
        speed = (ps["median"] / pl["median"]) if (ps["median"] and pl["median"]) else None
        out.append({
            "batch": batch, "version": BACKEND_VERSION.get(backend, backend),
            "image": image, "resolution": resolution, "algorithm": algo,
            "runs": main["n"],
            "pl_mean_ms": _round(pl["mean"]), "pl_median_ms": _round(pl["median"]),
            "pl_min_ms": _round(pl["mn"]), "pl_max_ms": _round(pl["mx"]),
            "pl_fps_median": _round(pl["fps"], 2),
            "ps_mean_ms": _round(ps["mean"]), "ps_median_ms": _round(ps["median"]),
            "ps_fps_median": _round(ps["fps"], 2),
            "speedup_ps_over_pl": _round(speed, 3),
            "correct": "PASS" if g["correct"] == {"PASS"} else "/".join(sorted(g["correct"])),
            "source": str(path.relative_to(REPO)).replace("\\", "/"),
        })
    return out

def read_v0_v3(path, batch):
    """早期 V0 / V3 的 CSV：带 ps_ms / pl_e2e_ms 两套时间。"""
    groups = OrderedDict()
    for row in csv.DictReader(path.open(encoding="utf-8")):
        key = (row["version"], row["image"], row["resolution"], "sobel")
        groups.setdefault(key, {"pl": [], "ps": [], "correct": set()})
        groups[key]["pl"].append(row.get("pl_e2e_ms"))
        groups[key]["ps"].append(row.get("ps_ms"))
        groups[key]["correct"].add(row.get("correct", ""))

    out = []
    for (version, image, resolution, algo), g in groups.items():
        pl, ps = _stats(g["pl"]), _stats(g["ps"])
        speed = (ps["median"] / pl["median"]) if (ps["median"] and pl["median"]) else None
        out.append({
            "batch": batch, "version": version, "image": image,
            "resolution": resolution, "algorithm": algo, "runs": pl["n"],
            "pl_mean_ms": _round(pl["mean"]), "pl_median_ms": _round(pl["median"]),
            "pl_min_ms": _round(pl["mn"]), "pl_max_ms": _round(pl["mx"]),
            "pl_fps_median": _round(pl["fps"], 2),
            "ps_mean_ms": _round(ps["mean"]), "ps_median_ms": _round(ps["median"]),
            "ps_fps_median": _round(ps["fps"], 2),
            "speedup_ps_over_pl": _round(speed, 3),
            "correct": "PASS" if g["correct"] == {"PASS"} else "/".join(sorted(g["correct"])),
            "source": str(path.relative_to(REPO)).replace("\\", "/"),
        })
    return out

def read_v3_param(path, batch):
    """V3 参数化核的板测 CSV：只有 PL 侧时间，输入图名见报告第 2 节。"""
    # 用例 → 输入图（与 data/evidence/board/sobel_v3_param_board/README.md 第 2 节一致）
    case_image = {
        "T1": "plant_a_256x256", "T2": "plant_a_256x256",
        "T3": "plant_b_320x240", "T4": "plant_b_320x240",
        "T5": "plant_a_640x480", "T6": "plant_a_640x480",
    }
    groups = OrderedDict()
    for row in csv.DictReader(path.open(encoding="utf-8")):
        case = row["case"]
        key = (case, case_image.get(case, "?"), row["size"], row["mode"].lower())
        groups.setdefault(key, {"pl": [], "correct": set()})
        groups[key]["pl"].append(row.get("ms"))
        groups[key]["correct"].add(row.get("correct", ""))

    out = []
    for (case, image, resolution, algo), g in groups.items():
        pl = _stats(g["pl"])
        out.append({
            "batch": batch, "version": "v3_param", "image": image,
            "resolution": resolution, "algorithm": algo, "runs": pl["n"],
            "pl_mean_ms": _round(pl["mean"]), "pl_median_ms": _round(pl["median"]),
            "pl_min_ms": _round(pl["mn"]), "pl_max_ms": _round(pl["mx"]),
            "pl_fps_median": _round(pl["fps"], 2),
            "ps_mean_ms": "", "ps_median_ms": "", "ps_fps_median": "",
            "speedup_ps_over_pl": "",
            "correct": "PASS" if g["correct"] == {"PASS"} else "/".join(sorted(g["correct"])),
            "source": str(path.relative_to(REPO)).replace("\\", "/"),
        })
    return out

def main():
    rows = []
    jobs = [
        # 统一口径（推荐，图表只用这一批）
        (REPO / "data/evidence/board/board_benchmark/r1_256_sobel.csv", "2026-10-05 统一口径", read_unified),
        (REPO / "data/evidence/board/board_benchmark/r2_256_prewitt.csv", "2026-10-05 统一口径", read_unified),
        (REPO / "data/evidence/board/board_benchmark/r3_320_sobel.csv", "2026-10-05 统一口径", read_unified),
        (REPO / "data/evidence/board/board_benchmark/r4_640_sobel.csv", "2026-10-05 统一口径", read_unified),
        (REPO / "data/evidence/board/board_benchmark/r5_320_prewitt.csv", "2026-10-05 统一口径", read_unified),
        (REPO / "data/evidence/board/board_benchmark/r6_640_prewitt.csv", "2026-10-05 统一口径", read_unified),
        # 早期口径（仅作历史记录）
        (REPO / "hardware/build/reports/sobel_v0/sobel_benchmark.csv", "2026-10-03 早期口径", read_v0_v3),
        (REPO / "hardware/build/reports/sobel_v3/sobel_v3_benchmark.csv", "2026-10-03 早期口径", read_v0_v3),
        (REPO / "data/evidence/board/sobel_v3_param_board/sobel_v3_param_board_benchmark.csv",
         "2026-10-04 参数化核首轮", read_v3_param),
    ]
    for path, batch, reader in jobs:
        if not path.exists():
            print(f"[跳过] 找不到 {path}")
            continue
        part = reader(path, batch)
        rows.extend(part)
        print(f"[读入] {path.name}: {len(part)} 组")

    rows.sort(key=lambda r: (r["version"], r["resolution"], r["algorithm"], r["image"]))
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n[写出] {OUT}  （{len(rows)} 组）")

    # 控制台预览
    head = f"{'版本':<10}{'尺寸':<10}{'算法':<9}{'组数':>5}{'PL中位ms':>10}{'FPS':>9}{'PS中位ms':>10}{'加速比':>9}"
    print("\n" + head)
    print("-" * len(head))

    def fmt(v, nd=3):
        return "-" if v in ("", None) else f"{v:.{nd}f}"

    for r in rows:
        print(f"{r['version']:<10}{r['resolution']:<10}{r['algorithm']:<9}{r['runs']:>5}"
              f"{fmt(r['pl_median_ms']):>10}{fmt(r['pl_fps_median'], 1):>9}"
              f"{fmt(r['ps_median_ms'], 2):>10}{fmt(r['speedup_ps_over_pl'], 2):>9}")

if __name__ == "__main__":
    main()
