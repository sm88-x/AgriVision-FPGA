# -*- coding: utf-8 -*-
"""edge_validation / B5：量化 Morphology 的实际作用（跑在 PYNQ 板上）。

对每张农业图跑三种模式（0=Bypass / 1=Erode / 2=Dilate），每条都：
  1. 与 Golden 逐像素对拍，确认板子算对了；
  2. 统计可客观比较的指标：
       fg_pixels    前景像素数（形态学把前景变小/变大）
       comp_total   连通块总数（8 邻域）
       comp_small   面积 < min_area 的碎块数 —— 也就是"孤立噪点数"
       comp_big     面积 >= min_area 的连通块数
       count        与 segmentation 同口径的目标数（轮廓 + 面积过滤 + 贴边过滤）
       avg_area / avg_length / avg_width / avg_perimeter / max_area
输出：edge_regression_morph_effect.csv（逐次）+ 终端汇总表。

用法（板端，与 edge_morph_driver.py 同目录）：
    sudo python3 morphology_effect.py --images morph_images --out results/morph_effect.csv
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
from PIL import Image, ImageOps

from edge_morph_driver import (
    SobelParamDriver, compare, pipeline_reference,
    MODE_SOBEL, MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE, MORPH_NAMES, MORPH_BORDER,
)

W, H = 640, 480
THRESHOLD = 40
MIN_AREA = 100.0
BORDER_MARGIN = 1
KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

def load_canvas(path):
    """与板端 Dashboard 一致：等比缩放 + 白边补到 640x480。"""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source) if hasattr(ImageOps, "exif_transpose") else source.copy()
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

def to_gray(rgb):
    return np.rint(0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.uint8)

def analyze(binary):
    """统计形态学效果指标 + 与 segmentation 同口径的表型指标。"""
    clean = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, KERNEL, iterations=1)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(clean, connectivity=8)
    areas = [int(a) for a in stats[1:, 4]]
    comp_total = len(areas)
    comp_small = sum(1 for a in areas if a < MIN_AREA)
    comp_big = comp_total - comp_small

    contours = cv2.findContours(clean.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
    kept = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < MIN_AREA:
            continue
        x, y, bw, bh = cv2.boundingRect(contour)
        if (x <= BORDER_MARGIN or y <= BORDER_MARGIN
                or x + bw >= W - BORDER_MARGIN or y + bh >= H - BORDER_MARGIN):
            continue
        rect = cv2.minAreaRect(contour)
        side_a, side_b = rect[1]
        kept.append((area, max(side_a, side_b), min(side_a, side_b),
                     float(cv2.arcLength(contour, True))))

    def avg(index):
        return float(np.mean([k[index] for k in kept])) if kept else 0.0

    return {
        "fg_pixels": int(np.count_nonzero(binary)),
        "comp_total": comp_total,
        "comp_small": comp_small,
        "comp_big": comp_big,
        "count": len(kept),
        "avg_area": round(avg(0), 2),
        "avg_length": round(avg(1), 2),
        "avg_width": round(avg(2), 2),
        "avg_perimeter": round(avg(3), 2),
        "max_area": round(max([k[0] for k in kept]), 2) if kept else 0.0,
    }

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="morph_images")
    ap.add_argument("--out", default="data/evidence/analysis/morph_effect.csv")
    ap.add_argument("--save-bin", action="store_true", help="把每张二值结果也存成 PNG")
    args = ap.parse_args(argv)

    images_dir = Path(args.images)
    files = sorted(p for p in images_dir.iterdir()
                   if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"})
    if not files:
        print("没有找到图片：", images_dir)
        return 1

    driver = SobelParamDriver()
    print("Overlay:", driver.kernel_name, "| 外圈规则:", MORPH_BORDER, "| 阈值:", THRESHOLD)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    if args.save_bin:
        (Path(args.out).parent / "morph_bins").mkdir(parents=True, exist_ok=True)

    from pynq import allocate
    inp = allocate(shape=(W * H,), dtype=np.uint8)
    out = allocate(shape=(W * H,), dtype=np.uint8)

    rows = []
    for path in files:
        rgb = load_canvas(path)
        gray = to_gray(rgb)
        inp[:] = gray.reshape(-1)
        inp.flush()
        print("\n===== %s =====" % path.name)
        print("  %-8s %-7s %-8s %-8s %-8s %-8s %-8s %s"
              % ("模式", "Golden", "前景像素", "碎块数", "大块数", "Count", "平均面积", "最大面积"))
        for morph in (MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE):
            driver.run_frame(inp, out, W, H, MODE_SOBEL, THRESHOLD, 5.0, morph)
            out.invalidate()
            binary = np.array(out, copy=True).reshape(H, W)
            ref = pipeline_reference(gray, MODE_SOBEL, THRESHOLD, morph)
            stats = compare(ref, binary)
            m = analyze(binary)
            m.update({"image": path.name, "morph_mode": MORPH_NAMES[morph],
                      "golden": stats["verdict"],
                      "diff_pixels": stats["different_pixels"]})
            rows.append(m)
            print("  %-8s %-7s %-8d %-8d %-8d %-8d %-8.1f %s"
                  % (MORPH_NAMES[morph], stats["verdict"], m["fg_pixels"], m["comp_small"],
                     m["comp_big"], m["count"], m["avg_area"], m["max_area"]))
            if args.save_bin:
                Image.fromarray(binary).save(
                    Path(args.out).parent / "morph_bins" /
                    ("%s_%s.png" % (path.stem, MORPH_NAMES[morph])))

    fields = ["image", "morph_mode", "golden", "diff_pixels", "fg_pixels",
              "comp_total", "comp_small", "comp_big", "count",
              "avg_area", "avg_length", "avg_width", "avg_perimeter", "max_area"]
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print("\n逐次数据已写入 %s（%d 行）" % (args.out, len(rows)))
    n_pass = sum(1 for r in rows if r["golden"] == "PASS")
    print("Golden：%d/%d PASS" % (n_pass, len(rows)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
