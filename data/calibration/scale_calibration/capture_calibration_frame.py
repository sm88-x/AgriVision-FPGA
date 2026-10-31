#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene B：用笔记本摄像头抓一张同帧参照图（尺子 + 种子，同一平面）。

相机机位不固定时，每张测量图都必须同时包含种子和尺子。
脚本先稳定曝光，再从连续帧中挑一张亮度、清晰度较合适的图。

用法：
    python -B data/calibration/scale_calibration/capture_calibration_frame.py --tag soybean \
        --out-dir work/calibration_shots --exposure -2
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
import time
from pathlib import Path

import cv2
import numpy as np

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="文件名标签，如 soybean / mungbean / redbean")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--out-dir", default="work/calibration_shots")
    ap.add_argument("--warmup", type=float, default=1.5, help="预热秒数（等自动曝光稳定）")
    ap.add_argument("--auto-exposure", action="store_true", help="使用自动曝光（默认关闭）")
    ap.add_argument("--exposure", type=float, default=-2.0,
                    help="DirectShow 手动曝光值；本机相机 -2 可稳定照亮白纸")
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise SystemExit("打不开摄像头 %d" % args.camera)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    # 本机自动曝光会从亮度 62 漂到 150，单靠多抓帧仍可能选到暗帧。
    # DirectShow 下 0.25 表示手动曝光，具体 exposure 数值由设备决定。
    if args.auto_exposure:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
    else:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        cap.set(cv2.CAP_PROP_EXPOSURE, args.exposure)
    t0 = time.time()
    frames = []
    while time.time() - t0 < args.warmup:
        ok, f = cap.read()
        if ok:
            frames.append(f)
        time.sleep(0.05)
    # 连抓几帧，优先挑白纸占比高且不过曝、同时较清晰的那张。
    for _ in range(8):
        ok, f = cap.read()
        if ok:
            frames.append(f)
        time.sleep(0.25)
    if not frames:
        cap.release()
        raise SystemExit("没抓到帧")

    def score(image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        bright = float((gray > 200).mean())
        saturated = float((gray >= 250).mean())
        return bright * 100.0 + min(sharp, 500.0) * 0.03 - saturated * 50.0

    frame = max(frames[-10:], key=score)
    cap.release()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("coin_%s.png" % args.tag)
    cv2.imwrite(str(path), frame)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    bright = float((gray > 200).mean())
    dark = float((gray < 80).mean())
    print("已保存 %s  (%dx%d)" % (path, frame.shape[1], frame.shape[0]))
    print("曝光模式：%s%s" % (
        "自动" if args.auto_exposure else "手动",
        "" if args.auto_exposure else "（%.1f）" % args.exposure))
    print("画面统计：亮度中位 %.0f｜亮区(>200) %.1f%%｜暗区(<80) %.1f%%"
          % (float(np.median(gray)), bright * 100, dark * 100))
    if bright < 0.12:
        print("[WARN] 亮区占比偏低：白纸可能没铺满/光线偏暗，建议调亮或换角度再拍")
    if dark > 0.35:
        print("[WARN] 暗区占比偏高：可能有手/阴影/背景入镜，建议只留白纸+硬币+种子")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
