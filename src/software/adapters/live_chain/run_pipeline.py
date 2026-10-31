# -*- coding: utf-8 -*-
"""pipeline_validation 统一调用接口：RGB2Gray -> Gaussian -> Edge -> Threshold -> Morphology

本文件是 B 组交付的**唯一入口**（任务书 src/software/app/run_pipeline.py）。
Qt / 回归脚本 / Mock 全部通过 run_pipeline() 调用，不各自实现算法。

设计要点
    1. 逐阶段 Golden 全部用 NumPy 显式实现，不依赖 OpenCV 的默认行为；
       凡是与 PL 约定有关的地方（取整、边界、饱和）都写成可切换参数并注明待确认项。
    2. MockBackend 用同一套 Golden 出结果，接口与真板完全一致，
       让 Qt 在 A 交付 bit/hwh 之前就能联调。
    3. 返回值固定为任务书规定的字段：
       output_image / annotated_image / overlay_version / config /
       golden_pass / pl_e2e_ms / fps / error

⚠️ 待与 A 确认（写死前必须先签字，否则 Golden 一定对不上）
    - Gray  ：Y = (77R + 150G + 29B + 128) >> 8      ← 任务书已定，本文件已按此实现
              注意一个反直觉点：按这个式子，纯绿 (0,255,0) 得到 **149 而不是 150**
              （150×255+128 = 38378，38378>>8 = 149）；纯红/纯蓝则是 77 / 29 原样通过。
              如果 A 为了让纯色"原样通过"而改了权重或舍入，两边必然对不上，必须问清。
    - Gauss ：3x3 核 [1 2 1; 2 4 2; 1 2 1]/16
              取整：GAUSS_ROUND  ('half_up' 加 8 再右移 / 'truncate' 直接右移)
              边界：BORDER_MODE  ('zero' 最外圈输出 0 / 'keep' 最外圈保留输入)
    - Edge  ：沿用 V3 口径 |gx|+|gy|，饱和到 0~255，最外圈 0
    - Morph ：3x3 min/max，最外圈 0（edge_validation 已在板上验证与 A 一致）
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import time

import numpy as np

# ---------------------------------------------------------------- 全局约定
GAUSS_ROUND = "half_up"     # 'half_up' | 'truncate'   —— 待 A 确认
BORDER_MODE = "zero"        # 'zero'    | 'keep'       —— 待 A 确认

EDGE_SOBEL, EDGE_PREWITT = "Sobel", "Prewitt"
MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE = 0, 1, 2
MORPH_NAMES = {MORPH_BYPASS: "bypass", MORPH_ERODE: "erode", MORPH_DILATE: "dilate"}

_KERNELS = {
    EDGE_SOBEL: (np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], np.int32),
                 np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], np.int32)),
    EDGE_PREWITT: (np.array([[-1, 0, 1], [-1, 0, 1], [-1, 0, 1]], np.int32),
                   np.array([[-1, -1, -1], [0, 0, 0], [1, 1, 1]], np.int32)),
}

DEFAULT_CONFIG = {
    "enable_gray": True,
    "enable_gauss": False,
    "edge_mode": EDGE_SOBEL,     # None 表示不跑边缘
    "threshold": 40,             # 0 = 饱和输出；>0 = 二值化
    "morph_mode": MORPH_BYPASS,
}

# 任务书点名的 Seed Preset：Gaussian 开、Sobel、固定阈值与形态学模式
SEED_PRESET = {
    "enable_gray": True,
    "enable_gauss": True,
    "edge_mode": EDGE_SOBEL,
    "threshold": 40,
    "morph_mode": MORPH_DILATE,
}

# ================================================================ 逐阶段 Golden
def rgb2gray(rgb):
    """Y = (77R + 150G + 29B + 128) >> 8   —— 任务书定稿的定点式。"""
    a = np.asarray(rgb, dtype=np.int32)
    if a.ndim != 3 or a.shape[2] < 3:
        raise ValueError("rgb2gray 需要 HxWx3 输入，收到 %s" % (a.shape,))
    y = (77 * a[:, :, 0] + 150 * a[:, :, 1] + 29 * a[:, :, 2] + 128) >> 8
    return np.clip(y, 0, 255).astype(np.uint8)

def _window_reduce(img, reduce_fn, fill):
    """3x3 窗口归约；内部直算，最外圈按 BORDER_MODE 处理。"""
    h, w = img.shape
    if h < 3 or w < 3:
        raise ValueError("尺寸至少 3x3，当前 %dx%d" % (w, h))
    core = np.full((h - 2, w - 2), fill, dtype=np.int32)
    for dy in range(3):
        for dx in range(3):
            core = reduce_fn(core, img[dy:dy + h - 2, dx:dx + w - 2].astype(np.int32))
    out = np.zeros_like(img) if BORDER_MODE == "zero" else img.copy()
    out[1:-1, 1:-1] = np.clip(core, 0, 255).astype(np.uint8)
    return out

def gaussian3x3(gray):
    """3x3 整数核 [1 2 1; 2 4 2; 1 2 1] / 16。

    取整与边界见文件头 GAUSS_ROUND / BORDER_MODE。**这两项必须与 A 一致。**

    ⚠️ 已实测的副作用（必须让 A 知道）：
        在 BORDER_MODE='zero' 下，高斯输出第 0 圈是 0、第 1 圈是真实值，
        这一圈落差会被后面的边缘检测算成一条**假边缘框**。
        实测纯白 640x480：关高斯时边缘像素 = 0；开高斯后 = 2228，
        恰好等于画面周长 2*((w-2)+(h-2)) = 2232（差 4 个角点）。
        对表型计数无影响（贴边轮廓本来就会被边界规则剔除），
        但演示画面上能看见这一圈框。A 若改用"复制边缘"填充则不会有这个问题。
    """
    g = np.asarray(gray, dtype=np.int32)
    h, w = g.shape
    if h < 3 or w < 3:
        raise ValueError("尺寸至少 3x3，当前 %dx%d" % (w, h))
    k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.int32)
    acc = np.zeros((h - 2, w - 2), np.int32)
    for dy in range(3):
        for dx in range(3):
            acc += k[dy, dx] * g[dy:dy + h - 2, dx:dx + w - 2]
    if GAUSS_ROUND == "half_up":
        core = (acc + 8) >> 4
    elif GAUSS_ROUND == "truncate":
        core = acc >> 4
    else:
        raise ValueError("未知 GAUSS_ROUND: %r" % (GAUSS_ROUND,))
    out = np.zeros_like(g) if BORDER_MODE == "zero" else g.copy()
    out[1:-1, 1:-1] = np.clip(core, 0, 255).astype(np.uint8)
    return out.astype(np.uint8)

def edge_filter(gray, mode=EDGE_SOBEL, threshold=0):
    """|gx| + |gy|，饱和到 0~255，最外圈 0；threshold>0 时二值化。与 V3 同口径。"""
    g = np.asarray(gray, dtype=np.int32)
    if mode not in _KERNELS:
        raise ValueError("edge_mode 只能是 %s，收到 %r" % (list(_KERNELS), mode))
    kx, ky = _KERNELS[mode]

    def conv(k):
        return (k[0, 0] * g[:-2, :-2] + k[0, 1] * g[:-2, 1:-1] + k[0, 2] * g[:-2, 2:]
                + k[1, 0] * g[1:-1, :-2] + k[1, 1] * g[1:-1, 1:-1] + k[1, 2] * g[1:-1, 2:]
                + k[2, 0] * g[2:, :-2] + k[2, 1] * g[2:, 1:-1] + k[2, 2] * g[2:, 2:])

    mag = np.abs(conv(kx)) + np.abs(conv(ky))
    if threshold > 0:
        mag = np.where(mag >= threshold, 255, 0)
    out = np.zeros(g.shape, np.uint8)
    out[1:-1, 1:-1] = np.clip(mag, 0, 255).astype(np.uint8)
    return out

def morph(binary, morph_mode):
    """3x3 Erode（min）/ Dilate（max），最外圈 0。与 edge_validation 板测口径一致。"""
    if morph_mode == MORPH_BYPASS:
        return np.asarray(binary, np.uint8).copy()
    if morph_mode == MORPH_ERODE:
        return _window_reduce(np.asarray(binary, np.uint8), np.minimum, 255)
    if morph_mode == MORPH_DILATE:
        return _window_reduce(np.asarray(binary, np.uint8), np.maximum, 0)
    raise ValueError("morph_mode 只能是 0/1/2，收到 %r" % (morph_mode,))

def pipeline_stages(rgb, config=None):
    """跑完整链，并把每一级的中间结果都返回（供逐阶段对拍用）。"""
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    stages = {"input_rgb": np.asarray(rgb, np.uint8)}

    gray = rgb2gray(stages["input_rgb"]) if cfg["enable_gray"] else None
    stages["gray"] = gray
    if gray is None:
        raise ValueError("当前实现要求 enable_gray=True（PL 必须真实做 RGB2Gray）")

    if cfg["enable_gauss"]:
        gray = gaussian3x3(gray)
    stages["gaussian"] = gray

    if cfg["edge_mode"] is not None:
        stages["edge"] = edge_filter(gray, cfg["edge_mode"], 0)
        stages["threshold"] = edge_filter(gray, cfg["edge_mode"], cfg["threshold"])
        stages["morphology"] = morph(stages["threshold"], cfg["morph_mode"])
        stages["output"] = stages["morphology"]
    else:
        stages["edge"] = None
        stages["threshold"] = None
        stages["morphology"] = None
        stages["output"] = gray
    return stages, cfg

# ================================================================ 后端
class MockBackend:
    """与真板同接口的软件后端，给 Qt 在 A 交付 bit/hwh 之前联调用。

    返回字典里 simulated=True，Qt 必须据此标注 SIMULATED，不能当实测数字。
    """

    version = "mock-pipeline_validation"

    def run(self, rgb, width, height, config):
        t0 = time.perf_counter()
        stages, cfg = pipeline_stages(rgb, config)
        ms = (time.perf_counter() - t0) * 1000.0
        return {
            "output_image": stages["output"],
            "stages": stages,
            "overlay_version": self.version,
            "config": cfg,
            "golden_pass": True,
            "pl_e2e_ms": round(ms, 4),
            "fps": round(1000.0 / ms, 1) if ms > 0 else None,
            "simulated": True,
            "error": None,
        }

class PLBackend:
    """真板后端。只有在这个 Python 环境能 import pynq 时才能用。"""

    def __init__(self, bitfile=None):
        from edge_morph_driver import SobelParamDriver        # 板端脚本目录里
        self.driver = SobelParamDriver(bitfile=bitfile)
        self.version = getattr(self.driver, "bitfile", "unknown")

    def run(self, rgb, width, height, config):
        stages, cfg = pipeline_stages(rgb, config)
        raise NotImplementedError(
            "等 A 交付 pipeline_validation_v4.bit/hwh 并确认寄存器表后接这里：\n"
            "  1) 把 gray/Gaussian 的使能位写进寄存器（enable_gray/enable_gauss）\n"
            "  2) 用 driver.run_frame(..., morph_mode=cfg['morph_mode']) 跑一帧\n"
            "  3) 与 stages['output'] 逐像素比，填 golden_pass"
        )

def run_pipeline(image, width, height, config=None, backend="mock", pl_backend=None):
    """统一入口。image 为 HxWx3 uint8（RGB）。返回任务书规定的字段。"""
    rgb = np.asarray(image)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return {"output_image": None, "annotated_image": None,
                "overlay_version": None, "config": config or {},
                "golden_pass": False, "pl_e2e_ms": None, "fps": None,
                "simulated": True, "error": "输入必须是 HxWx3 RGB 图像"}
    if rgb.shape[0] != height or rgb.shape[1] != width:
        return {"output_image": None, "annotated_image": None,
                "overlay_version": None, "config": config or {},
                "golden_pass": False, "pl_e2e_ms": None, "fps": None,
                "simulated": True,
                "error": "尺寸不符：声明 %dx%d，实际 %dx%d"
                         % (width, height, rgb.shape[1], rgb.shape[0])}
    be = pl_backend if (backend == "pl" and pl_backend) else MockBackend()
    try:
        result = be.run(rgb, width, height, config)
    except Exception as exc:                     # 任何后端异常都收敛成 error 字段
        return {"output_image": None, "annotated_image": None,
                "overlay_version": getattr(be, "version", None), "config": config or {},
                "golden_pass": False, "pl_e2e_ms": None, "fps": None,
                "simulated": (backend == "mock"), "error": "%s: %s" % (type(exc).__name__, exc)}
    result.setdefault("annotated_image", None)
    return result
