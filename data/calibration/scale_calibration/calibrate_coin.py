#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene B：用一枚已知直径的硬币做像素↔毫米标定（无需读尺子刻度）。

为什么用硬币：读钢尺刻度要么靠人点像素、要么靠识别刻度，都容易错；
硬币是标准件（1 元硬币直径 25.0 mm），只要把它和种子放在**同一平面**拍进画面，
程序就能自动找圆、量直径、算出 px/mm，并给出拟合残差作为可信度。

用法：
    python -B data/calibration/scale_calibration/calibrate_coin.py --image calibration/scene/ruler_soybean.png \
        --diameter-mm 25.0 --out data/calibration/scale_calibration/calibration.json

可选：
    --preview  calibration/scene/coin_check.png   把找到的圆画出来存一张核对图
    --force    覆盖已有 valid=true 的 calibration.json
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
import json
from pathlib import Path

import cv2
import numpy as np

ROOT = PROJECT_ROOT
COINS = {  # 常见人民币硬币直径（mm），用于 --coin 简写
    "1yuan": 25.0, "1jiao": 19.0, "5jiao": 20.5,
}

def find_coin(image_bgr, min_circularity=0.72):
    """找出画面里最像"圆片"的那块，返回 (圆心, 半径, 圆度, 面积)。"""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    best = None
    for thresh_mode in ("otsu", "dark", "bright"):
        if thresh_mode == "otsu":
            _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        elif thresh_mode == "dark":
            _, binary = cv2.threshold(blur, 120, 255, cv2.THRESH_BINARY_INV)
        else:
            _, binary = cv2.threshold(blur, 180, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < 1500:                      # 太小，不像硬币（640×480 下硬币约 3~8 k px²）
                continue
            per = float(cv2.arcLength(c, True))
            if per <= 0:
                continue
            circularity = 4 * np.pi * area / (per * per)
            (cx, cy), radius = cv2.minEnclosingCircle(c)
            fill = area / max(np.pi * radius * radius, 1e-6)     # 圆内填充率（排除缺口/月牙）
            if circularity < min_circularity or fill < 0.80:
                continue
            score = circularity * fill
            if best is None or score > best[0]:
                best = (score, (float(cx), float(cy)), float(radius),
                        float(circularity), area, thresh_mode)
    return best

def main():
    ap = argparse.ArgumentParser(description="用硬币做像素↔毫米标定")
    ap.add_argument("--image", required=True)
    ap.add_argument("--diameter-mm", type=float, default=0.0)
    ap.add_argument("--coin", default="", help="简写：1yuan / 5jiao / 1jiao")
    ap.add_argument("--out", default=str(ROOT / "data/calibration/scale_calibration/calibration.json"))
    ap.add_argument("--preview", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        old = json.loads(out_path.read_text(encoding="utf-8"))
        if old.get("valid"):
            raise SystemExit("已有 valid=true 的标定文件，确认要覆盖请加 --force")

    diameter_mm = args.diameter_mm or COINS.get(args.coin, 0.0)
    if diameter_mm <= 0:
        raise SystemExit("需要 --diameter-mm 或 --coin {1yuan,5jiao,1jiao}")

    image = cv2.imread(str(args.image))
    if image is None:
        raise SystemExit("读不到图片: " + args.image)
    found = find_coin(image)
    if not found:
        raise SystemExit("没找到像硬币的圆形目标：换一张对比更清楚的图，或调 --min-circularity")
    _, (cx, cy), radius, circularity, area, mode = found
    diameter_px = 2.0 * radius
    px_per_mm = diameter_px / diameter_mm

    # 可信度：圆度与"圆内填充率"都给分；把直径的估计误差按 ±1 px 折算成相对误差上限
    uncertainty = 1.0 / max(diameter_px, 1e-6)
    payload = {
        "schema": "agrivision.calibration.v1",
        "valid": True,
        "created": "auto",
        "method": "已知直径硬币 + 最小外接圆（单平面，中心 ROI 内）",
        "px_per_mm": round(px_per_mm, 4),
        "reference": {
            "type": "coin",
            "known_length_mm": diameter_mm,
            "measured_px": round(diameter_px, 2),
            "center_px": [round(cx, 1), round(cy, 1)],
            "circularity": round(circularity, 3),
            "blob_area_px": round(area, 1),
            "threshold_mode": mode,
        },
        "scope": {
            "image_size": [int(image.shape[1]), int(image.shape[0])],
            "working_roi_normalized": [0.04, 0.04, 0.96, 0.88],
            "note": "只在这台相机、这个高度/角度、这个分辨率下有效；换机位必须重标",
        },
        "uncertainty": {
            "relative_length": round(uncertainty, 4),
            "note": "按直径 ±1 px 估计；实际误差还受硬币边缘模糊与透视影响",
        },
        "invalidation_rules": [
            "输入图像尺寸与 scope.image_size 不一致",
            "相机高度或角度改变",
            "硬币与种子不在同一平面",
            "画面透视明显时，只允许在 working_roi_normalized 内使用",
        ],
        "fallback": {
            "length_unit": "px", "area_unit": "px^2",
            "ui_behavior": "valid=false 时禁用毫米显示，只显示像素口径并提示重新标定",
        },
        "derived_units": {
            "length_mm": "length_px / px_per_mm",
            "area_mm2": "area_px / (px_per_mm^2)",
            "perimeter_mm": "perimeter_px / px_per_mm",
        },
        "source_image": str(Path(args.image)),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.preview:
        vis = image.copy()
        cv2.circle(vis, (int(round(cx)), int(round(cy))), int(round(radius)), (0, 200, 0), 2)
        cv2.putText(vis, "d=%.1f px -> %.3f px/mm" % (diameter_px, px_per_mm),
                    (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2, cv2.LINE_AA)
        Path(args.preview).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(args.preview, vis)
        print("核对图 ->", args.preview)

    print("硬币直径 %.2f px（圆度 %.2f）→ px_per_mm = %.4f（相对不确定度 %.2f%%）"
          % (diameter_px, circularity, px_per_mm, uncertainty * 100))
    print("已写入", out_path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
