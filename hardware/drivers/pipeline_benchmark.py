#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline_validation 任务书第 6 项：V3 与 V4 同一口径的性能对比（板端采集）

同一张图、同一档分辨率、同一个 warm-up/重复次数，分别压 V3 和 V4 两种 bit。
两次运行各写一份 CSV，最后在电脑上合并成 benchmark/pipeline_validation_raw.csv。

用法（板端）：
    sudo python3 pipeline_benchmark.py --tag v3 --bit parameterized_edge.bit \
        --input gray8  --out results/bench_v3.csv
    sudo python3 pipeline_benchmark.py --tag v4 --bit configurable_pipeline.bit \
        --input rgbx32 --out results/bench_v4.csv

口径说明（写进 CSV 的 note 列，避免以后误读数字）：
    V3 输入 8bit/像素灰度（1 B/px），只有 Edge→Threshold；
    V4 输入 32bit/像素 RGBX888（4 B/px），可选 RGB2Gray / Gaussian / Morphology。
    两边 DMA 搬运量本就不同，所以"V3 vs V4"比的是**整条链的端到端延迟**，
    不是"同一个模块谁更快"。
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

import numpy as np

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "src/software/app"))

from edge_morph_driver import (                     # noqa: E402
    SobelParamDriver, compare, edge_reference, MODE_SOBEL, MODE_PREWITT,
    MORPH_BYPASS, MORPH_DILATE, MORPH_NAMES,
)
from pipeline_regression import (                  # noqa: E402
    pack_rgb, golden_for, load_canvas, check_capabilities,
    OUT_MORPH, REG_ENABLE_GRAY, REG_ENABLE_GAUSS, REG_OUT_SEL, THRESHOLD,
)
from run_pipeline import rgb2gray                  # noqa: E402

SIZES = [(256, 256), (320, 240), (640, 480)]
BASE_IMAGE = "edge_validation_images/leaf_01_dense_640x480.png"

def v4_configs(caps):
    """V4 要跑的几档配置：(名字, 开高斯, out_sel, 形态学)。"""
    out = [("edge", 0, OUT_MORPH, MORPH_BYPASS)]          # 与 V3 最接近的可比配置
    if caps["enable_gauss"]:
        out.append(("full", 1, OUT_MORPH, MORPH_BYPASS))   # pipeline_validation 完整链
        out.append(("full_dilate", 1, OUT_MORPH, MORPH_DILATE))
    return out

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True, help="要压的 bitfile")
    ap.add_argument("--tag", required=True, help="这一轮的名字（v3 / v4）")
    ap.add_argument("--input", choices=("gray8", "rgbx32"), required=True)
    ap.add_argument("--image", default=BASE_IMAGE)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--repeats", type=int, default=30)
    ap.add_argument("--out", default="data/evidence/analysis/bench.csv")
    args = ap.parse_args(argv)

    driver = SobelParamDriver(bitfile=args.bit)
    info = driver.info()
    caps = check_capabilities(driver)
    print("=" * 78)
    print("tag=%s  bit=%s  核=%s" % (args.tag, Path(args.bit).name, info["kernel_ip"]))
    print("寄存器表：", {k: hex(v) for k, v in sorted(driver.regs.items(), key=lambda kv: kv[1])})
    print("功能    ：", {k: ("有" if v else "没有") for k, v in caps.items()})
    print("=" * 78)

    is_v4 = args.input == "rgbx32"
    plan = v4_configs(caps) if is_v4 else [("edge", 0, OUT_MORPH, MORPH_BYPASS)]
    from pynq import allocate

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for (w, h) in SIZES:
        rgb = load_canvas(args.image, (w, h))
        gray = rgb2gray(rgb)
        for mode in (MODE_SOBEL, MODE_PREWITT):
            mode_name = "Sobel" if mode == MODE_SOBEL else "Prewitt"
            for name, gauss, out_sel, morph_mode in plan:
                if is_v4:
                    payload = pack_rgb(rgb)
                    reference = golden_for(rgb, mode, THRESHOLD, gauss, out_sel, morph_mode)
                    extra = {}
                    if caps["enable_gray"]:
                        extra[REG_ENABLE_GRAY] = 1
                    if caps["enable_gauss"]:
                        extra[REG_ENABLE_GAUSS] = 1 if gauss else 0
                    if caps["out_sel"]:
                        extra[REG_OUT_SEL] = out_sel
                else:
                    payload = np.ascontiguousarray(gray).reshape(-1)
                    reference = edge_reference(gray, mode, THRESHOLD)
                    extra = {}

                inp = allocate(shape=(int(payload.size),), dtype=np.uint8)
                out = allocate(shape=(w * h,), dtype=np.uint8)
                inp[:] = payload
                inp.flush()
                try:
                    driver.run_frame(inp, out, w, h, mode, THRESHOLD, 5.0, morph_mode, extra)
                    out.invalidate()
                    pl = np.array(out, copy=True).reshape(h, w)
                    stats = compare(reference, pl)
                    for _ in range(args.warmup):
                        driver.run_frame(inp, out, w, h, mode, THRESHOLD, 5.0, morph_mode, extra)
                    samples = np.asarray([
                        driver.run_frame(inp, out, w, h, mode, THRESHOLD, 5.0, morph_mode, extra) * 1e3
                        for _ in range(args.repeats)
                    ])
                    print("%-9s %4dx%-4d %-7s %-12s Golden %-4s 中位 %7.3f ms  mean %7.3f  FPS %6.1f"
                          % (args.tag, w, h, mode_name, name, stats["verdict"],
                             np.median(samples), samples.mean(), 1000.0 / np.median(samples)))
                    rows.append({
                        "tag": args.tag, "bit": Path(args.bit).name, "kernel_ip": info["kernel_ip"],
                        "width": w, "height": h, "algorithm": mode_name, "config": name,
                        "input_bytes": int(payload.size), "gauss": int(gauss),
                        "morph_mode": MORPH_NAMES[morph_mode], "golden": stats["verdict"],
                        "different_pixels": stats["different_pixels"], "max_diff": stats["max_error"],
                        "warmup": args.warmup, "repeats": args.repeats,
                        "mean_ms": round(float(samples.mean()), 4),
                        "median_ms": round(float(np.median(samples)), 4),
                        "min_ms": round(float(samples.min()), 4),
                        "max_ms": round(float(samples.max()), 4),
                        "fps": round(1000.0 / float(np.median(samples)), 1),
                    })
                except Exception as exc:               # noqa: BLE001
                    print("%-9s %4dx%-4d %-7s %-12s [失败] %s: %s"
                          % (args.tag, w, h, mode_name, name, type(exc).__name__, exc))
                    rows.append({
                        "tag": args.tag, "bit": Path(args.bit).name, "kernel_ip": info["kernel_ip"],
                        "width": w, "height": h, "algorithm": mode_name, "config": name,
                        "input_bytes": int(payload.size), "gauss": int(gauss),
                        "morph_mode": MORPH_NAMES[morph_mode], "golden": "ERROR",
                        "different_pixels": "", "max_diff": "", "warmup": args.warmup,
                        "repeats": args.repeats, "mean_ms": "", "median_ms": "",
                        "min_ms": "", "max_ms": "", "fps": "",
                    })
                finally:
                    del inp, out

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("本轮记录 -> %s（%d 行）" % (args.out, len(rows)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
