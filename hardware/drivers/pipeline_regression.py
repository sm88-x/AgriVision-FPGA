#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline_validation 板端验证：RGB2Gray + Gaussian + 完整可配置流水线

用法（板端，把本文件、edge_morph_driver.py、run_pipeline.py、bit/hwh 放同一目录）：
    sudo python3 pipeline_regression.py --out results/pipeline_regression.csv

设计原则
    1. **寄存器偏移全部从同名 .hwh 解析**，不写死——
       A 每加一代核寄存器表都会变，读 hwh 才不会改漏。
    2. **先打印 hwh 里的寄存器表和核里实际存在的功能**，再决定测什么；
       缺什么就明确报"跳过 + 原因"，不假装测过。
    3. Golden 判定来自**真实像素 Diff**（max_diff / different_pixel_count），
       与任务书 B4 的定义一致。

⚠️ A 交付 pipeline_validation_v4.bit/hwh 后，只需确认下面这一张表（其余全自动）
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
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

# 板端只要把 edge_morph_driver.py、run_pipeline.py 和本文件放同一目录即可；
# 在仓库里跑时会自动去 app/python/ 找 run_pipeline.py。
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "src/software/app"))

from edge_morph_driver import (                     # noqa: E402
    SobelParamDriver, compare, edge_reference, morph_reference,
    MODE_SOBEL, MODE_PREWITT, MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE, MORPH_NAMES,
)
from run_pipeline import rgb2gray, gaussian3x3    # noqa: E402

W, H = 640, 480
SIZES = [(256, 256), (320, 240), (640, 480)]

# ---------------------------------------------------------------------------
# 已按 A 的《register_map_sobel_v4_pipeline.md》确认（2026-10-10）
#   寄存器偏移一律从同名 .hwh 自动解析，这里只写"寄存器名"和输入协议
# ---------------------------------------------------------------------------
REG_ENABLE_GRAY = "enable_gray"     # 0x38：1=RGB2Gray；0=透传输入低 8bit（灰度路径）
REG_ENABLE_GAUSS = "enable_gauss"   # 0x40：1=Gaussian 3x3；0=旁路
REG_OUT_SEL = "out_sel"             # 0x48：0=最终morph 1=gray 2=gauss 3=edge/threshold
INPUT_IS_RGB = True                 # A 的输入是 32bit/拍 RGBX888
RGB_BYTES_PER_PIXEL = 4             # 每拍 1 像素、4 字节（bit[31:24] 忽略）
THRESHOLD = 40

# out_sel 取值与含义（A 的文档）
OUT_MORPH, OUT_GRAY, OUT_GAUSS, OUT_EDGE = 0, 1, 2, 3
OUT_NAMES = {OUT_MORPH: "morph", OUT_GRAY: "gray", OUT_GAUSS: "gauss", OUT_EDGE: "edge"}

def pack_rgb(rgb):
    """把 HxWx3 的 RGB 打成 A 要求的 RGBX888：np.uint32((R<<16)|(G<<8)|B)，小端送出。

    对应文档："输入 AXI4-Stream 32bit/拍 RGBX888，bit[23:16]=R、bit[15:8]=G、
    bit[7:0]=B、bit[31:24] 忽略，keep=0xF，每拍 1 像素"。
    """
    h, w = rgb.shape[:2]
    packed = (rgb[:, :, 0].astype(np.uint32) << 16) | \
             (rgb[:, :, 1].astype(np.uint32) << 8) | \
              rgb[:, :, 2].astype(np.uint32)
    return packed.astype("<u4").reshape(-1).view(np.uint8)

def golden_for(rgb, mode, threshold, enable_gauss, out_sel, morph_mode=MORPH_BYPASS):
    """按 A 文档第 4 节的级联语义生成 Golden。

    已验证与我们的 B 组实现等价：
        out_sel=1 -> rgb2gray(rgb)（全帧有效，无外圈清零）
        out_sel=2 -> gaussian3x3(gray)（外圈 0）——仅 enable_gauss=1 时有意义
        out_sel=3 -> edge_reference(...)
        out_sel=0 -> morph_reference(edge_reference(...))
    """
    gray = rgb2gray(rgb)
    if out_sel == OUT_GRAY:
        return gray
    if out_sel == OUT_GAUSS:
        if not enable_gauss:
            raise ValueError("enable_gauss=0 时 out_sel=2 输出的是旁路平移流，不是高斯，跳过")
        return gaussian3x3(gray)
    g = gaussian3x3(gray) if enable_gauss else gray
    edge = edge_reference(g, mode, threshold)
    return edge if out_sel == OUT_EDGE else morph_reference(edge, morph_mode)

def load_canvas(path, size):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source) if hasattr(ImageOps, "exif_transpose") else source.copy()
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        rgb_image = Image.alpha_composite(background, rgba).convert("RGB")
    w, h = size
    sw, sh = rgb_image.size
    scale = min(w / sw, h / sh)
    nw = min(w, max(1, int(round(sw * scale))))
    nh = min(h, max(1, int(round(sh * scale))))
    import cv2
    resized = cv2.resize(np.asarray(rgb_image), (nw, nh),
                         interpolation=(cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR))
    canvas = np.full((h, w, 3), 255, dtype=np.uint8)
    left, top = (w - nw) // 2, (h - nh) // 2
    canvas[top:top + nh, left:left + nw] = resized
    return canvas

def check_capabilities(driver):
    """按 hwh 里实际存在的寄存器，判断这个核支持哪些功能。"""
    names = set(driver.regs)
    caps = {
        "morph_mode": "morph_mode" in names,
        "enable_gray": REG_ENABLE_GRAY in names if REG_ENABLE_GRAY else False,
        "enable_gauss": REG_ENABLE_GAUSS in names if REG_ENABLE_GAUSS else False,
        "out_sel": REG_OUT_SEL in names if REG_OUT_SEL else False,
    }
    return caps

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="edge_validation_images", help="农业图目录")
    ap.add_argument("--units", default="unit_tests", help="单元测试图目录（可选）")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--repeats", type=int, default=20)
    ap.add_argument("--out", default="data/evidence/analysis/pipeline_regression.csv")
    args = ap.parse_args(argv)

    driver = SobelParamDriver()
    info = driver.info()
    print("=" * 78)
    print("Overlay :", info["bitfile"], "| 核:", info["kernel_ip"])
    print("寄存器表（自动从 hwh 解析）：")
    for name, off in sorted(driver.regs.items(), key=lambda kv: kv[1]):
        print("    %-16s %s" % (name, off))
    caps = check_capabilities(driver)
    print()
    print("核里实际具备的功能：")
    for k, v in caps.items():
        print("    %-14s %s" % (k, "有" if v else "**没有**"))

    missing = [k for k, v in caps.items() if not v]
    if missing:
        print()
        print("⚠️ 下面这些功能在 hwh 里找不到对应寄存器，相关用例会被跳过：")
        for k in missing:
            print("    - %s（若是 A 用了别的寄存器名，改本文件顶部的 REG_* 常量即可）" % k)
    print("=" * 78)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    # 用例矩阵（对应任务书"先跑单模块，再跑完整链"的逐级顺序）：
    #   1) 三档分辨率 × {gray / gauss / edge / morph}  —— 逐级对拍（out_sel 1/2/3/0）
    #   2) 三档分辨率 × 关高斯走完整链                  —— 验证旁路不回退
    #   3) 三档分辨率 × Prewitt 走完整链                —— 验证双算法
    images_dir = Path(args.images)
    cases = []
    for size in SIZES:
        for out_sel in (OUT_GRAY, OUT_GAUSS, OUT_EDGE, OUT_MORPH):
            if out_sel == OUT_GAUSS and not caps["enable_gauss"]:
                continue
            cases.append(("D2_%s_%dx%d" % (OUT_NAMES[out_sel], size[0], size[1]),
                          size, True, MODE_SOBEL, out_sel, MORPH_BYPASS))
        cases.append(("D2_nogauss_morph_%dx%d" % (size[0], size[1]),
                      size, False, MODE_SOBEL, OUT_MORPH, MORPH_BYPASS))
        cases.append(("D2_dilate_%dx%d" % (size[0], size[1]),
                      size, True, MODE_SOBEL, OUT_MORPH, MORPH_DILATE))
        cases.append(("D2_prewitt_morph_%dx%d" % (size[0], size[1]),
                      size, True, MODE_PREWITT, OUT_MORPH, MORPH_BYPASS))
    print("用例数：%d（三档分辨率 × 逐级/旁路/双算法）" % len(cases))
    if not images_dir.is_dir():
        print("找不到农业图目录：", images_dir)
    results = []
    for case_id, size, gauss, mode, out_sel, morph_mode in cases:
        print("\n----- %s -----" % case_id)
        w, h = size
        mode_name = "Sobel" if mode == MODE_SOBEL else "Prewitt"
        img_path = sorted(images_dir.glob("*.png"))[0] if images_dir.is_dir() else None
        if img_path is None:
            print("  跳过：没有输入图")
            continue
        rgb = load_canvas(img_path, size)
        # 软件参考：按 out_sel 只取对应那一级（逐级对拍）
        reference = golden_for(rgb, mode, THRESHOLD, gauss, out_sel, morph_mode)

        from pynq import allocate
        if INPUT_IS_RGB:
            payload = pack_rgb(rgb)
        else:
            payload = rgb2gray(rgb).reshape(-1)
        inp = allocate(shape=(int(payload.size),), dtype=np.uint8)
        out = allocate(shape=(w * h,), dtype=np.uint8)
        inp[:] = payload
        inp.flush()

        extra = {}
        if caps["enable_gray"]:
            extra[REG_ENABLE_GRAY] = 1 if INPUT_IS_RGB else 0
        if caps["enable_gauss"]:
            extra[REG_ENABLE_GAUSS] = 1 if gauss else 0
        if caps["out_sel"]:
            extra[REG_OUT_SEL] = out_sel

        try:
            pl_ms = driver.run_frame(inp, out, w, h, mode, THRESHOLD, 5.0,
                                     morph_mode, extra) * 1e3
            out.invalidate()
            pl = np.array(out, copy=True).reshape(h, w)
            stats = compare(reference, pl)
            print("  %dx%d %s 高斯%s out_sel=%s(%s) morph=%s -> Golden %s  不同像素=%d  最大误差=%d  PL %.3f ms"
                  % (w, h, mode_name, "开" if gauss else "关", out_sel, OUT_NAMES[out_sel],
                     MORPH_NAMES[morph_mode], stats["verdict"],
                     stats["different_pixels"], stats["max_error"], pl_ms))
            # 性能：warm-up + 正式测量（任务书 B4：warm-up 3 次、正式 ≥20 次）
            for _ in range(args.warmup):
                driver.run_frame(inp, out, w, h, mode, THRESHOLD, 5.0, morph_mode, extra)
            samples = np.asarray([
                driver.run_frame(inp, out, w, h, mode, THRESHOLD, 5.0, morph_mode, extra) * 1e3
                for _ in range(args.repeats)
            ])
            print("      性能 warmup=%d repeats=%d  mean=%.3f median=%.3f min=%.3f max=%.3f ms  FPS=%.1f"
                  % (args.warmup, args.repeats, samples.mean(), np.median(samples),
                     samples.min(), samples.max(), 1000.0 / samples.mean()))
            results.append({"case": case_id, "size": "%dx%d" % size, "gauss": int(gauss),
                            "algorithm": mode_name, "out_sel": out_sel,
                            "morph_mode": MORPH_NAMES[morph_mode], "golden": stats["verdict"],
                            "max_diff": stats["max_error"],
                            "different_pixels": stats["different_pixels"],
                            "mean_ms": round(float(samples.mean()), 4),
                            "median_ms": round(float(np.median(samples)), 4),
                            "min_ms": round(float(samples.min()), 4),
                            "max_ms": round(float(samples.max()), 4),
                            "fps": round(1000.0 / samples.mean(), 1)})
        except Exception as exc:                       # noqa: BLE001
            print("  [失败] %s: %s" % (type(exc).__name__, exc))
            results.append({"case": case_id, "size": "%dx%d" % size, "gauss": int(gauss),
                            "algorithm": mode_name, "out_sel": out_sel,
                            "morph_mode": MORPH_NAMES[morph_mode], "golden": "ERROR", "max_diff": "",
                            "different_pixels": "", "mean_ms": "", "median_ms": "",
                            "min_ms": "", "max_ms": "", "fps": ""})
        finally:
            del inp, out

    if results:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
        print("\n用例清单已写入 %s（%d 行）" % (args.out, len(results)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
