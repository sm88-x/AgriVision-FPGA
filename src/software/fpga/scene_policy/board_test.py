#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene A：PYNQ-Z2 上三配置实测。

在 320x240 / 640x480 下，对 normal / low_contrast / dense 三套配置各跑
>=30 帧，记录 PS 提交、PL 计算、DMA 输入、DMA 尾部、端到端、FPS；
另跑 300 帧连续切换稳定性测试。同一输入样本的三种输出图单独保存，
供 B 比较分割效果。

运行位置：PYNQ-Z2 板端 Jupyter / 命令行。需要把以下文件放在同一目录：
    board_test.py
    scene_policy.py
    integrated_pipeline_validated.bit
    integrated_pipeline_validated.hwh

如果板上仍是旧的 configurable_pipeline.bit/.hwh，也可以直接跑：
    %run board_test.py --bit configurable_pipeline.bit
两版的前 8 个配置寄存器偏移完全一致；DMA 基地址不同，但脚本从 hwh
动态读取，不写死地址。

板端用法：
    %run board_test.py
或
    sudo python3 board_test.py --frames 30 --stability-frames 300

真实图片测试（推荐；目录下的图会逐张跑三套配置，输出图按图名分开保存）：
    %run board_test.py --images data/images/seed --frames 30
单张真实图：
    %run board_test.py --image data/images/seed/060.jpg --frames 30

产物（默认写到脚本所在目录，拷回仓库对应位置即可）：
    benchmark/scene_pipeline_modes.csv
    benchmark/scene_300frames.log
    benchmark/scene_ps_decision.csv
    benchmark/scene_summary.json
    demo/scene_pl_outputs/*.png

PC 上只能跑接口自检（没有 pynq）：
    python board_test.py --selftest
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
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import scene_policy as sp  # noqa: E402

# pynq 只在板端存在；PC 自检时延迟失败，但文件仍可 import
try:
    from pynq import Overlay, MMIO, allocate
    HAVE_PYNQ = True
except ImportError:          # pragma: no cover - 取决于运行环境
    Overlay = MMIO = allocate = None
    HAVE_PYNQ = False

# 优先使用队友最新的 integrated_pipeline_validated（额外带 ROI/前景计数寄存器）；
# 找不到时自动退回 system_validation 版。两版前 8 个配置寄存器偏移一致。
BIT_FILE = "integrated_pipeline_validated.bit"
FALLBACK_BITS = ["configurable_pipeline.bit"]
KERNEL_IP = "sobel_v4_pipeline_0"
DMA_IP = "axi_dma_0"

OFF_AP_CTRL = 0x00
AP_DONE = 0x2
MM2S_DMASR_OFF = 0x04
S2MM_DMASR_OFF = 0x34
DMASR_IOC = 0x1000
DMASR_INTERNAL_ERR = 0x1E000000
POLL_INTERVAL = 0.00005      # 0.05 ms

MIN_W, MAX_W = 3, 1920
MIN_H, MAX_H = 3, 1080

CONFIG_ORDER = ["normal", "low_contrast", "dense"]

# ----------------------------------------------------------------------
# 输入图像
# ----------------------------------------------------------------------
def _draw_disc(rgb, cy, cx, radius, color):
    h, w = rgb.shape[:2]
    y0, y1 = max(0, cy - radius), min(h, cy + radius + 1)
    x0, x1 = max(0, cx - radius), min(w, cx + radius + 1)
    if y0 >= y1 or x0 >= x1:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1]
    inside = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius * radius
    rgb[y0:y1, x0:x1][inside] = color

def make_synthetic_rgb(width, height):
    """生成一张同时含高对比、低对比、密集区域的测试图。

    这样三种配置在同一张输入上的差异肉眼可见：
      * normal 配置能保住高对比边缘，但会压掉低对比弱边；
      * low_contrast 配置能接上弱边，但噪声/纹理也会更多；
      * dense 配置阈值最高，适合把密集团块内部纹理压掉。
    """
    rgb = np.full((height, width, 3), 245, np.uint8)
    r = max(4, int(round(min(width, height) * 0.045)))
    dark = (55, 45, 35)
    faint = (188, 183, 176)     # 与 245 背景对比约 60

    # 高对比单粒
    for fx, fy in [(0.16, 0.22), (0.40, 0.18), (0.68, 0.24), (0.24, 0.52)]:
        _draw_disc(rgb, int(fy * height), int(fx * width), r, dark)

    # 低对比单粒
    for fx, fy in [(0.55, 0.50), (0.78, 0.56), (0.88, 0.30), (0.62, 0.78)]:
        _draw_disc(rgb, int(fy * height), int(fx * width), r, faint)

    # 密集区：5x4 小粒，部分紧挨
    small = max(3, int(round(r * 0.62)))
    for row in range(4):
        for col in range(5):
            cx = int((0.08 + col * 0.055) * width)
            cy = int((0.72 + row * 0.065) * height)
            _draw_disc(rgb, cy, cx, small, dark)
    return rgb

def _load_rgb(path):
    try:
        from PIL import Image
        with Image.open(path) as source:
            return np.array(source.convert("RGB"))
    except ImportError:
        pass
    try:
        import cv2
    except ImportError:
        raise RuntimeError("读图需要 Pillow 或 cv2：%s" % path)
    bgr = cv2.imread(path)
    if bgr is None:
        raise RuntimeError("读图失败：%s" % path)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

def fit_to_size(rgb, width, height, pad=255):
    """等比缩放到目标框内，再用白色补边到精确 width x height。"""
    src_h, src_w = rgb.shape[:2]
    scale = min(float(width) / src_w, float(height) / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    try:
        from PIL import Image
        resized = np.array(Image.fromarray(rgb).resize((new_w, new_h),
                                                       Image.LANCZOS))
    except ImportError:
        import cv2
        resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.full((height, width, 3), pad, np.uint8)
    top = (height - new_h) // 2
    left = (width - new_w) // 2
    canvas[top:top + new_h, left:left + new_w] = resized
    return canvas

def prepare_input(width, height, image_path=None):
    if image_path:
        rgb = _load_rgb(image_path)
        if rgb.shape[1] != width or rgb.shape[0] != height:
            rgb = fit_to_size(rgb, width, height)
        return rgb, "image:%s" % Path(image_path).name
    return make_synthetic_rgb(width, height), "synthetic:mixed"

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp")

def image_tag(image_path):
    """输出文件名前缀：真实图用文件名，合成图为空。"""
    return "" if not image_path else Path(image_path).stem + "_"

def source_label(source):
    """汇总表里的短输入名。"""
    if source.startswith("image:"):
        return Path(source[6:]).stem
    return "synth"

def find_input_path(name):
    """相对路径依次按当前目录、脚本目录查找；返回存在的 Path 或 None。"""
    candidate = Path(name)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    for base in (Path.cwd(), HERE):
        found = base / name
        if found.exists():
            return found
    return None

def _missing_message(name):
    return ("找不到图片：%s\n  当前目录：%s\n  脚本目录：%s\n"
            "  请检查文件名，或改用绝对路径" % (name, Path.cwd(), HERE))

def resolve_images(args):
    """把 --images（目录或逗号分隔列表）/ --image 解析成图片路径列表。

    返回空列表表示用合成测试图。--images 优先于 --image。
    相对路径先按当前目录找，再按脚本所在目录找（和 bit 的查找规则一致）。
    """
    items = []
    if args.images:
        raw = args.images.strip()
        found = find_input_path(raw)
        if found is not None and found.is_dir():
            items = [str(p) for p in sorted(Path(found).iterdir())
                     if p.suffix.lower() in IMAGE_SUFFIXES]
            if not items:
                raise SystemExit(
                    "目录里没有图片：%s\n  支持的后缀：%s"
                    % (found, ", ".join(IMAGE_SUFFIXES)))
        elif "," in raw or Path(raw).suffix.lower() in IMAGE_SUFFIXES:
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                hit = find_input_path(part)
                if hit is None or not hit.is_file():
                    raise SystemExit(_missing_message(part))
                items.append(str(hit))
        else:
            raise SystemExit(
                "找不到图片目录：%s\n  当前目录：%s\n  脚本目录：%s\n"
                "  如果图片就在当前目录，可写 --images .\n"
                "  也可以用绝对路径："
                "--images /home/xilinx/jupyter_notebooks/scene/images"
                % (raw, Path.cwd(), HERE))
    elif args.image:
        hit = find_input_path(args.image)
        if hit is None or not hit.is_file():
            raise SystemExit(_missing_message(args.image))
        items = [str(hit)]
    return items

def rgb_to_rgbx_words(rgb):
    """RGB -> MM2S 32bit RGBX888，bit[23:16]=R、[15:8]=G、[7:0]=B。"""
    a = np.asarray(rgb, dtype=np.uint32)
    words = ((a[:, :, 0] << 16) | (a[:, :, 1] << 8) | a[:, :, 2])
    return words.reshape(-1)

def save_png(path, gray):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        Image.fromarray(gray.astype(np.uint8), mode="L").save(str(path))
    except ImportError:
        import cv2
        cv2.imwrite(str(path), gray.astype(np.uint8))

def save_rgb_png(path, rgb):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(str(path))
    except ImportError:
        import cv2
        cv2.imwrite(str(path), cv2.cvtColor(rgb.astype(np.uint8),
                                           cv2.COLOR_RGB2BGR))

def sha256_bytes(array):
    return hashlib.sha256(array.tobytes()).hexdigest()

# ----------------------------------------------------------------------
# 板端驱动：带分项计时
# ----------------------------------------------------------------------
class V4Driver:
    def __init__(self, bitfile=BIT_FILE, kernel=KERNEL_IP, dma_name=DMA_IP):
        if not HAVE_PYNQ:
            raise RuntimeError("这个类只能在 PYNQ 板子上用（需要 pynq 包）")
        self.bitfile = str(bitfile)
        self.ol = Overlay(self.bitfile)
        missing = [name for name in (kernel, dma_name)
                   if name not in self.ol.ip_dict]
        if missing:
            raise RuntimeError("Overlay 里没有 %s；实际有 %s"
                               % (missing, sorted(self.ol.ip_dict)))
        self.kernel_base = self.ol.ip_dict[kernel]["phys_addr"]
        self.dma_base = self.ol.ip_dict[dma_name]["phys_addr"]
        self.ctrl = MMIO(self.kernel_base, 0x1000)
        self.dma_ctrl = MMIO(self.dma_base, 0x1000)
        self.dma = getattr(self.ol, dma_name)
        self.ap_done_seen = False   # 本进程是否见过 ap_done 被清零

    def info(self):
        return {
            "bitfile": self.bitfile,
            "ip_dict": sorted(self.ol.ip_dict.keys()),
            "kernel_base": hex(self.kernel_base),
            "dma_base": hex(self.dma_base),
            "input_format": "RGBX888 32bit，bit[23:16]=R，[15:8]=G，[7:0]=B",
        }

    def run_frame(self, inp_buf, out_buf, config, width, height, timeout):
        """跑一帧并返回分项计时。

        计时口径（见 data/evidence/fpga/scene_policy/register_map.md 第 5 节）：
            ps_submit : 第一个数据寄存器写 -> AP_CTRL=1
            pl_busy   : AP_CTRL=1 -> ap_done 置位（观察不到清零时记 None）
            dma_in    : AP_CTRL=1 -> MM2S IOC
            dma_tail  : ap_done -> S2MM IOC（观察不到 ap_done 时记 None）
            e2e       : 第一个数据寄存器写 -> S2MM IOC
        """
        if not (MIN_W <= width <= MAX_W and MIN_H <= height <= MAX_H):
            raise ValueError("分辨率 %dx%d 超出范围" % (width, height))

        # 清双通道完成位（写 1 清除）
        self.dma_ctrl.write(MM2S_DMASR_OFF, DMASR_IOC)
        self.dma_ctrl.write(S2MM_DMASR_OFF, DMASR_IOC)

        t0 = time.perf_counter()

        # 8 个数据寄存器：顺序与 apply_config 固定一致
        for offset, value in sp.apply_config(config, width=width, height=height):
            self.ctrl.write(offset, value)

        # 先挂 DMA，再拉 ap_start（segmentation 真板实测顺序）
        self.dma.recvchannel.transfer(out_buf)   # S2MM: width*height 字节
        self.dma.sendchannel.transfer(inp_buf)   # MM2S: width*height 拍

        self.ctrl.write(OFF_AP_CTRL, 1)
        t_start = time.perf_counter()

        cleared = False
        done_ts = None
        mm2s_ts = None
        s2mm_ts = None
        mm2s = s2mm = 0
        deadline = t_start + timeout

        while True:
            now = time.perf_counter()

            ctrl_val = self.ctrl.read(OFF_AP_CTRL)
            if not (ctrl_val & AP_DONE):
                cleared = True
                self.ap_done_seen = True
            elif cleared and done_ts is None:
                done_ts = now

            mm2s = self.dma_ctrl.read(MM2S_DMASR_OFF)
            s2mm = self.dma_ctrl.read(S2MM_DMASR_OFF)
            if mm2s_ts is None and (mm2s & DMASR_IOC):
                mm2s_ts = now
            if s2mm_ts is None and (s2mm & DMASR_IOC):
                s2mm_ts = now

            if mm2s_ts is not None and s2mm_ts is not None:
                break
            if now > deadline:
                raise TimeoutError(
                    "帧超时 %.1fs: MM2S_DMASR=%s S2MM_DMASR=%s AP_CTRL=%s"
                    % (timeout, hex(mm2s), hex(s2mm), hex(ctrl_val)))
            time.sleep(POLL_INTERVAL)

        dma_error = bool((mm2s & DMASR_INTERNAL_ERR)
                         or (s2mm & DMASR_INTERNAL_ERR))
        timing = {
            "ps_submit_ms": (t_start - t0) * 1000.0,
            "pl_busy_ms": ((done_ts - t_start) * 1000.0
                           if done_ts is not None else None),
            "dma_in_ms": (mm2s_ts - t_start) * 1000.0,
            "dma_tail_ms": ((s2mm_ts - done_ts) * 1000.0
                            if done_ts is not None else None),
            "e2e_ms": (s2mm_ts - t0) * 1000.0,
            "fps": 1000.0 / max(1e-9, (s2mm_ts - t0) * 1000.0),
            "mm2s_dmasr": mm2s,
            "s2mm_dmasr": s2mm,
            "dma_error": dma_error,
            "ap_done_seen": done_ts is not None,
        }
        return timing

# ----------------------------------------------------------------------
# 统计与输出
# ----------------------------------------------------------------------
def _ms(value):
    return "" if value is None else round(float(value), 4)

def median_p95(values):
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None, None
    arr = np.asarray(clean, dtype=np.float64)
    return float(np.median(arr)), float(np.percentile(arr, 95))

def measure_decision(rgb, repeats):
    """measure PS 场景判断耗时（compute_frame_stats + auto_config）。"""
    for _ in range(3):
        sp.auto_config(sp.compute_frame_stats(rgb))
    samples = []
    decision = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        stats = sp.compute_frame_stats(rgb)
        decision = sp.auto_config(stats)
        samples.append((time.perf_counter() - t0) * 1000.0)
    med, p95 = median_p95(samples)
    return {
        "scene": decision["scene"],
        "config_id": decision["config_id"],
        "reason": decision["decision"]["reason"],
        "stats": decision["decision"]["stats"],
        "repeats": repeats,
        "median_ms": med,
        "p95_ms": p95,
        "min_ms": min(samples),
        "max_ms": max(samples),
    }

def parse_resolutions(text):
    result = []
    for item in text.replace(" ", "").split(","):
        if not item:
            continue
        if "x" not in item:
            raise ValueError("分辨率格式应为 WxH：%s" % item)
        w, h = item.split("x", 1)
        result.append((int(w), int(h)))
    if not result:
        raise ValueError("至少给一个分辨率")
    return result

def write_summary(summary, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8")

def print_table(summary_rows):
    print("\n=== 三配置实测汇总（median）===")
    header = ("input", "resolution", "scene", "id", "n", "ps_submit",
              "pl_busy", "dma_in", "dma_tail", "e2e", "fps")
    print("%-8s %-10s %-13s %2s %4s %10s %9s %8s %9s %8s %7s"
          % header)
    for row in summary_rows:
        source = source_label(row.get("input_source", "synthetic:mixed"))
        if row.get("frames", 0) == 0 or row.get("error"):
            print("%-8s %-10s %-13s %2d  FAIL  %s"
                  % (source, row["resolution"], row["scene"],
                     row["config_id"], row.get("error", "no frames")))
            continue
        print("%-8s %-10s %-13s %2d %4d %10s %9s %8s %9s %8s %7.1f"
              % (source, row["resolution"], row["scene"],
                 row["config_id"], row["frames"],
                 "%.3f" % row["ps_submit_ms"],
                 "N/A" if row["pl_busy_ms"] is None else "%.3f" % row["pl_busy_ms"],
                 "%.3f" % row["dma_in_ms"],
                 "N/A" if row["dma_tail_ms"] is None else "%.3f" % row["dma_tail_ms"],
                 "%.3f" % row["e2e_ms"], row["fps"]))

# ----------------------------------------------------------------------
# 测试主流程
# ----------------------------------------------------------------------
def get_buffers(cache, width, height):
    """按分辨率复用 DMA 缓冲区，避免多张图反复 allocate 造成内存碎片。"""
    key = (width, height)
    if key not in cache:
        cache[key] = (allocate(shape=(width * height,), dtype=np.uint32),
                      allocate(shape=(width * height,), dtype=np.uint8))
    return cache[key]

def run_timing(driver, rows, image_path, resolutions, frames, warmup,
               decision_repeats, out_root, timeout, summary_rows, ps_rows,
               buffers=None):
    if buffers is None:
        buffers = {}
    for width, height in resolutions:
        rgb, source = prepare_input(width, height, image_path)
        words = rgb_to_rgbx_words(rgb)
        inp, out = get_buffers(buffers, width, height)
        inp[:] = words
        if hasattr(inp, "flush"):
            inp.flush()

        input_hash = sha256_bytes(rgb)
        stats = sp.compute_frame_stats(rgb)
        auto_scene, auto_reason = sp.explain_scene(stats)
        decision = measure_decision(rgb, decision_repeats)

        print("\n--- %dx%d  输入=%s ---" % (width, height, source))
        print("    PS 场景判断: %s (config %d)  %s"
              % (auto_scene, sp.CONFIG_TABLE[auto_scene]["config_id"],
                 auto_reason))
        print("    PS 决策耗时: median=%.2f ms  p95=%.2f ms"
              % (decision["median_ms"], decision["p95_ms"]))
        ps_rows.append({
            "resolution": "%dx%d" % (width, height),
            "width": width,
            "height": height,
            "input_source": source,
            "stats_scene": auto_scene,
            "stats_config_id": sp.CONFIG_TABLE[auto_scene]["config_id"],
            "stats_reason": auto_reason,
            "contrast": round(stats["contrast"], 3),
            "foreground_ratio": round(stats["foreground_ratio"], 6),
            "component_count": stats["component_count"],
            "largest_component_ratio": round(
                stats["largest_component_ratio"], 6),
            "repeats": decision["repeats"],
            "median_ms": round(decision["median_ms"], 4),
            "p95_ms": round(decision["p95_ms"], 4),
            "min_ms": round(decision["min_ms"], 4),
            "max_ms": round(decision["max_ms"], 4),
        })

        demo_dir = Path(out_root) / "data/evidence/demo/pipeline_outputs"
        tag = image_tag(image_path)
        save_rgb_png(demo_dir / ("%dx%d_%sinput.png"
                                 % (width, height, tag)), rgb)

        for scene in CONFIG_ORDER:
            config = dict(sp.CONFIG_TABLE[scene])
            print("    [%s] config_id=%d thr=%d morph=%d"
                  % (scene, config["config_id"], config["threshold"],
                     config["morph_mode"]))

            saved_hashes = {}
            try:
                for _ in range(max(0, warmup)):
                    driver.run_frame(inp, out, config, width, height, timeout)

                scene_rows = []
                for frame_id in range(frames):
                    timing = driver.run_frame(inp, out, config, width, height,
                                              timeout)
                    if hasattr(out, "invalidate"):
                        out.invalidate()
                    result = np.array(out).reshape(height, width)
                    out_hash = sha256_bytes(result)

                    if frame_id == 0:
                        save_png(demo_dir / ("%dx%d_%s%s_cfg%d.png"
                                             % (width, height, tag, scene,
                                                config["config_id"])),
                                 result)

                    saved_hashes.setdefault(out_hash, 0)
                    saved_hashes[out_hash] += 1

                    row = {
                        "phase": "timing",
                        "resolution": "%dx%d" % (width, height),
                        "width": width,
                        "height": height,
                        "scene": scene,
                        "config_id": config["config_id"],
                        "frame_id": frame_id,
                        "input_source": source,
                        "input_sha256": input_hash,
                        "output_sha256": out_hash,
                        "ps_submit_ms": _ms(timing["ps_submit_ms"]),
                        "pl_busy_ms": _ms(timing["pl_busy_ms"]),
                        "dma_in_ms": _ms(timing["dma_in_ms"]),
                        "dma_tail_ms": _ms(timing["dma_tail_ms"]),
                        "e2e_ms": _ms(timing["e2e_ms"]),
                        "fps": round(timing["fps"], 2),
                        "ps_decision_ms": "",
                        "mm2s_dmasr": hex(timing["mm2s_dmasr"]),
                        "s2mm_dmasr": hex(timing["s2mm_dmasr"]),
                        "dma_error": int(timing["dma_error"]),
                        "ap_done_seen": int(timing["ap_done_seen"]),
                    }
                    rows.append(row)
                    scene_rows.append(timing)

                if saved_hashes:
                    print("      输出一致性: %d 种哈希 / %d 帧%s"
                          % (len(saved_hashes), frames,
                             "（一致）" if len(saved_hashes) == 1 else "（不一致！）"))
                summary_rows.append(make_summary_row(
                    "%dx%d" % (width, height), scene, config["config_id"],
                    frames, scene_rows, len(saved_hashes), source))
            except Exception as exc:
                print("      [失败] %s" % exc)
                summary_rows.append({
                    "resolution": "%dx%d" % (width, height),
                    "input_source": source,
                    "scene": scene,
                    "config_id": config["config_id"],
                    "frames": 0,
                    "ps_submit_ms": None,
                    "pl_busy_ms": None,
                    "dma_in_ms": None,
                    "dma_tail_ms": None,
                    "e2e_ms": None,
                    "fps": 0.0,
                    "hash_groups": 0,
                    "error": str(exc),
                })
        # 缓冲区留在 buffers 里复用，跨图片/跨分辨率共享

def make_summary_row(resolution, scene, config_id, frames, timings,
                     hash_groups, source="synthetic:mixed"):
    def med(key):
        return median_p95([item[key] for item in timings])[0]

    def p95(key):
        return median_p95([item[key] for item in timings])[1]

    e2e = [item["e2e_ms"] for item in timings]
    return {
        "resolution": resolution,
        "input_source": source,
        "scene": scene,
        "config_id": config_id,
        "frames": frames,
        "ps_submit_ms": round(med("ps_submit_ms"), 4),
        "ps_submit_p95_ms": round(p95("ps_submit_ms"), 4),
        "pl_busy_ms": (None if med("pl_busy_ms") is None
                       else round(med("pl_busy_ms"), 4)),
        "pl_busy_p95_ms": (None if p95("pl_busy_ms") is None
                           else round(p95("pl_busy_ms"), 4)),
        "dma_in_ms": round(med("dma_in_ms"), 4),
        "dma_in_p95_ms": round(p95("dma_in_ms"), 4),
        "dma_tail_ms": (None if med("dma_tail_ms") is None
                        else round(med("dma_tail_ms"), 4)),
        "dma_tail_p95_ms": (None if p95("dma_tail_ms") is None
                            else round(p95("dma_tail_ms"), 4)),
        "e2e_ms": round(med("e2e_ms"), 4),
        "e2e_p95_ms": round(p95("e2e_ms"), 4),
        "e2e_min_ms": round(min(e2e), 4),
        "e2e_max_ms": round(max(e2e), 4),
        "fps": round(1000.0 / max(1e-9, med("e2e_ms")), 2),
        "hash_groups": hash_groups,
        "dma_error": int(any(item["dma_error"] for item in timings)),
    }

def run_stability(driver, rows, width, height, total_frames, configs,
                  switch_every, out_root, timeout, log_path, image_path=None,
                  buffers=None):
    """连续帧稳定性：按 configs 轮换，每 switch_every 帧切一次配置。"""
    rgb, source = prepare_input(width, height, image_path)
    input_hash = sha256_bytes(rgb)
    words = rgb_to_rgbx_words(rgb)
    inp, out = get_buffers(buffers if buffers is not None else {},
                           width, height)
    inp[:] = words
    if hasattr(inp, "flush"):
        inp.flush()

    demo_dir = Path(out_root) / "data/evidence/demo/pipeline_outputs"
    hashes = {}
    timeouts = 0
    completed = 0
    mismatch = 0
    started = time.time()

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("scene A 300-frame stability log\n")
        handle.write("start=%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        handle.write("resolution=%dx%d input=%s\n" % (width, height, source))
        handle.write("config_cycle=%s switch_every=%d total=%d\n"
                     % (",".join(configs), switch_every, total_frames))
        handle.write("frame_id,config_id,scene,e2e_ms,dma_in_ms,dma_tail_ms,"
                     "mm2s_dmasr,s2mm_dmasr,output_sha256,status\n")
        handle.flush()

        for frame_id in range(total_frames):
            scene = configs[(frame_id // max(1, switch_every)) % len(configs)]
            config = dict(sp.CONFIG_TABLE[scene])
            try:
                timing = driver.run_frame(inp, out, config, width, height,
                                          timeout)
                if hasattr(out, "invalidate"):
                    out.invalidate()
                result = np.array(out).reshape(height, width)
                out_hash = sha256_bytes(result)

                first_hash = hashes.setdefault(scene, out_hash)
                if out_hash != first_hash:
                    mismatch += 1
                completed += 1

                if frame_id in (0, 100, 200, total_frames - 1):
                    save_png(demo_dir / ("stability_%dx%d_%sf%03d_%s.png"
                                         % (width, height,
                                            image_tag(image_path),
                                            frame_id, scene)),
                             result)

                rows.append({
                    "phase": "stability",
                    "resolution": "%dx%d" % (width, height),
                    "width": width,
                    "height": height,
                    "scene": scene,
                    "config_id": config["config_id"],
                    "frame_id": frame_id,
                    "input_source": source,
                    "input_sha256": input_hash,
                    "output_sha256": out_hash,
                    "ps_submit_ms": _ms(timing["ps_submit_ms"]),
                    "pl_busy_ms": _ms(timing["pl_busy_ms"]),
                    "dma_in_ms": _ms(timing["dma_in_ms"]),
                    "dma_tail_ms": _ms(timing["dma_tail_ms"]),
                    "e2e_ms": _ms(timing["e2e_ms"]),
                    "fps": round(timing["fps"], 2),
                    "ps_decision_ms": "",
                    "mm2s_dmasr": hex(timing["mm2s_dmasr"]),
                    "s2mm_dmasr": hex(timing["s2mm_dmasr"]),
                    "dma_error": int(timing["dma_error"]),
                    "ap_done_seen": int(timing["ap_done_seen"]),
                })

                handle.write("%d,%d,%s,%.4f,%.4f,%s,%s,%s,%s,OK\n"
                             % (frame_id, config["config_id"], scene,
                                timing["e2e_ms"], timing["dma_in_ms"],
                                "" if timing["dma_tail_ms"] is None
                                else "%.4f" % timing["dma_tail_ms"],
                                hex(timing["mm2s_dmasr"]),
                                hex(timing["s2mm_dmasr"]),
                                out_hash))
                if frame_id % 25 == 0:
                    handle.flush()
                    print("    [stability] %d/%d 帧  scene=%s  e2e=%.3f ms"
                          % (frame_id, total_frames, scene,
                             timing["e2e_ms"]))
            except Exception as exc:
                timeouts += 1
                handle.write("%d,%d,%s,,,,,,,TIMEOUT:%s\n"
                             % (frame_id, config["config_id"], scene, exc))
                handle.flush()
                print("    [stability][失败] frame %d: %s" % (frame_id, exc))
                break

        elapsed = time.time() - started
        handle.write("SUMMARY completed=%d/%d timeouts=%d hash_mismatches=%d "
                     "deadlock=%s elapsed_s=%.2f\n"
                     % (completed, total_frames, timeouts, mismatch,
                        "YES" if timeouts else "NO", elapsed))
        handle.flush()

    print("\n--- 300 帧稳定性（%dx%d）---" % (width, height))
    print("    完成 %d/%d 帧，超时 %d，哈希不一致 %d，用时 %.1f s"
          % (completed, total_frames, timeouts, mismatch, elapsed))
    return {
        "resolution": "%dx%d" % (width, height),
        "requested": total_frames,
        "completed": completed,
        "timeouts": timeouts,
        "hash_mismatches": mismatch,
        "ok": completed == total_frames and timeouts == 0 and mismatch == 0,
    }

# ----------------------------------------------------------------------
# 自检（PC，无需板卡）
# ----------------------------------------------------------------------
def selftest():
    ok = True
    for width, height in [(320, 240), (640, 480)]:
        rgb = make_synthetic_rgb(width, height)
        words = rgb_to_rgbx_words(rgb)
        good = (rgb.shape == (height, width, 3) and words.shape == (width * height,)
                and int(words[0] & 0xFF) == int(rgb[0, 0, 2]))
        stats = sp.compute_frame_stats(rgb)
        cfg = sp.auto_config(stats)
        regs = sp.apply_config(cfg, width=width, height=height)
        good = good and len(regs) == 8 and regs[0] == (sp.OFF_WIDTH, width)
        print("%dx%d: words=%d stats_scene=%s config_id=%d regs=%d %s"
              % (width, height, words.size, cfg["scene"], cfg["config_id"],
                 len(regs), "PASS" if good else "FAIL"))
        ok = ok and good

    parsed = parse_resolutions("320x240,640x480")
    print("parse_resolutions ->", parsed)
    ok = ok and parsed == [(320, 240), (640, 480)]

    fitted = fit_to_size(np.zeros((40, 60, 3), np.uint8), 320, 240)
    fit_ok = fitted.shape == (240, 320, 3) and int(fitted[0, 0, 0]) == 255
    print("fit_to_size ->", fitted.shape, "PASS" if fit_ok else "FAIL")
    ok = ok and fit_ok

    tag_ok = (image_tag(None) == ""
              and image_tag(str(Path("a") / "b/015.jpg")) == "015_")
    print("image_tag ->", "PASS" if tag_ok else "FAIL")
    ok = ok and tag_ok

    label_ok = (source_label("image:015.jpg") == "015"
                and source_label("synthetic:mixed") == "synth")
    print("source_label ->", "PASS" if label_ok else "FAIL")
    ok = ok and label_ok

    for name in CONFIG_ORDER:
        if name not in sp.CONFIG_TABLE:
            ok = False
            print("缺少配置:", name)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1

# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def resolve_bit(path=None):
    """按 --bit、当前目录、脚本目录、仓库 board/overlay 的顺序找 bit。"""
    names = [path] if path else [BIT_FILE] + FALLBACK_BITS
    bases = [Path.cwd(), HERE, HERE.parent.parent / "hardware/drivers"]
    tried = []
    for name in names:
        candidate = Path(name)
        if candidate.is_absolute():
            if candidate.exists():
                return candidate
            tried.append(str(candidate))
            continue
        for base in bases:
            candidate = base / name
            tried.append(str(candidate))
            if candidate.exists():
                return candidate
    raise FileNotFoundError(
        "找不到 bit 文件；试过：%s。请用 --bit 指定完整路径。" % tried)

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="scene A：PYNQ-Z2 三配置实测（320x240 / 640x480）")
    parser.add_argument("--bit", default=None,
                        help="overlay bit 文件；缺省先找 integrated_pipeline_validated.bit，"
                             "再找 configurable_pipeline.bit")
    parser.add_argument("--image", default=None,
                        help="可选：用一张真实图做统一输入；缺省用合成测试图")
    parser.add_argument("--images", default=None,
                        help="可选：真实图目录或逗号分隔的图片列表；"
                             "每张图都会跑三套配置，输出图按图名分开保存")
    parser.add_argument("--resolutions", default="320x240,640x480",
                        help="逗号分隔，默认 320x240,640x480")
    parser.add_argument("--frames", type=int, default=30,
                        help="每个配置的计时帧数，默认 30")
    parser.add_argument("--warmup", type=int, default=2,
                        help="每个配置计时前的预热帧数，默认 2")
    parser.add_argument("--stability-frames", type=int, default=300,
                        help="连续稳定性帧数，默认 300")
    parser.add_argument("--stability-resolution", default="640x480",
                        help="稳定性测试分辨率，默认 640x480")
    parser.add_argument("--switch-every", type=int, default=10,
                        help="稳定性测试每多少帧切换一次配置，默认 10")
    parser.add_argument("--decision-repeats", type=int, default=30,
                        help="PS 场景判断重复次数，默认 30")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="单帧超时秒数，默认 5.0")
    parser.add_argument("--out-root", default=str(HERE),
                        help="产物根目录，默认脚本所在目录")
    parser.add_argument("--skip-stability", action="store_true",
                        help="跳过 300 帧稳定性测试")
    parser.add_argument("--selftest", action="store_true",
                        help="PC 接口自检，不需要板卡")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if not HAVE_PYNQ:
        print("没有找到 pynq 包：本脚本要在 PYNQ-Z2 板端运行。")
        print("PC 上可先跑：python board_test.py --selftest")
        return 2

    resolutions = parse_resolutions(args.resolutions)
    stability_w, stability_h = parse_resolutions(args.stability_resolution)[0]

    out_root = Path(args.out_root)
    bench_dir = out_root / "benchmark"
    bench_dir.mkdir(parents=True, exist_ok=True)
    csv_path = bench_dir / "scene_pipeline_modes.csv"
    log_path = bench_dir / "scene_300frames.log"
    ps_path = bench_dir / "scene_ps_decision.csv"
    summary_path = bench_dir / "scene_summary.json"

    print("=== scene A 板端测试 ===")
    driver = V4Driver(resolve_bit(args.bit))
    print("Overlay 信息：")
    for key, value in driver.info().items():
        print("    %s: %s" % (key, value))
    print("输出根目录：%s" % out_root)

    image_list = resolve_images(args)
    if image_list:
        print("真实图片：%d 张（%s）"
              % (len(image_list),
                 ", ".join(Path(item).name for item in image_list)))

    rows = []
    summary_rows = []
    ps_rows = []
    buffers = {}

    for image_path in image_list or [None]:
        run_timing(driver, rows, image_path, resolutions, args.frames,
                   args.warmup, args.decision_repeats, out_root, args.timeout,
                   summary_rows, ps_rows, buffers=buffers)

    stability = None
    stability_image = args.image or (image_list[0] if image_list else None)
    if not args.skip_stability and args.stability_frames > 0:
        stability = run_stability(
            driver, rows, stability_w, stability_h, args.stability_frames,
            CONFIG_ORDER, args.switch_every, out_root, args.timeout, log_path,
            image_path=stability_image, buffers=buffers)

    # ---- CSV ----
    fields = ["phase", "resolution", "width", "height", "scene", "config_id",
              "frame_id", "input_source", "input_sha256", "output_sha256",
              "ps_submit_ms", "pl_busy_ms", "dma_in_ms", "dma_tail_ms",
              "e2e_ms", "fps", "ps_decision_ms", "mm2s_dmasr", "s2mm_dmasr",
              "dma_error", "ap_done_seen"]
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print("\n已写入 %s（%d 行）" % (csv_path, len(rows)))

    ps_fields = ["resolution", "width", "height", "input_source", "stats_scene",
                 "stats_config_id", "stats_reason", "contrast",
                 "foreground_ratio", "component_count",
                 "largest_component_ratio", "repeats", "median_ms", "p95_ms",
                 "min_ms", "max_ms"]
    with open(ps_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ps_fields)
        writer.writeheader()
        writer.writerows(ps_rows)
    print("已写入 %s" % ps_path)

    print_table(summary_rows)

    # ---- 验收判定 ----
    timing_ok = all(row.get("frames", 0) >= args.frames
                    and not row.get("error")
                    and row.get("dma_error", 0) == 0
                    and row.get("hash_groups", 1) == 1
                    for row in summary_rows)
    stability_ok = (stability is None) or stability["ok"]
    pl_busy_ok = all(row.get("pl_busy_ms") is not None
                     for row in summary_rows if row.get("frames", 0) > 0)
    passed = timing_ok and stability_ok

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "overlay": driver.info(),
        "settings": {
            "resolutions": ["%dx%d" % item for item in resolutions],
            "frames_per_config": args.frames,
            "warmup": args.warmup,
            "configs": CONFIG_ORDER,
            "inputs": image_list or ["synthetic"],
            "stability_frames": args.stability_frames,
            "stability_resolution": "%dx%d" % (stability_w, stability_h),
            "stability_input": stability_image or "synthetic",
            "switch_every": args.switch_every,
            "timeout_s": args.timeout,
        },
        "configs": {name: sp.CONFIG_TABLE[name] for name in CONFIG_ORDER},
        "ps_decision": ps_rows,
        "timing_summary": summary_rows,
        "stability": stability,
        "checks": {
            "timing_complete": timing_ok,
            "stability_ok": stability_ok,
            "pl_busy_observed": pl_busy_ok,
            "overall": passed,
        },
    }
    write_summary(summary, summary_path)
    print("已写入 %s" % summary_path)

    if not pl_busy_ok:
        print("\n注意：未观察到 ap_done 清零，pl_busy/dma_tail 在 CSV 中留空。")
        print("      MM2S/S2MM IOC 与 e2e 计时仍然有效；请把该现象写进日志。")

    print("\n=== 判定：%s ===" % ("PASS" if passed else "FAIL"))
    return 0 if passed else 1

if __name__ == "__main__":
    sys.exit(main())
