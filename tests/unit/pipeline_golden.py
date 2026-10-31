# -*- coding: utf-8 -*-
"""pipeline_validation / B1：测试集构建 + 逐阶段 Golden 验证（不占板卡）

产出
    tests/unit/pipeline_golden_cases.csv   测试用例清单（类别/尺寸/各级统计/不变量是否通过）
    evidence/diff/<case>/*.png    逐级中间结果（输入 / 灰度 / 高斯 / 边缘 / 阈值 / 形态学）

测试集按任务书要求覆盖：单色、彩条、随机、几何、种子≥5张；三档 256x256 / 320x240 / 640x480。

不变量（Golden 自身的正确性证据，不是"跑通就算对"）
    1. 纯色输入：灰度输出必须是常数，且等于定点公式算出的值
    2. 所有窗口型算子（高斯/边缘/形态学）最外一圈必须全 0
    3. 灰度级没有窗口，最外圈保留原值（不受 zero-border 规则影响）
    4. 阈值输出只能是 0 或 255
    5. Erode 后前景像素数 <= 阈值图 <= Dilate 后前景像素数
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
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src/software/app"))
from run_pipeline import (                                   # noqa: E402
    SEED_PRESET, MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE,
    rgb2gray, gaussian3x3, edge_filter, morph, pipeline_stages,
)

# 本脚本放在仓库的 tests/ 下，上一级就是仓库根；不写死任何绝对路径
REPO = PROJECT_ROOT
if not (REPO / "data/fixtures/agricultural_images").is_dir():
    raise SystemExit("找不到 data/images/edge_validation，请在仓库的 tests/ 目录下运行（当前推断的仓库根：%s）" % REPO)
SIZES = [(256, 256), (320, 240), (640, 480)]
OUT_CSV = HERE / "pipeline_golden_cases.csv"
EVIDENCE = PROJECT_ROOT / "outputs/tests/golden_diff"

# ---------------------------------------------------------------- 测试集构造
def make_solid(w, h, rgb):
    return np.tile(np.array(rgb, np.uint8), (h, w, 1))

def make_colorbars(w, h):
    bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
            (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    x = (np.arange(w) * len(bars) // w).clip(0, len(bars) - 1)
    return np.tile(np.array(bars, np.uint8)[x], (h, 1, 1))

def make_random(w, h, seed=20261009):
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)

def make_checker(w, h, cell=16):
    yy, xx = np.mgrid[0:h, 0:w]
    mask = ((yy // cell + xx // cell) % 2).astype(np.uint8)
    return np.repeat((mask * 255)[:, :, None], 3, axis=2)

def make_border_dots(w, h):
    """边界亮点：专门卡"最外圈强制 0"这条规则。"""
    img = np.zeros((h, w, 3), np.uint8)
    for y, x in [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1), (0, w // 2), (h - 1, w // 2)]:
        img[y, x] = 255
    return img

def build_cases():
    cases = []
    for name, rgb in [("solid_red", (255, 0, 0)), ("solid_green", (0, 255, 0)),
                      ("solid_blue", (0, 0, 255)), ("solid_white", (255, 255, 255)),
                      ("solid_black", (0, 0, 0))]:
        w, h = SIZES[-1]
        cases.append((name, "单色", (w, h), make_solid(w, h, rgb)))
    for w, h in SIZES:
        cases.append(("colorbars_%dx%d" % (w, h), "彩条", (w, h), make_colorbars(w, h)))
        cases.append(("random_%dx%d" % (w, h), "随机", (w, h), make_random(w, h)))
        cases.append(("checker_%dx%d" % (w, h), "几何", (w, h), make_checker(w, h)))
        cases.append(("borderdots_%dx%d" % (w, h), "几何/边界", (w, h), make_border_dots(w, h)))
    seeds = sorted((REPO / "data/fixtures/agricultural_images").glob("seed_*.png"))
    for p in seeds:
        for w, h in SIZES:
            im = Image.open(p).convert("RGB").resize((w, h), Image.BILINEAR)
            cases.append(("%s_%dx%d" % (p.stem, w, h), "种子", (w, h), np.asarray(im, np.uint8)))
    return cases

# ---------------------------------------------------------------- 不变量检查
def border_is_zero(img):
    return bool(img[0].max() == 0 and img[-1].max() == 0
                and img[:, 0].max() == 0 and img[:, -1].max() == 0)

def check_invariants(name, rgb, st):
    notes = []
    gray, gauss = st["gray"], st["gaussian"]
    thr, ero, dil = st["threshold"], morph(st["threshold"], MORPH_ERODE), morph(st["threshold"], MORPH_DILATE)

    if name.startswith("solid_"):
        uniq = np.unique(gray)
        notes.append(("纯色图灰度必须是常数", uniq.size == 1))
        notes.append(("灰度值符合定点公式",
                      int(uniq[0]) == int(rgb2gray(np.array([[rgb[0, 0]]], np.uint8))[0, 0])))
    notes.append(("高斯最外圈全 0", border_is_zero(gauss)))
    notes.append(("边缘最外圈全 0", border_is_zero(st["edge"])))
    notes.append(("阈值最外圈全 0", border_is_zero(thr)))
    notes.append(("阈值只含 0/255", set(np.unique(thr)).issubset({0, 255})))
    n_thr, n_ero, n_dil = (int(np.count_nonzero(x)) for x in (thr, ero, dil))
    notes.append(("腐蚀<=阈值<=膨胀", n_ero <= n_thr <= n_dil))
    # 级联产生的"假边缘框"：落在第 1 圈/倒数第 2 圈的阈值前景像素（仅开高斯时存在）
    h, w = thr.shape
    ring = np.zeros_like(thr, bool)
    ring[1, :] = ring[h - 2, :] = ring[:, 1] = ring[:, w - 2] = True
    frame_px = int(np.count_nonzero(thr[ring]))
    return notes, (n_thr, n_ero, n_dil, frame_px)

def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    rows, failed = [], 0
    cases = build_cases()
    print("测试用例数: %d（单色5 + 彩条3 + 随机3 + 几何6 + 种子%d）" % (
        len(cases), sum(1 for c in cases if c[1] == "种子")))
    print()
    print("%-34s %-10s %-11s %-9s %-9s %s" % ("用例", "类别", "尺寸", "阈值前景", "腐蚀", "不变量"))
    print("-" * 92)

    for name, kind, (w, h), rgb in cases:
        st, cfg = pipeline_stages(rgb, SEED_PRESET)
        notes, (n_thr, n_ero, n_dil, frame_px) = check_invariants(name, rgb, st)
        bad = [t for t, ok in notes if not ok]
        if bad:
            failed += 1
        rows.append({
            "case": name, "category": kind, "width": w, "height": h,
            "threshold_fg": n_thr, "erode_fg": n_ero, "dilate_fg": n_dil,
            "border_frame_px": frame_px,
            "output_fg": int(np.count_nonzero(st["output"])),
            "gray_mean": round(float(st["gray"].mean()), 2),
            "gauss_mean": round(float(st["gaussian"].mean()), 2),
            "invariants_ok": "YES" if not bad else "NO",
            "failed": ";".join(bad),
        })
        if kind in ("种子", "几何/边界") or name.startswith("solid_"):
            d = EVIDENCE / name
            d.mkdir(parents=True, exist_ok=True)
            for key in ("input_rgb", "gray", "gaussian", "edge", "threshold", "morphology"):
                arr = np.asarray(st[key], np.uint8)
                Image.fromarray(arr if arr.ndim == 3 else arr, mode=None).save(d / ("%s.png" % key))
        print("%-34s %-10s %-11s %-9d %-9d %s" % (
            name, kind, "%dx%d" % (w, h), n_thr, n_ero, "全部通过" if not bad else "❌ " + ";".join(bad)))

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)

    print("-" * 92)
    print("清单: %s（%d 行）" % (OUT_CSV, len(rows)))
    print("逐级中间结果: %s" % EVIDENCE)
    print("不变量失败用例: %d" % failed)
    return 1 if failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
