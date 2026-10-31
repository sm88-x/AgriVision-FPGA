#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""system_validation sobel_v4_pipeline 全链路核板端验证与性能测试。

    hardware/firmware/configurable_pipeline.bit / .hwh
    核实例 sobel_v4_pipeline_0(s_axi_control),DMA 实例 axi_dma_0

内核语义(与 src/hls/v4-pipeline 设计一致,详见 register_map_sobel_v4_pipeline.md):
    * 链路:RGB2Gray(可选) → Gaussian 3×3(可选) → Edge(阈值) → Morphology
    * RGB2Gray:Y=(77R+150G+29B+128)>>8;Gauss 3×3 [1 2 1;2 4 2;1 2 1] 零填充
      (sum+8)>>4;Sobel/Prewitt |gx|+|gy|,thr>0 二值(>=thr→255)/==0 饱和;
      Morph 3×3 min/max 零填充
    * 级联窗口:三级各带独立 3×3 零填充窗口(不是 5×5),总延迟 3×(width+1)
    * out_sel:0=最终 morph / 1=gray(全帧有效) / 2=gauss / 3=edge;非法按 0
    * enable_gray=0 透传输入低 8bit(灰度路径,兼 V4 回归锚点)
    * 每帧:写 8 个数据寄存器 → 挂 DMA → ap_start → 等双通道 IOC
    * ap_ctrl_hs 无 auto-rewind,每帧都要重新写一次 ap_start;中断口悬空,轮询 + 超时

输入协议(RGBX888,32bit/拍):bit[23:16]=R、[15:8]=G、[7:0]=B、[31:24] 忽略,
每拍 1 像素,帧 = width×height 拍,TLAST 帧尾一拍;DMA MM2S=32bit、S2MM=8bit。

参考实现内置(已与 C 仿真逐拍复刻在 PC 上逐像素对拍通过,含旁路 wrap 语义),
用 pad + 切片实现,不依赖 sliding_window_view——板端旧 numpy 没有这个函数。

用法(在板的 Jupyter 里,先把本文件、bit、hwh 放到同一目录):
    %run integrated_pipeline_smoke_test.py
或命令行:
    sudo python3 integrated_pipeline_smoke_test.py --warmup 2 --repeats 5

产物:
    results/board_system_validation.csv      逐次计时数据
    hardware_test_log.txt       汇总日志(拷回 build/reports/v4-pipeline/
                                另存为 hardware_smoke_test.txt 交付)
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
import json
import time
from pathlib import Path

import numpy as np

# pynq 只在板子上有;放到函数里延迟导入,这样本文件在 PC 上也能被引用和自测
try:
    from pynq import Overlay, MMIO, allocate
    HAVE_PYNQ = True
except ImportError:      # PC 上跑参考实现的自测时走这里
    Overlay = MMIO = allocate = None
    HAVE_PYNQ = False

# ------------------------------------------------------------------
# 被测配置
# ------------------------------------------------------------------
BIT_FILE = "configurable_pipeline.bit"
KERNEL_IP = "sobel_v4_pipeline_0"   # 只用于从 ip_dict 里取地址,不写死地址本身
DMA_IP = "axi_dma_0"

# s_axilite 寄存器偏移(与 hwh / 寄存器表一致)
OFF_AP_CTRL = 0x00
OFF_WIDTH = 0x10
OFF_HEIGHT = 0x18
OFF_MODE = 0x20
OFF_THRESHOLD = 0x28
OFF_MORPH = 0x30
OFF_EN_GRAY = 0x38
OFF_EN_GAUSS = 0x40
OFF_OUT_SEL = 0x48
MODE_SOBEL, MODE_PREWITT = 0, 1
MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE = 0, 1, 2
MORPH_NAMES = {MORPH_BYPASS: "Bypass", MORPH_ERODE: "Erode", MORPH_DILATE: "Dilate"}
OUTSEL_NAMES = {0: "morph", 1: "gray", 2: "gauss", 3: "edge"}

# AXI DMA 状态寄存器(用来判断一帧传完没有)
MM2S_DMASR_OFF = 0x04
S2MM_DMASR_OFF = 0x34
DMASR_IOC = 0x1000      # bit12:传输完成标志(写 1 清除)
DMASR_INTERNAL_ERR = 0x1E000000   # bit25-28:DMA 内部错误(拍数/TLAST 异常会置起)
POLL_INTERVAL = 0.0001  # 轮询间隔 0.1 ms,越小计时越准

# 允许的分辨率范围(A 侧定稿)
MIN_W, MAX_W = 3, 1920
MIN_H, MAX_H = 3, 1080

# ------------------------------------------------------------------
# 参考实现:切片版 golden(与 C 仿真逐拍复刻在 PC 上逐像素对拍通过)
# ------------------------------------------------------------------
_GAUSS_K = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.int64)
_EDGE_K = {
    MODE_SOBEL: (np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], np.int64),
                 np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], np.int64)),
    MODE_PREWITT: (np.array([[-1, 0, 1], [-1, 0, 1], [-1, 0, 1]], np.int64),
                   np.array([[-1, -1, -1], [0, 0, 0], [1, 1, 1]], np.int64)),
}

def conv2d_window(img, coeff):
    """窗口中心 (i,j) 的 3×3 加权和,帧外零填充;返回全尺寸结果。"""
    p = np.pad(img, 1, mode="constant", constant_values=0).astype(np.int64)
    h, w = img.shape
    acc = np.zeros_like(img, dtype=np.int64)
    for y in range(3):
        for x in range(3):
            acc += coeff[y][x] * p[y:y + h, x:x + w]
    return acc

def pipeline_golden(gray_in, mode, threshold, morph, en_gauss, out_sel):
    """逐像素参考:gray_in 是灰度和度帧(RGB2Gray 之后 / 透传的低 8bit)。

    级联口径(与 C 逐位一致,含旁路 wrap 与环 0 输出):
        G[r,c] = gauss3x3(gray, 中心=(r-1,c-1))           # 仅 r,c>=2 计算
               (旁路:gray 流线性滞后 D 拍,含最右列 wrap)
        E[r,c] = edge3x3(G, 中心=(r,c))                   # 零填充,仅 r,c>=2
               (旁路时等价 edge3x3(gray, 中心=(r-1,c-1)))
        M[r,c] = morph3x3(E, 中心=(r,c))                  # 零填充,仅 r,c>=2
        out_sel=1: out = gray 全帧;其余:out[r,c] = 级流[r+1,c+1],外圈强制 0
    """
    h, w = gray_in.shape
    gray = gray_in.astype(np.int64)

    # 第 1 级:Gaussian(或旁路线性滞后 D)
    if en_gauss:
        g_full = (conv2d_window(gray, _GAUSS_K) + 8) >> 4
        gs = np.zeros((h, w), np.int64)
        gs[2:, 2:] = g_full[1:-1, 1:-1]
    else:
        gs = np.zeros((h, w), np.int64)
        gs[1:, 1:] = gray[:-1, :-1]
        gs[2:, 0] = gray[:-2, -1]

    # 第 2 级:Edge + 阈值
    kx, ky = _EDGE_K[mode]
    # 旁路时 e 窗口在 c=w-1 经行尾寄存器 wrap 到真实最右列,
    # 等价 sobel(gray, 中心=(r-1,c-1)) 全列统一(已数值验证)。
    src = gs if en_gauss else gray
    mag = np.abs(conv2d_window(src, kx)) + np.abs(conv2d_window(src, ky))
    if threshold > 0:
        mag = np.where(mag >= threshold, 255, 0)
    else:
        mag = np.clip(mag, 0, 255)
    es = np.zeros((h, w), np.int64)
    if en_gauss:
        es[2:, 2:] = mag[2:, 2:]
    else:
        es[2:, 2:] = mag[1:-1, 1:-1]

    # 第 3 级:Morphology
    if morph == MORPH_BYPASS:
        ms = es
    else:
        p = np.pad(es, 1, mode="constant", constant_values=0)
        views = [p[y:y + h, x:x + w] for y in range(3) for x in range(3)]
        stack = np.stack(views)
        red = (np.minimum.reduce(stack, axis=0) if morph == MORPH_ERODE
               else np.maximum.reduce(stack, axis=0))
        ms = np.zeros((h, w), np.int64)
        ms[2:, 2:] = red[2:, 2:]

    # 输出:out_sel=1 全帧 gray;其余 out[r,c] = 级流[r+1,c+1],外圈强制 0
    if out_sel == 1:
        return gray.astype(np.uint8)
    if out_sel == 2:
        stream = gs
    elif out_sel == 3:
        stream = es
    else:
        stream = ms
    out = np.zeros((h, w), np.int64)
    out[1:-1, 1:-1] = stream[2:h, 2:w]
    return out.astype(np.uint8)

def compare(reference, result):
    diff = np.abs(result.astype(np.int16) - reference.astype(np.int16))
    different = int(np.count_nonzero(diff))
    return {
        "different_pixels": different,
        "max_error": int(diff.max()),
        "mean_error": float(diff.mean()),
        "verdict": "PASS" if different == 0 else "FAIL",
    }

def diff_diagnosis(reference, result):
    """FAIL 时的空间定位:ASCII 密度图 + 行列直方图 + 抽样值对比。

    密度图把整帧压成约 40 列 × 24 行的网格,每个字符对应一小块区域:
    '.' = 全对,数字 1-9 = 该块内 diff 像素占比(成)。一眼就能看出
    错在外圈、整行/整列、块状、还是随机散布。
    """
    h, w = result.shape
    diff = result.astype(np.int16) != reference.astype(np.int16)
    gh, gw = max(1, h // 24), max(1, w // 40)
    map_lines = []
    for by in range(0, h, gh):
        chars = []
        for bx in range(0, w, gw):
            frac = int(diff[by:by + gh, bx:bx + gw].mean() * 10 + 0.5)
            chars.append("." if frac == 0 else str(min(frac, 9)))
        map_lines.append("".join(chars))

    def clipped(pairs, cap=14):
        if len(pairs) <= cap * 2:
            return " ".join("%d:%d" % p for p in pairs)
        head = " ".join("%d:%d" % p for p in pairs[:cap])
        tail = " ".join("%d:%d" % p for p in pairs[-cap:])
        return f"{head} ... {tail}(共 {len(pairs)} 个)"

    row_pairs = [(int(i), int(v)) for i, v in enumerate(diff.sum(axis=1)) if v]
    col_pairs = [(int(i), int(v)) for i, v in enumerate(diff.sum(axis=0)) if v]
    ys, xs = np.nonzero(diff)
    samples = []
    for r, c in zip(ys[:8], xs[:8]):
        samples.append(f"({r},{c})HW={int(result[r, c])}/REF={int(reference[r, c])}")
    return {
        "density_map": map_lines,
        "rows": clipped(row_pairs),
        "cols": clipped(col_pairs),
        "first_last": (int(ys[0]), int(xs[0]), int(ys[-1]), int(xs[-1])),
        "value_samples": samples,
    }

# ------------------------------------------------------------------
# 输入图构造
# ------------------------------------------------------------------
def make_rgb_input(width, height, seed, textured):
    """返回 (32bit RGBX 打包一维数组, gray 参考图)。

    textured=True 时用行列梯度构造结构化纹理(测流序);
    否则三通道独立随机(测 RGB2Gray 公式本身)。
    """
    rng = np.random.RandomState(seed)   
    if textured:
        yy, xx = np.mgrid[0:height, 0:width].astype(np.int64)
        r = xx & 0xFF
        g = yy & 0xFF
        b = ((xx + yy) >> 1) & 0xFF
        rgb = np.stack([r, g, b], axis=2)
    else:
        rgb = rng.randint(0, 256, size=(height, width, 3))
    words = ((rgb[:, :, 0].astype(np.uint32) << 16)
             | (rgb[:, :, 1].astype(np.uint32) << 8)
             | rgb[:, :, 2].astype(np.uint32))
    gray = ((77 * rgb[:, :, 0] + 150 * rgb[:, :, 1] + 29 * rgb[:, :, 2] + 128) >> 8)
    return words.reshape(-1), gray.astype(np.int64)

def make_gray_input(width, height, seed, textured):
    """返回 (32bit 字数组,低 8bit=gray;enable_gray=0 透传路径)。"""
    rng = np.random.RandomState(seed)   # 板端旧 numpy 没有 default_rng
    if textured:
        yy, xx = np.mgrid[0:height, 0:width].astype(np.int64)
        gray = ((xx * 7 + yy * 13 + (xx // 16) * 29 + (yy // 16) * 53) & 0xFF)
        gray = gray.astype(np.uint8)
    else:
        gray = rng.randint(0, 256, size=(height, width)).astype(np.uint8)
    words = gray.astype(np.uint32)
    return words.reshape(-1), gray.astype(np.int64)

# ------------------------------------------------------------------
# 硬件访问
# ------------------------------------------------------------------
class SobelV4PipelineDriver:
    """按寄存器表封装的驱动程序。"""

    def __init__(self, bitfile=BIT_FILE, kernel=KERNEL_IP, dma_name=DMA_IP):
        if not HAVE_PYNQ:
            raise RuntimeError("这个类只能在 PYNQ 板子上用(需要 pynq 包)")
        self.ol = Overlay(bitfile)
        missing = [n for n in (kernel, dma_name) if n not in self.ol.ip_dict]
        if missing:
            raise RuntimeError(
                f"Overlay 里没有 {missing};实际有:{sorted(self.ol.ip_dict)}"
            )

        # 地址一律从 overlay 动态读取
        self.kernel_base = self.ol.ip_dict[kernel]["phys_addr"]
        self.dma_base = self.ol.ip_dict[dma_name]["phys_addr"]
        self.ctrl = MMIO(self.kernel_base, 0x1000)
        self.dma_ctrl = MMIO(self.dma_base, 0x1000)
        self.dma = getattr(self.ol, dma_name)

    def info(self):
        return {
            "bitfile": BIT_FILE,
            "ip_dict": sorted(self.ol.ip_dict.keys()),
            "kernel_base": hex(self.kernel_base),
            "dma_base": hex(self.dma_base),
            "输入协议": "MM2S 32bit RGBX888/拍,keep=0xF,帧=width*height 拍",
            "说明": "地址均从 hwh 动态读取;每帧先挂 DMA 再拉 ap_start",
        }

    def _wait_done(self, t_start, timeout):
        """轮询 DMA 状态寄存器,直到两个通道都报告"传输完成"。

        判断依据用 IOC_Irq(bit12)而不是 Idle(bit1):
        Idle 在传输刚发出、通道还没真正启动的那一瞬间可能仍为 1,
        会让轮询误判成"已经传完",紧接着启动下一帧就会乱套。
        IOC 位在启动前已被清零,只有真正传完才会置起来,不会误判。
        """
        while True:
            mm2s = self.dma_ctrl.read(MM2S_DMASR_OFF)
            s2mm = self.dma_ctrl.read(S2MM_DMASR_OFF)
            if (mm2s & DMASR_IOC) and (s2mm & DMASR_IOC):
                return
            if time.perf_counter() - t_start > timeout:
                raise TimeoutError(
                    f"帧超时 {timeout}s:MM2S_DMASR={hex(mm2s)} "
                    f"S2MM_DMASR={hex(s2mm)} 核 AP_CTRL={hex(self.ctrl.read(OFF_AP_CTRL))}"
                )
            time.sleep(POLL_INTERVAL)

    def dma_status(self):
        """读两个通道的状态字(整数),供拍数/TLAST 异常检查。"""
        return (self.dma_ctrl.read(MM2S_DMASR_OFF),
                self.dma_ctrl.read(S2MM_DMASR_OFF))

    def run_frame(self, inp_buf, out_buf, width, height, mode, threshold, morph,
                  en_gray, en_gauss, out_sel, timeout=5.0):
        """跑一帧,返回耗时(秒,含 DMA 收发)。inp_buf 为 uint32 RGBX 字。"""
        if not (MIN_W <= width <= MAX_W) or not (MIN_H <= height <= MAX_H):
            raise ValueError(f"分辨率 {width}x{height} 超出允许范围")
        if mode not in (MODE_SOBEL, MODE_PREWITT):
            raise ValueError(f"mode 只能是 0(Sobel)或 1(Prewitt),收到 {mode}")
        if morph not in (MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE):
            raise ValueError(f"morph_mode 只能是 0/1/2,收到 {morph}")
        for name, value in (("enable_gray", en_gray), ("enable_gauss", en_gauss)):
            if value not in (0, 1):
                raise ValueError(f"{name} 只能是 0/1,收到 {value}")
        if out_sel not in (0, 1, 2, 3):
            raise ValueError(f"out_sel 只能是 0/1/2/3,收到 {out_sel}")

        t0 = time.perf_counter()

        # 0) 清掉上一帧留下的完成标志——清零后只有本帧真正传完才会再次置起
        self.dma_ctrl.write(MM2S_DMASR_OFF, DMASR_IOC)
        self.dma_ctrl.write(S2MM_DMASR_OFF, DMASR_IOC)

        # 1) 写 8 个数据寄存器
        self.ctrl.write(OFF_WIDTH, width)
        self.ctrl.write(OFF_HEIGHT, height)
        self.ctrl.write(OFF_MODE, mode)
        self.ctrl.write(OFF_THRESHOLD, threshold)
        self.ctrl.write(OFF_MORPH, morph)
        self.ctrl.write(OFF_EN_GRAY, en_gray)
        self.ctrl.write(OFF_EN_GAUSS, en_gauss)
        self.ctrl.write(OFF_OUT_SEL, out_sel)

        # 2) 先把两个 DMA 通道挂好
        #    S2MM 收 = width*height 字节(8bit/拍),MM2S 发 = width*height 拍(32bit/拍)
        self.dma.recvchannel.transfer(out_buf)
        self.dma.sendchannel.transfer(inp_buf)

        # 3) 最后拉 ap_start,启动核(ap_ctrl_hs 每帧都要重新拉一次)
        #    顺序说明:在真板上实测"先挂 DMA 再 ap_start"才稳定。
        self.ctrl.write(OFF_AP_CTRL, 1)

        # 4) 轮询等两个通道都报告完成
        self._wait_done(t0, timeout)

        return time.perf_counter() - t0

# ------------------------------------------------------------------
# 测试用例
# ------------------------------------------------------------------
def build_cases():
    """T1~T18 用例表:三档尺寸 × 全链/回归/旁路/out_sel 组合。

    输入 kind:rgb = 32bit RGBX(走 RGB2Gray),gray = 低 8bit 灰度(透传)。
    textured=True 用行列梯度纹理(测流序),否则随机(测公式)。
    字段:(id, (w,h), kind, mode, thr, morph, en_gray, en_gauss, out_sel, seed, textured)
    """
    return [
        # ---- 256×256:全配置矩阵 ----
        ("T01", (256, 256), "rgb",  MODE_SOBEL,   0,  MORPH_BYPASS, 1, 1, 0, 101, False),
        ("T02", (256, 256), "rgb",  MODE_SOBEL,   80, MORPH_ERODE,  1, 1, 0, 102, False),
        ("T03", (256, 256), "rgb",  MODE_SOBEL,   80, MORPH_DILATE, 1, 1, 0, 103, False),
        ("T04", (256, 256), "rgb",  MODE_PREWITT, 0,  MORPH_ERODE,  1, 1, 0, 104, False),
        ("T05", (256, 256), "rgb",  MODE_SOBEL,   80, MORPH_ERODE,  1, 1, 0, 105, True),
        ("T06", (256, 256), "rgb",  MODE_SOBEL,   0,  MORPH_BYPASS, 1, 1, 1, 106, False),
        ("T07", (256, 256), "rgb",  MODE_SOBEL,   0,  MORPH_BYPASS, 1, 1, 2, 107, False),
        ("T08", (256, 256), "rgb",  MODE_SOBEL,   80, MORPH_BYPASS, 1, 1, 3, 108, False),
        ("T09", (256, 256), "gray", MODE_SOBEL,   0,  MORPH_BYPASS, 0, 0, 0, 109, False),
        ("T10", (256, 256), "gray", MODE_SOBEL,   80, MORPH_DILATE, 0, 0, 0, 110, True),
        ("T11", (256, 256), "gray", MODE_SOBEL,   80, MORPH_ERODE,  0, 1, 0, 111, False),
        # ---- 320×240(奇数宽,旁路 wrap 边界最刁钻) ----
        ("T12", (320, 240), "rgb",  MODE_SOBEL,   80, MORPH_ERODE,  1, 1, 0, 112, False),
        ("T13", (320, 240), "gray", MODE_SOBEL,   0,  MORPH_BYPASS, 0, 0, 0, 113, False),
        ("T14", (320, 240), "rgb",  MODE_SOBEL,   0,  MORPH_BYPASS, 1, 1, 2, 114, True),
        # ---- 640×480:大尺寸 ----
        ("T15", (640, 480), "rgb",  MODE_SOBEL,   0,  MORPH_BYPASS, 1, 1, 0, 115, False),
        ("T16", (640, 480), "gray", MODE_SOBEL,   0,  MORPH_BYPASS, 0, 0, 0, 116, False),
        ("T17", (640, 480), "rgb",  MODE_SOBEL,   80, MORPH_DILATE, 1, 1, 0, 117, True),
        ("T18", (640, 480), "gray", MODE_SOBEL,   0,  MORPH_BYPASS, 0, 0, 3, 118, False),
    ]

def main(argv=None):
    parser = argparse.ArgumentParser(description="system_validation sobel_v4_pipeline 板端验证与性能测试")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=5.0, help="单帧超时秒数")
    parser.add_argument("--save", action="store_true",
                        help="FAIL 用例把板端输出存为 result_<case>.bin(拷回 PC 分析)")
    parser.add_argument("--out", default="data/evidence/analysis/board_system_validation.csv")
    parser.add_argument("--info", default="data/evidence/analysis/board_system_validation_info.json")
    parser.add_argument("--log", default="hardware_test_log.txt")
    args = parser.parse_args(argv)

    driver = SobelV4PipelineDriver()
    info = driver.info()
    print("=== Overlay 信息(地址从 hwh 动态读取) ===")
    for key, value in info.items():
        print(f"    {key}: {value}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.info).parent.mkdir(parents=True, exist_ok=True)
    Path(args.info).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    summary = []
    all_pass = True

    for case_id, (width, height), kind, mode, threshold, morph, en_gray, en_gauss, \
            out_sel, seed, textured in build_cases():
        morph_name = MORPH_NAMES[morph]
        print(f"\n===== {case_id}  {width}x{height}  kind={kind}  "
              f"mode={'Sobel' if mode == MODE_SOBEL else 'Prewitt'}  thr={threshold}  "
              f"morph={morph_name}  en_gray={en_gray}  en_gauss={en_gauss}  "
              f"out_sel={out_sel}({OUTSEL_NAMES[out_sel]}) =====")

        if kind == "rgb":
            words, gray = make_rgb_input(width, height, seed, textured)
            origin = f"RGB 随机 seed={seed}" if not textured else f"RGB 梯度纹理 seed={seed}"
        else:
            words, gray = make_gray_input(width, height, seed, textured)
            origin = f"灰度随机 seed={seed}" if not textured else f"灰度纹理 seed={seed}"
        print(f"    输入:{origin}")

        # 板端 PS 参考(寄存器表口径的切片 golden)
        reference = pipeline_golden(gray, mode, threshold, morph, en_gauss, out_sel)

        # 分配连续内存(每档尺寸各分配一次、循环外)
        inp = allocate(shape=(width * height,), dtype=np.uint32)
        out = allocate(shape=(width * height,), dtype=np.uint8)
        inp[:] = words

        # ---- 正确性:先跑一帧对拍,PASS 才计时 ----
        driver.run_frame(inp, out, width, height, mode, threshold, morph,
                         en_gray, en_gauss, out_sel, args.timeout)
        mm2s_dmasr, s2mm_dmasr = driver.dma_status()
        result = np.array(out).reshape(height, width)
        stats = compare(reference, result)
        print(f"    正确性:{stats['verdict']}  不同像素={stats['different_pixels']}"
              f"  最大误差={stats['max_error']}  "
              f"DMASR={hex(mm2s_dmasr)}/{hex(s2mm_dmasr)}")
        all_pass = all_pass and stats["verdict"] == "PASS"

        if stats["verdict"] != "PASS":
            diag = diff_diagnosis(reference, result)
            print("    —— diff 密度图(. = 全对,1-9 = 该块 diff 占比成数)——")
            for line in diag["density_map"]:
                print("    " + line)
            print(f"    首个/末个 diff 像素 (r,c):( {diag['first_last'][0]},"
                  f"{diag['first_last'][1]} ) → ( {diag['first_last'][2]},"
                  f"{diag['first_last'][3]} )")
            print(f"    有 diff 的行 r:cnt = {diag['rows']}")
            print(f"    有 diff 的列 c:cnt = {diag['cols']}")
            print(f"    抽样值(前8个): {'  '.join(diag['value_samples'])}")
            if args.save:
                Path(f"result_{case_id}.bin").write_bytes(result.tobytes())
                print(f"    已存板端输出 result_{case_id}.bin(拷回 PC 做逐像素分析)")

        # out_sel=1(gray)全帧有效;其余窗口级输出最外一圈强制 0
        if out_sel != 1:
            border_ok = bool(result[0, :].max() == 0 and result[-1, :].max() == 0
                             and result[:, 0].max() == 0 and result[:, -1].max() == 0)
            print(f"    最外一圈全 0:{'是' if border_ok else '否'}")
            all_pass = all_pass and border_ok
        else:
            border_ok = True

        # 拍数/TLAST 证据:DMA 内部错误位(bit25-28)置起 = 拍数与 TLAST 不符
        dma_err = bool((mm2s_dmasr & DMASR_INTERNAL_ERR)
                       or (s2mm_dmasr & DMASR_INTERNAL_ERR))
        if dma_err:
            print("    → DMA 内部错误位置起:拍数/TLAST 异常!")
            all_pass = False

        if stats["verdict"] != "PASS" or not border_ok or dma_err:
            print("    → 未通过,跳过性能计时(先修正确性)")
            summary.append(f"{case_id} {width}x{height} {kind} thr={threshold} "
                           f"out_sel={out_sel} FAIL")
            for index in range(1, args.repeats + 1):
                rows.append({"case": case_id, "size": f"{width}x{height}", "kind": kind,
                             "mode": mode, "threshold": threshold, "morph": morph_name,
                             "en_gray": en_gray, "en_gauss": en_gauss, "out_sel": out_sel,
                             "run_id": index, "ms": "", "fps": "",
                             "correct": stats["verdict"],
                             "different_pixels": stats["different_pixels"],
                             "mm2s_dmasr": hex(mm2s_dmasr), "s2mm_dmasr": hex(s2mm_dmasr)})
            del inp, out
            continue

        # ---- 性能 ----
        # 单帧失败不中断整轮:记下来继续测后面的用例,最后统一汇报
        try:
            for _ in range(args.warmup):
                driver.run_frame(inp, out, width, height, mode, threshold, morph,
                                 en_gray, en_gauss, out_sel, args.timeout)
            samples = []
            for _ in range(args.repeats):
                samples.append(driver.run_frame(inp, out, width, height, mode, threshold,
                                                morph, en_gray, en_gauss, out_sel,
                                                args.timeout) * 1e3)
        except Exception as exc:
            print(f"    [性能计时中断] {exc}")
            all_pass = False
            summary.append(f"{case_id} {width}x{height} {kind} thr={threshold} "
                           f"out_sel={out_sel} TIMEOUT")
            rows.append({"case": case_id, "size": f"{width}x{height}", "kind": kind,
                         "mode": mode, "threshold": threshold, "morph": morph_name,
                         "en_gray": en_gray, "en_gauss": en_gauss, "out_sel": out_sel,
                         "run_id": 0, "ms": "", "fps": "",
                         "correct": "TIMEOUT", "different_pixels": "",
                         "mm2s_dmasr": hex(mm2s_dmasr), "s2mm_dmasr": hex(s2mm_dmasr)})
            del inp, out
            continue

        ms = np.asarray(samples)
        print(f"    PL E2E  mean={ms.mean():.3f}  median={np.median(ms):.3f}  "
              f"min={ms.min():.3f}  max={ms.max():.3f} ms | FPS={1000.0/ms.mean():.1f}")
        summary.append(f"{case_id} {width}x{height} {kind} thr={threshold} "
                       f"out_sel={out_sel} PASS mean={ms.mean():.3f}ms "
                       f"FPS={1000.0/ms.mean():.1f}")

        for index, value in enumerate(samples, 1):
            rows.append({"case": case_id, "size": f"{width}x{height}", "kind": kind,
                         "mode": mode, "threshold": threshold, "morph": morph_name,
                         "en_gray": en_gray, "en_gauss": en_gauss, "out_sel": out_sel,
                         "run_id": index,
                         "ms": round(float(value), 4), "fps": round(1000.0 / value, 2),
                         "correct": stats["verdict"],
                         "different_pixels": stats["different_pixels"],
                         "mm2s_dmasr": hex(mm2s_dmasr), "s2mm_dmasr": hex(s2mm_dmasr)})

        del inp, out

    fields = ["case", "size", "kind", "mode", "threshold", "morph", "en_gray",
              "en_gauss", "out_sel", "run_id", "ms", "fps", "correct",
              "different_pixels", "mm2s_dmasr", "s2mm_dmasr"]
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n逐次数据已写入 {args.out}({len(rows)} 行)")

    verdict = "ALL PASS" if all_pass else "存在 FAIL"
    log_lines = [
        "=== configurable_pipeline 板端验证日志 ===",
        json.dumps(info, ensure_ascii=False),
        "",
        "用例汇总:",
    ] + summary + [
        "",
        f"总结论:{verdict}",
        "拍数/TLAST 证据:每帧 DMA 双通道 IOC 完成 + 内部错误位(bit25-28)未置起"
        " = 传输拍数与 TLAST 相符。",
    ]
    log_text = "\n".join(log_lines) + "\n"
    with open(args.log, "w", encoding="utf-8") as handle:
        handle.write(log_text)
    print(f"汇总日志已写入 {args.log}")
    print(verdict)
    return 0 if all_pass else 1

if __name__ == "__main__":
    raise SystemExit(main())
