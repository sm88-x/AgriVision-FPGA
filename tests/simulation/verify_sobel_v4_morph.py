#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""PC 校验：板测脚本形态学参考实现的逐像素一致性验证。

把 board/overlay/edge_morphology_system_board_test.py 的 morph_reference

（numpy 切片窗口实现）与独立显式循环实现（与 HLS tb 的 golden

apply_morph_to_golden 同语义：窗口 tval[r+dr, c+dc]，dr/dc ∈ -1..1，

外圈 0）逐像素对照。覆盖 tb 同款 38 组图型 + 板测公式图。

"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import importlib.util
from pathlib import Path

import numpy as np

BOARD = PROJECT_ROOT / "hardware/drivers/edge_morph_driver.py"

spec = importlib.util.spec_from_file_location("network_board", BOARD)

m = importlib.util.module_from_spec(spec)

spec.loader.exec_module(m)

def make_pattern(w, h, kind, seed):
    yy, xx = np.mgrid[:h, :w]
    if kind == 0: return np.zeros((h, w), np.uint8)
    if kind == 1: return np.full((h, w), 255, np.uint8)
    if kind == 2: return ((xx * 7 + yy * 13) & 255).astype(np.uint8)
    if kind == 3: return (((xx // 8 + yy // 8) & 1) * 255).astype(np.uint8)
    if kind == 4: return np.random.default_rng(seed).integers(0, 256, (h, w), dtype=np.uint8)
    if kind == 5: return ((xx == 0) | (yy == 0) | (xx == w-1) | (yy == h-1)).astype(np.uint8) * 255
    return ((xx * 17 + yy * 31) & 255).astype(np.uint8)

fail = 0

def morph_loop_ref(gray, mode, threshold, morph):

    """独立实现：显式双层循环，取以 (r,c) 为中心的 3×3 窗口 min/max。"""

    tval = m.edge_reference(gray, mode, threshold).astype(np.int32)

    if morph == m.MORPH_BYPASS:

        return tval.astype(np.uint8)

    h, w = tval.shape

    out = np.zeros((h, w), np.uint8)

    for r in range(1, h - 1):

        for c in range(1, w - 1):

            vals = [tval[r + dr, c + dc] for dr in (-1, 0, 1) for dc in (-1, 0, 1)]

            out[r, c] = min(vals) if morph == m.MORPH_ERODE else max(vals)

    return out

def check(gray, mode, thr, morph, name):

    global fail

    a = m.morph_reference(m.edge_reference(gray, mode, thr), morph)

    b = morph_loop_ref(gray, mode, thr, morph)

    d = int(np.count_nonzero(a != b))

    if d:

        fail += 1

    print(f"{name:28s} diff={d:6d}  {'PASS' if d == 0 else 'FAIL'}")

    return d

print("== tb 图型 38 组（独立生成，非原始缺失脚本的逐字节图型）==")

for p in range(6):

    for (w, h) in ((256, 256), (320, 240), (640, 480)):

        img = make_pattern(w, h, p, 4000 + p)

        for mo in (m.MORPH_ERODE, m.MORPH_DILATE):

            check(img, m.MODE_SOBEL, 80, mo,

                  f"P{p}_{w}x{h}_{'E' if mo == 1 else 'D'}")

for mo in (m.MORPH_ERODE, m.MORPH_DILATE):

    img = make_pattern(320, 240, 6, 5000 + mo)

    check(img, m.MODE_SOBEL, 0, mo, f"P6_320x240_{'E' if mo == 1 else 'D'}")

print("\n== 公式图（板测 T4~T6 同源）==")

yy, xx = np.mgrid[0:240, 0:320].astype(np.int64)

formula = ((xx * 7 + yy * 13 + (xx // 16) * 29 + (yy // 16) * 53)

           & 0xFF).astype(np.uint8)

check(formula, m.MODE_SOBEL, 80, m.MORPH_ERODE, "formula_320x240_E")

check(formula, m.MODE_SOBEL, 80, m.MORPH_DILATE, "formula_320x240_D")

print(f"\n总结论：{'ALL PASS' if fail == 0 else f'{fail} FAIL'}")

raise SystemExit(0 if fail == 0 else 1)

