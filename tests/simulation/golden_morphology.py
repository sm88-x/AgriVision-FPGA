# -*- coding: utf-8 -*-
"""edge_validation / B1：V4 Morphology（3x3 Erode / Dilate）Golden Reference。

用途
    给板子上的 PL 形态学算子提供"标准答案"：同样的输入，Python 算出应该是什么结果，
    然后和板上跑出来的图逐像素比对。

口 径（三条，必须和 A 确认后固化）
    1. 窗口：3x3 全窗口，Erode 取 9 个像素的最小值，Dilate 取最大值。
       —— 对 0/255 二值图，等价于 "9 个像素全 1 才输出 1"（AND）与 "有一个 1 就输出 1"（OR）。
    2. 内部像素：直接用输入的 3x3 窗口算，不做任何边界外推。
       因为上一级（Edge/Threshold）输出的最外圈本来就是 0，所以窗口天然能看到边界的 0。
    3. 最外一圈像素：默认强 制输出 0（border='zero'），与现有 V3 的"最外圈 0"约定一致。
       如果 A 改成"外圈保持原值"，把 border 换成 'keep' 即可，两种都实现了。

自检
    脚本内置两套独立实现（numpy 滑窗 与 OpenCV erode/dilate），两者内部区域必须逐像素一致，
    才算 Golden 本身可信。

用法
    python golden_morphology.py            # 跑单元测试图、自检、导出 PNG 和清单
    python golden_morphology.py --border keep
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
from PIL import Image

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE / "unit_tests"

# morph_mode 的取值约定（与 A 的寄存器表对齐；A 若改用别的编码，只改这里）
MODE_BYPASS = 0
MODE_ERODE = 1
MODE_DILATE = 2
MODE_NAMES = {MODE_BYPASS: "bypass", MODE_ERODE: "erode", MODE_DILATE: "dilate"}

def _sliding(img, op):
    """numpy 滑窗实现：内部区域做 3x3 min/max，外圈先留 0。"""
    h, w = img.shape
    if h < 3 or w < 3:
        raise ValueError("尺寸至少 3x3，当前 %dx%d" % (w, h))
    core = np.full((h - 2, w - 2), 255 if op == "min" else 0, dtype=np.uint8)
    for dy in range(3):
        for dx in range(3):
            win = img[dy:dy + h - 2, dx:dx + w - 2]
            core = np.minimum(core, win) if op == "min" else np.maximum(core, win)
    return core

def _opencv(img, op):
    """OpenCV 独立实现，只取内部区域做交叉验证用。"""
    k = np.ones((3, 3), np.uint8)
    if op == "min":
        out = cv2.erode(img, k, borderType=cv2.BORDER_REPLICATE)
    else:
        out = cv2.dilate(img, k, borderType=cv2.BORDER_REPLICATE)
    return out[1:-1, 1:-1]

def morph(img, mode, border="zero"):
    """按 morph_mode 处理一张 uint8 图（0/255 二值图或灰度图均可）。

    border='zero'：外圈强制 0（默认，与 V3 口径一致）
    border='keep'：外圈保持输入原值（便于人眼观察真实照片效果）
    """
    img = np.asarray(img, dtype=np.uint8)
    if mode == MODE_BYPASS:
        return img.copy()
    if mode not in (MODE_ERODE, MODE_DILATE):
        raise ValueError("未知 morph_mode: %r（0=bypass, 1=erode, 2=dilate）" % (mode,))

    op = "min" if mode == MODE_ERODE else "max"
    core = _sliding(img, op)
    core_cv = _opencv(img, op)
    if not np.array_equal(core, core_cv):
        raise AssertionError(
            "Golden 自检失败：numpy 与 OpenCV 实现不一致（mode=%s），先修 Golden 再上板"
            % MODE_NAMES[mode]
        )

    out = np.zeros_like(img) if border == "zero" else img.copy()
    out[1:-1, 1:-1] = core
    return out

def compare(reference, result):
    """与板上结果逐像素比对（B4 要求的指标）。"""
    ref = np.asarray(reference, dtype=np.int16)
    res = np.asarray(result, dtype=np.int16)
    if ref.shape != res.shape:
        return {"verdict": "SHAPE_MISMATCH", "different_pixel_count": -1,
                "max_diff": -1, "mean_error": float("nan"),
                "shape_ref": ref.shape, "shape_res": res.shape}
    diff = np.abs(res - ref)
    different = int(np.count_nonzero(diff))
    return {
        "verdict": "PASS" if different == 0 else "FAIL",
        "different_pixel_count": different,
        "max_diff": int(diff.max()),
        "mean_error": float(diff.mean()),
        "shape_ref": ref.shape,
        "shape_res": res.shape,
    }

# ----------------------------------------------------------------------
# 单元测试图：极端情况，专治边界和异常
# ----------------------------------------------------------------------
def make_unit_tests():
    tests = {}
    rng = np.random.default_rng(20261008)

    tests["01_all_zero"] = np.zeros((16, 16), np.uint8)
    tests["02_all_255"] = np.full((16, 16), 255, np.uint8)

    img = np.zeros((16, 16), np.uint8)
    img[8, 8] = 255
    tests["03_single_pixel"] = img

    img = np.zeros((16, 16), np.uint8)
    img[7:10, 7:10] = 255
    tests["04_center_3x3_block"] = img

    img = np.zeros((16, 16), np.uint8)
    img[0:4, 0:4] = 255          # 贴左上角
    img[0:4, 12:16] = 255        # 贴右上角
    img[12:16, 6:10] = 255       # 贴下边界
    tests["05_border_blocks"] = img

    base = np.zeros((16, 16), np.uint8)
    base[4:12, 4:12] = 255
    noise = rng.random((16, 16))
    img = base.copy()
    img[noise < 0.06] = 255
    img[noise > 0.94] = 0
    tests["06_salt_pepper"] = img

    img = np.zeros((32, 32), np.uint8)
    cv2.rectangle(img, (2, 2), (29, 29), 255, 2)
    img[2:5, 14:18] = 0          # 制造断口
    tests["07_broken_outline"] = img

    tests["08_random_binary"] = (rng.random((24, 32)) > 0.5).astype(np.uint8) * 255

    return tests

def check_invariants(name, test_img, golden_erode, golden_dilate):
    """把"理应成立"的性质写成断言，避免 Golden 自己写错。"""
    notes = []
    if np.count_nonzero(test_img) == 0:                      # 全 0
        ok = np.count_nonzero(golden_erode) == 0 and np.count_nonzero(golden_dilate) == 0
        notes.append(("全0图 -> 输出仍全0", ok))
    if np.all(test_img == 255):                              # 全 255
        inner = golden_erode[1:-1, 1:-1]
        ok = np.all(inner == 255) and np.count_nonzero(golden_dilate[1:-1, 1:-1] == 255) == inner.size
        notes.append(("全255图 -> 内部仍是255", ok))
    # Erode 结果不应比输入更多前景；Dilate 不应比输入更少（只在内部区域比较）
    e_ok = np.all(golden_erode[1:-1, 1:-1] <= test_img[1:-1, 1:-1])
    notes.append(("Erode <= 原图（内部）", bool(e_ok)))
    d_ok = np.all(golden_dilate[1:-1, 1:-1] >= test_img[1:-1, 1:-1])
    notes.append(("Dilate >= 原图（内部）", bool(d_ok)))
    return notes

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--border", default="zero", choices=["zero", "keep"],
                    help="外圈规则：zero=强制0（默认，与V3一致）；keep=保持原值")
    args = ap.parse_args()

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    tests = make_unit_tests()
    rows = []

    print("=" * 78)
    print("edge_validation / B1  Golden Morphology 自检   外圈规则 = %s" % args.border)
    print("=" * 78)
    print("%-22s %-8s %-16s %-16s %s" % ("测试图", "尺寸", "Erode 前景像素", "Dilate 前景像素", "不变量"))
    print("-" * 78)

    for name, img in tests.items():
        h, w = img.shape
        golden_erode = morph(img, MODE_ERODE, border=args.border)
        golden_dilate = morph(img, MODE_DILATE, border=args.border)
        notes = check_invariants(name, img, golden_erode, golden_dilate)
        bad = [n for n, ok in notes if not ok]

        Image.fromarray(img).save(UNIT_DIR / ("%s_input.png" % name))
        Image.fromarray(golden_erode).save(UNIT_DIR / ("%s_golden_erode.png" % name))
        Image.fromarray(golden_dilate).save(UNIT_DIR / ("%s_golden_dilate.png" % name))

        rows.append({
            "test": name, "width": w, "height": h,
            "input_fg": int(np.count_nonzero(img)),
            "erode_fg": int(np.count_nonzero(golden_erode)),
            "dilate_fg": int(np.count_nonzero(golden_dilate)),
            "invariants_ok": "YES" if not bad else "NO",
            "failed_invariants": ";".join(bad),
        })
        print("%-22s %-8s %-16d %-16d %s" % (
            name, "%dx%d" % (w, h),
            int(np.count_nonzero(golden_erode)),
            int(np.count_nonzero(golden_dilate)),
            "全部通过" if not bad else "❌ " + ";".join(bad)))

    csv_path = UNIT_DIR / "golden_manifest.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print("-" * 78)
    print("单元测试图与 Golden 已导出:", UNIT_DIR)
    print("清单:", csv_path.name)
    print()
    print("对照板子的用法：")
    print("  from golden_morphology import morph, compare, MODE_ERODE, MODE_DILATE")
    print("  golden = morph(test_img, MODE_ERODE)")
    print("  print(compare(golden, pl_result))   # -> max_diff / different_pixel_count / verdict")
    print()
    print("提醒：外圈规则（zero / keep）必须先和 A 定死，否则板子与 Golden 永远差一圈。")

if __name__ == "__main__":
    main()
