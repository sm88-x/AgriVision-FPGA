#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline_validation 板端验证（数据集批量）：12 张农业测试图 × 各 Pipeline 模式

与 pipeline_regression.py 的分工：
    pipeline_regression.py  —— 按**分辨率**逐级对拍（gray/gauss/edge/morph 各一级）
    本文件                 —— 按**图片**跑完整链，每张图 × 每种 Pipeline 模式各对拍一次，
                              用来回答"Seed/叶片数据集在真实完整流水线下是否逐像素一致"。

用法（板端，与 edge_morph_driver.py / run_pipeline.py / pipeline_regression.py 同目录）：
    sudo python3 dataset_regression.py --images edge_validation_images --out results/dataset_regression.csv
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
    SobelParamDriver, compare, MODE_SOBEL, MODE_PREWITT,
    MORPH_BYPASS, MORPH_DILATE, MORPH_NAMES,
)
from pipeline_regression import (                  # noqa: E402
    pack_rgb, golden_for, load_canvas, check_capabilities,
    OUT_MORPH, REG_ENABLE_GRAY, REG_ENABLE_GAUSS, REG_OUT_SEL, THRESHOLD,
)

W, H = 640, 480

# Pipeline 模式表：(模式名, 开高斯, 算法, out_sel, 形态学)
#   full        完整链：RGB2Gray → Gaussian → Edge → 输出
#   full_dilate 完整链 + 膨胀（演示用的降碎块配置）
#   nogauss     关高斯走完整链（验证旁路不回退）
#   prewitt     完整链换成 Prewitt
PIPELINE_MODES = [
    ("full",        1, MODE_SOBEL,   OUT_MORPH, MORPH_BYPASS),
    ("full_dilate", 1, MODE_SOBEL,   OUT_MORPH, MORPH_DILATE),
    ("nogauss",     0, MODE_SOBEL,   OUT_MORPH, MORPH_BYPASS),
    ("prewitt",     1, MODE_PREWITT, OUT_MORPH, MORPH_BYPASS),
]

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="edge_validation_images")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--repeats", type=int, default=20)
    ap.add_argument("--out", default="data/evidence/analysis/dataset_regression.csv")
    args = ap.parse_args(argv)

    driver = SobelParamDriver()
    info = driver.info()
    print("Overlay :", info["bitfile"], "| 核:", info["kernel_ip"])
    caps = check_capabilities(driver)
    print("功能：", {k: ("有" if v else "没有") for k, v in caps.items()})

    images = sorted(Path(args.images).glob("*.png"))
    if not images:
        print("找不到测试图：", args.images)
        return 1
    print("测试图 %d 张 × Pipeline 模式 %d 种 = %d 个用例"
          % (len(images), len(PIPELINE_MODES), len(images) * len(PIPELINE_MODES)))
    print("=" * 78)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for img_path in images:
        rgb = load_canvas(img_path, (W, H))
        payload = pack_rgb(rgb)
        for name, gauss, mode, out_sel, morph_mode in PIPELINE_MODES:
            mode_name = "Sobel" if mode == MODE_SOBEL else "Prewitt"
            reference = golden_for(rgb, mode, THRESHOLD, gauss, out_sel, morph_mode)

            from pynq import allocate
            inp = allocate(shape=(int(payload.size),), dtype=np.uint8)
            out = allocate(shape=(W * H,), dtype=np.uint8)
            inp[:] = payload
            inp.flush()
            extra = {}
            if caps["enable_gray"]:
                extra[REG_ENABLE_GRAY] = 1
            if caps["enable_gauss"]:
                extra[REG_ENABLE_GAUSS] = 1 if gauss else 0
            if caps["out_sel"]:
                extra[REG_OUT_SEL] = out_sel

            try:
                driver.run_frame(inp, out, W, H, mode, THRESHOLD, 5.0, morph_mode, extra)
                out.invalidate()
                pl = np.array(out, copy=True).reshape(H, W)
                stats = compare(reference, pl)
                for _ in range(args.warmup):
                    driver.run_frame(inp, out, W, H, mode, THRESHOLD, 5.0, morph_mode, extra)
                samples = np.asarray([
                    driver.run_frame(inp, out, W, H, mode, THRESHOLD, 5.0, morph_mode, extra) * 1e3
                    for _ in range(args.repeats)
                ])
                nonzero = float(np.count_nonzero(pl)) * 100.0 / pl.size
                print("%-26s %-12s Golden %-4s 不同像素=%-6d 最大误差=%-3d 非零=%5.2f%%  中位 %.3f ms"
                      % (img_path.name, name, stats["verdict"], stats["different_pixels"],
                         stats["max_error"], nonzero, np.median(samples)))
                rows.append({
                    "image": img_path.name, "pipeline": name, "algorithm": mode_name,
                    "gauss": int(gauss), "morph_mode": MORPH_NAMES[morph_mode],
                    "golden": stats["verdict"], "different_pixels": stats["different_pixels"],
                    "max_diff": stats["max_error"],
                    "nonzero_pct": round(nonzero, 2),
                    "mean_ms": round(float(samples.mean()), 4),
                    "median_ms": round(float(np.median(samples)), 4),
                    "min_ms": round(float(samples.min()), 4),
                    "max_ms": round(float(samples.max()), 4),
                    "fps": round(1000.0 / float(np.median(samples)), 1),
                })
            except Exception as exc:                   # noqa: BLE001
                print("%-26s %-12s [失败] %s: %s"
                      % (img_path.name, name, type(exc).__name__, exc))
                rows.append({
                    "image": img_path.name, "pipeline": name, "algorithm": mode_name,
                    "gauss": int(gauss), "morph_mode": MORPH_NAMES[morph_mode],
                    "golden": "ERROR", "different_pixels": "", "max_diff": "",
                    "nonzero_pct": "", "mean_ms": "", "median_ms": "",
                    "min_ms": "", "max_ms": "", "fps": "",
                })
            finally:
                del inp, out

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    passed = sum(1 for r in rows if r["golden"] == "PASS")
    print("=" * 78)
    print("合计 %d 个用例：Golden PASS %d / %d" % (len(rows), passed, len(rows)))
    print("逐次记录 -> %s" % args.out)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
