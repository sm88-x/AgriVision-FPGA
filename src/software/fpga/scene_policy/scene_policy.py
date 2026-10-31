#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PS 侧场景判断与 V4 动态配置策略。

设计约束：
  * 只复用 V4 已有的 AXI-Lite 数据寄存器，不改硬件、不重做 segmentation 性能复测；
  * PS 侧只算低成本特征：灰度对比度、前景占比、连通域密度；
  * 普通 / 低对比度 / 密集 三类配置；
  * auto_config(frame_stats) 给自动模式，manual_config() 给可重复的手动固定配置；
  * apply_config() 只写数据寄存器，不碰 AP_CTRL —— 调用方必须在一帧结束
    （ap_done + 两个 DMA 通道 IOC 都已置起）之后、下一帧 ap_start 之前调用。

本文件不 import pynq，可在 PC 上直接跑自检：
    python scene_policy.py --selftest
    python scene_policy.py --images ../../data/images/seed/*.jpg
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
import os
import sys

import numpy as np

try:                      # 板端有 cv2；PC 上没有时退回纯 numpy 实现
    import cv2
except ImportError:       # pragma: no cover - 取决于运行环境
    cv2 = None

POLICY_VERSION = "scene-a-v1"

# ---- V4 数据寄存器偏移（与 build/reports/v4-pipeline/register_map_*.md 一致）----
OFF_WIDTH = 0x10
OFF_HEIGHT = 0x18
OFF_EDGE_MODE = 0x20      # 寄存器名 mode：0=Sobel，1=Prewitt
OFF_THRESHOLD = 0x28
OFF_MORPH = 0x30          # 0=Bypass，1=Erode，2=Dilate
OFF_EN_GRAY = 0x38
OFF_EN_GAUSS = 0x40
OFF_OUT_SEL = 0x48        # 0=最终 morph（默认）

MODE_SOBEL = 0
MODE_PREWITT = 1
MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE = 0, 1, 2

# ---- 三类场景的初始配置 ----
# manual 用 phenotyping 任务书点名的 Seed Preset（Gaussian + Sobel + thr40 + Dilate），
# 保证手动模式每次结果可重复。
CONFIG_TABLE = {
    "manual": {
        "config_id": 0,
        "scene": "manual",
        "enable_gray": 1,
        "enable_gauss": 1,
        "edge_mode": MODE_SOBEL,
        "threshold": 40,
        "morph_mode": MORPH_DILATE,
        "out_sel": 0,
    },
    "normal": {
        "config_id": 1,
        "scene": "normal",
        # 稀疏、对比度足够的种子：Gaussian 去噪 + 较高阈值 + 腐蚀去掉孤立杂边
        "enable_gray": 1,
        "enable_gauss": 1,
        "edge_mode": MODE_SOBEL,
        "threshold": 80,
        "morph_mode": MORPH_ERODE,
        "out_sel": 0,
    },
    "low_contrast": {
        "config_id": 2,
        "scene": "low_contrast",
        # 弱边缘：降低阈值把边接上，再用 Dilate 连接断边
        "enable_gray": 1,
        "enable_gauss": 1,
        "edge_mode": MODE_SOBEL,
        "threshold": 30,
        "morph_mode": MORPH_DILATE,
        "out_sel": 0,
    },
    "dense": {
        "config_id": 3,
        "scene": "dense",
        # 密集/接触：提高阈值压掉种皮纹理，腐蚀让粘连处变细，便于 B 分离
        "enable_gray": 1,
        "enable_gauss": 1,
        "edge_mode": MODE_SOBEL,
        "threshold": 120,
        "morph_mode": MORPH_ERODE,
        "out_sel": 0,
    },
}

# 分类阈值：先用 C 手上 7 张真实种子图定标（详见 register_map.md 的表格），
# 因样本有限，下午上板后允许按 B 的分割结果调整，但必须把新阈值写回本表。
DEFAULT_POLICY = {
    "low_contrast_max": 50.0,       # p95-p5 < 50 → 低对比度
    "dense_fg_ratio_min": 0.15,     # 前景占比 >= 15% → 密集
    "dense_components_min": 20,     # 连通域数 >= 20 → 密集
    "dense_largest_ratio_min": 0.60,
    "dense_largest_fg_min": 0.10,
    "sample_max_w": 160,            # 特征统计统一降到 <=160x120，保证 PS 开销低
    "sample_max_h": 120,
    "min_component_frac": 0.001,    # 小于采样面积 0.1% 的连通域当噪声
}

VALID_EDGE_MODES = (MODE_SOBEL, MODE_PREWITT)
VALID_MORPH_MODES = (MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE)
VALID_OUT_SEL = (0, 1, 2, 3)
MIN_W, MAX_W = 3, 1920
MIN_H, MAX_H = 3, 1080
MIN_THRESHOLD, MAX_THRESHOLD = 0, 4095

def _as_rgb(array):
    """接受 HxWx3 uint8（RGB）或 HxW（灰度），统一成 HxWx3 uint8。"""
    a = np.asarray(array)
    if a.ndim == 2:
        return np.stack([a, a, a], axis=2).astype(np.uint8)
    if a.ndim == 3 and a.shape[2] >= 3:
        return a[:, :, :3].astype(np.uint8)
    raise ValueError("需要 HxW 或 HxWx3 图像，收到 shape=%r" % (a.shape,))

def rgb_to_gray(rgb):
    """与 PL 一致的定点灰度：Y = (77R + 150G + 29B + 128) >> 8。"""
    a = _as_rgb(rgb).astype(np.int32)
    y = (77 * a[:, :, 0] + 150 * a[:, :, 1] + 29 * a[:, :, 2] + 128) >> 8
    return np.clip(y, 0, 255).astype(np.uint8)

def _downsample(gray, policy):
    """降到 <=160x120；板端有 cv2 用 INTER_AREA，PC 无 cv2 时用等间隔抽样。"""
    h, w = gray.shape
    scale = min(1.0, float(policy["sample_max_w"]) / w,
                float(policy["sample_max_h"]) / h)
    if scale >= 1.0:
        return gray
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    if cv2 is not None:
        return cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)
    step_y = max(1, int(round(float(h) / nh)))
    step_x = max(1, int(round(float(w) / nw)))
    return gray[::step_y, ::step_x]

def _otsu_threshold(gray):
    """纯 numpy Otsu；返回使类间方差最大的阈值 t，前景按 gray > t 定义。"""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 0
    levels = np.arange(256, dtype=np.float64)
    sum_all = float(np.dot(levels, hist))
    weight_bg = 0.0
    sum_bg = 0.0
    best_t = 0
    best_var = -1.0
    for t in range(256):
        weight_bg += hist[t]
        if weight_bg <= 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg <= 0:
            break
        sum_bg += t * hist[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        var = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var > best_var:
            best_var = var
            best_t = t
    return int(best_t)

def _label_areas_py(mask):
    """8 连通域面积（无 cv2 时的纯 Python 兜底；只用于 PC 自检）。"""
    h, w = mask.shape
    seen = np.zeros((h, w), dtype=np.bool_)
    areas = []
    for y0 in range(h):
        row = mask[y0]
        for x0 in range(w):
            if not row[x0] or seen[y0, x0]:
                continue
            stack = [(y0, x0)]
            seen[y0, x0] = True
            area = 0
            while stack:
                y, x = stack.pop()
                area += 1
                y1, y2 = max(0, y - 1), min(h, y + 2)
                x1, x2 = max(0, x - 1), min(w, x + 2)
                for ny in range(y1, y2):
                    for nx in range(x1, x2):
                        if mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            areas.append(area)
    return np.asarray(areas, dtype=np.int64)

def _component_areas(mask):
    if cv2 is not None:
        _, _, stats, _ = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=8)
        return stats[1:, cv2.CC_STAT_AREA].astype(np.int64)
    return _label_areas_py(mask)

def compute_frame_stats(rgb, policy=None):
    """计算场景特征（PS 侧、低成本）。

    返回字段：
        contrast              灰度 p95-p5（0..255）
        gray_std              灰度标准差
        otsu_threshold        Otsu 阈值
        polarity              "dark"（深色目标/亮背景）或 "bright"
        foreground_ratio      前景像素占采样图比例 0..1
        component_count       面积 >= 采样面积 0.1% 的连通域个数
        component_density     每 1000 个采样像素的连通域数
        largest_component_ratio  最大连通域 / 全部前景像素
        sample_size           [w,h] 采样尺寸
    """
    p = dict(DEFAULT_POLICY)
    if policy:
        p.update(policy)

    rgb = _as_rgb(rgb)
    gray_full = rgb_to_gray(rgb)
    gray = _downsample(gray_full, p)

    lo, hi = np.percentile(gray, [5, 95])
    contrast = float(hi - lo)
    threshold = _otsu_threshold(gray)

    # 用边框像素估计背景亮度，避免“目标超过画面一半时少数类变成背景”
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    bg_level = float(np.median(border))
    if bg_level >= threshold:
        mask = gray < threshold       # 亮背景、深色目标（种子图常见）
        polarity = "dark"
    else:
        mask = gray > threshold       # 深背景、亮色目标
        polarity = "bright"

    fg_ratio = float(mask.mean())
    # 极端比例说明边框估计失效，退回“少数类=前景”
    if fg_ratio > 0.90 or fg_ratio < 0.005:
        bright = gray > threshold
        if bright.mean() <= (1.0 - bright.mean()):
            mask, polarity = bright, "bright"
        else:
            mask, polarity = ~bright, "dark"
        fg_ratio = float(mask.mean())

    areas = _component_areas(mask)
    min_area = max(6, int(round(p["min_component_frac"] * mask.size)))
    big = areas[areas >= min_area]
    count = int(big.size)
    fg_px = int(mask.sum())
    largest = int(areas.max()) if areas.size else 0

    return {
        "width": int(rgb.shape[1]),
        "height": int(rgb.shape[0]),
        "sample_size": [int(gray.shape[1]), int(gray.shape[0])],
        "gray_mean": float(gray.mean()),
        "gray_std": float(gray.std()),
        "contrast": contrast,
        "otsu_threshold": int(threshold),
        "background_level": bg_level,
        "polarity": polarity,
        "foreground_ratio": fg_ratio,
        "foreground_px": fg_px,
        "component_count": count,
        "component_density": float(count) / max(1e-9, mask.size / 1000.0),
        "largest_component_ratio": float(largest) / max(1, fg_px),
    }

def explain_scene(stats, policy=None):
    """返回 (scene, reason)。分类顺序：低对比度优先，其次密集，否则普通。"""
    p = dict(DEFAULT_POLICY)
    if policy:
        p.update(policy)

    if stats["contrast"] < p["low_contrast_max"]:
        return "low_contrast", "contrast %.1f < %.1f" % (
            stats["contrast"], p["low_contrast_max"])

    dense_reasons = []
    if stats["foreground_ratio"] >= p["dense_fg_ratio_min"]:
        dense_reasons.append("fg %.3f >= %.2f" % (
            stats["foreground_ratio"], p["dense_fg_ratio_min"]))
    if stats["component_count"] >= p["dense_components_min"]:
        dense_reasons.append("components %d >= %d" % (
            stats["component_count"], p["dense_components_min"]))
    if (stats["largest_component_ratio"] >= p["dense_largest_ratio_min"]
            and stats["foreground_ratio"] >= p["dense_largest_fg_min"]):
        dense_reasons.append("largest %.2f >= %.2f and fg %.3f >= %.2f" % (
            stats["largest_component_ratio"], p["dense_largest_ratio_min"],
            stats["foreground_ratio"], p["dense_largest_fg_min"]))
    if dense_reasons:
        return "dense", "; ".join(dense_reasons)

    return "normal", ("contrast %.1f, fg %.3f, components %d, largest %.2f"
                      % (stats["contrast"], stats["foreground_ratio"],
                         stats["component_count"],
                         stats["largest_component_ratio"]))

def classify_scene(stats, policy=None):
    return explain_scene(stats, policy)[0]

def auto_config(frame_stats, policy=None):
    """自动模式入口：特征 dict（或直接传图像数组）→ 配置 dict。

    返回的 dict 可直接交给 apply_config；额外带 decision 字段供日志/界面展示。
    """
    if isinstance(frame_stats, np.ndarray):
        frame_stats = compute_frame_stats(frame_stats, policy)
    scene, reason = explain_scene(frame_stats, policy)
    config = dict(CONFIG_TABLE[scene])
    config["decision"] = {
        "policy_version": POLICY_VERSION,
        "scene": scene,
        "reason": reason,
        "stats": dict(frame_stats),
    }
    return config

def manual_config():
    """手动模式：固定、可重复的配置（phenotyping Seed Preset）。"""
    return dict(CONFIG_TABLE["manual"])

def _check_int(name, value, lo, hi):
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, (int, np.integer)):
        raise ValueError("%s 必须是整数，收到 %r" % (name, value))
    value = int(value)
    if value < lo or value > hi:
        raise ValueError("%s 超出范围 [%d, %d]：%d" % (name, lo, hi, value))
    return value

def config_registers(config, width=None, height=None):
    """把配置 dict 变成 [(offset, value), ...]，顺序固定为 width..out_sel。"""
    w = width if width is not None else config.get("width")
    h = height if height is not None else config.get("height")
    if w is None or h is None:
        raise ValueError("config_registers 需要 width/height（参数或 config 内）")

    w = _check_int("width", w, MIN_W, MAX_W)
    h = _check_int("height", h, MIN_H, MAX_H)
    edge_mode = _check_int("edge_mode", config.get("edge_mode", MODE_SOBEL),
                           min(VALID_EDGE_MODES), max(VALID_EDGE_MODES))
    if edge_mode not in VALID_EDGE_MODES:
        raise ValueError("edge_mode 只能是 0(Sobel) 或 1(Prewitt)")
    threshold = _check_int("threshold", config.get("threshold", 0),
                           MIN_THRESHOLD, MAX_THRESHOLD)
    morph = _check_int("morph_mode", config.get("morph_mode", MORPH_BYPASS),
                       min(VALID_MORPH_MODES), max(VALID_MORPH_MODES))
    if morph not in VALID_MORPH_MODES:
        raise ValueError("morph_mode 只能是 0/1/2")
    en_gray = _check_int("enable_gray", config.get("enable_gray", 1), 0, 1)
    en_gauss = _check_int("enable_gauss", config.get("enable_gauss", 0), 0, 1)
    out_sel = _check_int("out_sel", config.get("out_sel", 0),
                         min(VALID_OUT_SEL), max(VALID_OUT_SEL))
    if out_sel not in VALID_OUT_SEL:
        raise ValueError("out_sel 只能是 0/1/2/3")

    return [
        (OFF_WIDTH, w),
        (OFF_HEIGHT, h),
        (OFF_EDGE_MODE, edge_mode),
        (OFF_THRESHOLD, threshold),
        (OFF_MORPH, morph),
        (OFF_EN_GRAY, en_gray),
        (OFF_EN_GAUSS, en_gauss),
        (OFF_OUT_SEL, out_sel),
    ]

def apply_config(config, write=None, width=None, height=None):
    """写 V4 数据寄存器。

    write: callable(offset, value)，例如板端驱动传 driver.write_reg；
           省略时只返回寄存器列表（PC 自检用），不产生副作用。
    调用时机：上一帧 ap_done 且两个 DMA 通道都 IOC 之后、下一帧 ap_start 之前。
    """
    regs = config_registers(config, width=width, height=height)
    if write is not None:
        for offset, value in regs:
            write(offset, value)
    return regs

# ----------------------------------------------------------------------
# 自检 / 命令行
# ----------------------------------------------------------------------
def _draw_discs(rgb, centers, radius, color):
    h, w = rgb.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    for cy, cx in centers:
        inside = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius * radius
        rgb[inside] = color

def _demo_images():
    h, w = 240, 320
    rng = np.random.RandomState(7)

    normal = np.full((h, w, 3), 245, np.uint8)
    normal_centers = [(60, 70), (60, 170), (140, 110), (140, 230), (190, 60)]
    _draw_discs(normal, normal_centers, 18, (55, 45, 35))

    low = np.full((h, w, 3), 220, np.uint8)
    low_centers = [(70, 80), (70, 190), (150, 120), (150, 240), (190, 70)]
    _draw_discs(low, low_centers, 18, (190, 185, 180))

    dense = np.full((h, w, 3), 245, np.uint8)
    dense_centers = []
    for row in range(7):
        for col in range(10):
            cx = 20 + col * 30 + (rng.randint(-3, 4) if row % 2 else 0)
            cy = 20 + row * 31 + (rng.randint(-3, 4) if row % 2 else 0)
            dense_centers.append((cy, cx))
    _draw_discs(dense, dense_centers, 10, (60, 50, 40))

    return {"normal": normal, "low_contrast": low, "dense": dense}

def _load_rgb(path):
    try:
        from PIL import Image
        with Image.open(path) as source:
            return np.array(source.convert("RGB"))
    except ImportError:
        pass
    if cv2 is None:
        raise RuntimeError("读图需要 Pillow 或 cv2：%s" % path)
    bgr = cv2.imread(path)
    if bgr is None:
        raise RuntimeError("读图失败：%s" % path)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

def selftest():
    expected = {"normal": "normal", "low_contrast": "low_contrast",
                "dense": "dense"}
    print("=== scene_policy 自检（合成图）===")
    print("scene        contrast  fg%%    comps  largest  -> classify  config")
    ok = True
    for name, image in _demo_images().items():
        stats = compute_frame_stats(image)
        scene, reason = explain_scene(stats)
        cfg = auto_config(stats)
        regs = apply_config(cfg, width=image.shape[1], height=image.shape[0])
        passed = (scene == expected[name]
                  and cfg["config_id"] == CONFIG_TABLE[scene]["config_id"]
                  and len(regs) == 8)
        ok = ok and passed
        print("%-12s %7.1f  %5.1f  %5d  %6.2f  -> %-12s id=%d %s"
              % (name, stats["contrast"], stats["foreground_ratio"] * 100,
                 stats["component_count"], stats["largest_component_ratio"],
                 scene, cfg["config_id"], "PASS" if passed else "FAIL"))
        print("             reason: %s" % reason)
        print("             regs: %s"
              % ", ".join("%#04x=%d" % item for item in regs))

    manual = manual_config()
    manual_regs = apply_config(manual, width=320, height=240)
    if manual["config_id"] != 0 or len(manual_regs) != 8:
        ok = False
    print("manual       id=%d threshold=%d morph=%d"
          % (manual["config_id"], manual["threshold"], manual["morph_mode"]))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1

def print_images(paths):
    print("image                              contrast  fg%%    comps  largest  scene         config")
    results = []
    for path in paths:
        rgb = _load_rgb(path)
        stats = compute_frame_stats(rgb)
        scene, reason = explain_scene(stats)
        cfg = auto_config(stats)
        results.append({"path": path, "stats": stats, "scene": scene,
                        "config_id": cfg["config_id"], "reason": reason})
        print("%-34s %8.1f  %5.1f  %5d  %6.2f  %-12s id=%d"
              % (os.path.basename(path), stats["contrast"],
                 stats["foreground_ratio"] * 100, stats["component_count"],
                 stats["largest_component_ratio"], scene, cfg["config_id"]))
    return results

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="scene A 场景判断与 V4 动态配置策略")
    parser.add_argument("--selftest", action="store_true",
                        help="用合成图验证三类场景与寄存器映射")
    parser.add_argument("--images", nargs="+", metavar="PATH",
                        help="对真实图片计算特征并分类")
    parser.add_argument("--json", metavar="PATH",
                        help="把逐图特征/分类写到 JSON")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.images:
        results = print_images(args.images)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump(results, handle, ensure_ascii=False, indent=2)
            print("已写入 %s" % args.json)
        return 0
    parser.print_help()
    return 2

if __name__ == "__main__":
    sys.exit(main())
