# -*- coding: utf-8 -*-
"""segmentation 演示回归：一次跑完 D1~D4 矩阵 + 农业主 Demo 稳定性连跑

⚠️ 版本依赖（2026-10-08 起，运行前请先读这段）
    本脚本绑定的是 **segmentation 版 Dashboard**：它复用 Dashboard 建立的
    WIDTH / HEIGHT / to_gray / measure_ps / run_fpga / analyze_phenotype 六个名字。

    仓库当前的 `hardware/drivers/dashboard.py` 已经升级为 agripheno 版——
    表型分析改由 `agripheno.AgriEngine` 承担（新增 ROI、自动参数选择、
    Seeds / Green leaves 两套 profile），上面那六个名字**已经不存在**。
    因此本脚本现在会停在下面的守卫处并给出提示，这是预期行为，不是环境问题。

    处理方式（需与队友确认后再动）：
      a) 适配新版：改用 `agripheno.AgriEngine.analyze()` 重写；由于分析口径
         已变（ROI / profile / 自动参数），D1~D4 的数字必须重新上板验证；
      b) 归档：把本脚本连同 segmentation 结果一起移入 `history/`，只保留原始记录。

    segmentation 那一轮的原始数据（D1~D4 + 连跑 5 次，共 12 行）已完整保存在
    `board/results/segmentation_demo_regression/`，**查阅报告不依赖本脚本**。

前提（若已适配新版，此前提改为：与 agripheno.py 放在同一目录）：
    同一个 Jupyter 内核里已经执行过 `%run dashboard.py`。
本脚本直接复用它的 to_gray / measure_ps / run_fpga / analyze_phenotype，
不另写一套算法，保证测的就是 Dashboard 实际用的那条链路。

用法（在 Dashboard 那个 Notebook 里接着跑）：
    %run application_regression.py

输出：
    屏幕上一张结果表 + results/application_regression.csv（逐次原始记录）

需要的素材放在脚本同目录（任意尺寸都行，会自动等比缩放补白到 640x480）：
    plant_a_640x480.png        农业主 Demo
    plant_leaf_640x480.png     叶片 Demo
    microscopy_640x480.png     显微图（没有就自动跳过 D3）
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
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

# WIDTH / HEIGHT / to_gray / measure_ps / run_fpga / analyze_phenotype
# 都来自前面 %run dashboard.py 建立的命名空间。
#
# 注意：%run 默认会把脚本放在一个**空命名空间**里执行，看不到 Dashboard 的变量，
# 必须用 %run -i（-i = 在当前交互命名空间里执行）。
_missing = [n for n in ("WIDTH", "HEIGHT", "to_gray", "measure_ps",
                        "run_fpga", "analyze_phenotype") if n not in globals()]
if _missing:
    raise SystemExit(
        "缺少这些变量：%s\n\n"
        "两种可能，请对号入座：\n"
        "1) 只是忘了加 -i：请改用  %%run -i application_regression.py\n"
        "   （不加 -i 时脚本在空命名空间执行，看不到 Dashboard 建立的变量）\n"
        "2) 跑的是新版 Dashboard：本脚本绑定的是 segmentation 版 Dashboard，\n"
        "   新版已把表型分析换成 agripheno.AgriEngine，那六个名字不再存在。\n"
        "   这种情况下本脚本需要先适配（数字要重新上板验证）。\n"
        "   segmentation 的原始数据已归档在 board/results/segmentation_demo_regression/，\n"
        "   查阅报告不需要跑本脚本。详见本文件开头说明。"
        % (", ".join(_missing),)
    )

W, H = WIDTH, HEIGHT
ROOT = Path.cwd()

THRESHOLD = 40        # 与 Dashboard 默认值一致（>0 表示二值化）
MIN_AREA = 100.0

# 演示回归矩阵 —— 用例编号沿用任务书
CASES = [
    ("D1", "plant_a_640x480.png", "Sobel", "FPGA"),
    ("D1b", "plant_a_640x480.png", "Prewitt", "FPGA"),
    ("D2", "plant_leaf_640x480.png", "Sobel", "FPGA"),
    ("D2b", "plant_leaf_640x480.png", "Prewitt", "FPGA"),
    ("D4", "plant_a_640x480.png", "Sobel", "PS"),
    ("D3a", "microscopy_640x480.png", "Sobel", "FPGA"),
    ("D3b", "microscopy_640x480.png", "Prewitt", "FPGA"),
]
STABILITY_CASE = ("D1", "plant_a_640x480.png", "Sobel", "FPGA")
STABILITY_RUNS = 5

def load_canvas(path):
    """与 Dashboard 的上传逻辑完全一致：等比缩放 + 白边补到 640x480。"""
    with Image.open(path) as source:
        image = (ImageOps.exif_transpose(source)
                 if hasattr(ImageOps, "exif_transpose") else source.copy())
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        rgb_image = Image.alpha_composite(background, rgba).convert("RGB")
    sw, sh = rgb_image.size
    scale = min(W / sw, H / sh)
    nw = min(W, max(1, int(round(sw * scale))))
    nh = min(H, max(1, int(round(sh * scale))))
    resized = cv2.resize(np.asarray(rgb_image), (nw, nh),
                         interpolation=(cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR))
    canvas = np.full((H, W, 3), 255, dtype=np.uint8)
    left, top = (W - nw) // 2, (H - nh) // 2
    canvas[top:top + nh, left:left + nw] = resized
    return canvas

def run_one(tag, image, algo, backend, save_dir=None):
    """跑一次完整链路，返回一行记录；素材缺失返回 None。

    save_dir 给了就顺手存一张标注图（稳定性连跑的重复项不存，省得堆一堆一样的图）。
    """
    path = ROOT / image
    if not path.exists():
        print("  %-5s [跳过] 找不到 %s" % (tag, image))
        return None

    canvas = load_canvas(path)
    gray = to_gray(canvas)

    # PS 参考与软件耗时（与 Dashboard 同一套口径）
    ps_edge, ps_ms, _ = measure_ps(gray, algo, THRESHOLD)

    if backend == "FPGA":
        edge, fpga_ms = run_fpga(gray, algo, THRESHOLD)
        golden = bool(np.array_equal(edge, ps_edge))
        diff = int(np.count_nonzero(edge != ps_edge))
    else:
        edge, fpga_ms, golden, diff = ps_edge, None, None, None

    count = None
    pheno = None
    if golden is not False:          # 对拍失败就不做表型（与 Dashboard 一致）
        pheno, annotated, _ = analyze_phenotype(
            canvas, edge,
            binary_output=(THRESHOLD > 0),
            min_area=MIN_AREA,
            close_iterations=1,
            edge_threshold=40,
        )
        count = pheno["count"]
        if save_dir is not None and "#" not in tag:
            save_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(annotated).save(str(save_dir / ("%s_annotated.png" % tag)))

    speed = (ps_ms / fpga_ms) if fpga_ms else None
    print("  %-5s %-24s %-8s %-4s  PS %8.2f ms  FPGA %s  Golden %s  Count %s  avg_area %s"
          % (tag, image, algo, backend, ps_ms,
             ("%7.3f ms" % fpga_ms) if fpga_ms else "   -    ",
             ("PASS" if golden else "FAIL(%d)" % diff) if golden is not None else "  -  ",
             count if count is not None else "-",
             ("%.1f" % pheno["avg_area"]) if pheno else "-"))

    def _r(v, nd=2):
        return round(float(v), nd) if (pheno and v is not None) else ""

    return {
        "case": tag, "image": image, "algorithm": algo, "backend": backend,
        "threshold": THRESHOLD, "ps_ms": round(ps_ms, 4),
        "fpga_e2e_ms": round(fpga_ms, 4) if fpga_ms else "",
        "speedup": round(speed, 3) if speed else "",
        "golden": "PASS" if golden else ("FAIL" if golden is False else "N/A"),
        "different_pixels": diff if diff is not None else "",
        "count": count if count is not None else "",
        "avg_area": _r(pheno["avg_area"]) if pheno else "",
        "avg_length": _r(pheno["avg_length"]) if pheno else "",
        "avg_width": _r(pheno["avg_width"]) if pheno else "",
        "avg_perimeter": _r(pheno["avg_perimeter"]) if pheno else "",
    }

def main():
    rows = []
    save_dir = ROOT / "data/evidence/analysis/annotated"

    print("=" * 72)
    print("演示回归矩阵（D1~D4）")
    print("=" * 72)
    for tag, image, algo, backend in CASES:
        try:
            row = run_one(tag, image, algo, backend, save_dir=save_dir)
        except Exception as exc:
            print("  %-5s [失败] %s: %s" % (tag, type(exc).__name__, exc))
            row = {"case": tag, "image": image, "algorithm": algo,
                   "backend": backend, "threshold": THRESHOLD,
                   "ps_ms": "", "fpga_e2e_ms": "", "speedup": "",
                   "golden": "ERROR:%s" % type(exc).__name__,
                   "different_pixels": "", "count": "",
                   "avg_area": "", "avg_length": "", "avg_width": "",
                   "avg_perimeter": ""}
        if row:
            rows.append(row)

    print()
    print("=" * 72)
    print("农业主 Demo 稳定性连跑（%d 次，检查是否卡死 / 错位 / 结果不更新）"
          % STABILITY_RUNS)
    print("=" * 72)
    tag, image, algo, backend = STABILITY_CASE
    times, counts, all_pass = [], [], True
    for i in range(1, STABILITY_RUNS + 1):
        t0 = time.perf_counter()
        row = run_one("%s#%d" % (tag, i), image, algo, backend)
        times.append(time.perf_counter() - t0)
        if row:
            rows.append(row)
            counts.append(row["count"])
            all_pass = all_pass and row["golden"] == "PASS"
    print("  连跑 %d 次总耗时 %.1f s（平均 %.2f s/次）"
          % (STABILITY_RUNS, sum(times), sum(times) / len(times)))
    print("  全部 Golden PASS：%s" % all_pass)
    print("  各次 Count：%s（一致 = 结果可复现）" % counts)

    out = ROOT / "data/evidence/segmentation_comparison_regression.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["case", "image", "algorithm", "backend", "threshold", "ps_ms",
              "fpga_e2e_ms", "speedup", "golden", "different_pixels", "count",
              "avg_area", "avg_length", "avg_width", "avg_perimeter"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print()
    print("逐次记录 → %s（%d 行）" % (out, len(rows)))
    if save_dir.exists():
        print("标注图   → %s（%d 张）" % (save_dir, len(list(save_dir.glob("*.png")))))

main()
