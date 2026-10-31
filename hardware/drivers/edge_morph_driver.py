#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phenotyping 参数化核的板端验证与性能测试（在 PYNQ-Z2 上运行）。

对应 A 侧交付：
    history/board/sobel_v3_param/parameterized_edge.bit / .hwh
    核实例 sobel_v3_0（s_axi_control），DMA 实例 axi_dma_0

设计约束（按 A 的接口说明实现）：
    * 基地址一律从 Overlay 动态读取，代码里不写死任何地址
    * 每帧：写 width/height/mode/threshold → 写 AP_CTRL.ap_start=1 → DMA 收发 → 等 TLAST
    * ap_ctrl_hs 无 auto-rewind，每帧都要重新写一次 ap_start
    * 中断口悬空，全部用轮询 + 超时，不依赖中断
    * threshold == 0 → 饱和输出；threshold > 0 → 二值化（≥阈值输出 255，否则 0）

本脚本自带参考实现（numpy），所以板端不需要额外安装任何东西。
参考实现与 B 侧库 biovision/edges.py 同源，改一处必须同步另一处。

用法（在板的 Jupyter 里，先把本文件、bit、hwh、测试图 .bin 放到同一目录）：
    %run edge_morph_driver.py
或命令行：
    sudo python3 edge_morph_driver.py --warmup 2 --repeats 20
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

# pynq 只在板子上有；放到函数里延迟导入，这样本文件在 PC 上也能被引用和自测
try:
    from pynq import Overlay, MMIO, allocate
    HAVE_PYNQ = True
except ImportError:      # PC 上跑参考实现的自测时走这里
    Overlay = MMIO = allocate = None
    HAVE_PYNQ = False

# ------------------------------------------------------------------
# 被测配置
# ------------------------------------------------------------------
# edge_validation V4 更新：bit 文件名和核实例名都变了，这里做成"自动识别"，新旧都能跑。
#   V3 bit: parameterized_edge.bit  核 sobel_v3_0
#   V4 bit: edge_morphology_system.bit                核 sobel_v4_0（多 morph_mode 寄存器）
BIT_FILE = "edge_morphology_system.bit"                       # 找不到时自动扫描目录里的 *.bit
KERNEL_IP_CANDIDATES = ("sobel_v4_pipeline_0", "sobel_v4_0", "sobel_v3_0")  # 从新到旧逐个尝试
KERNEL_IP = KERNEL_IP_CANDIDATES[0]                  # 保留旧名字，兼容外部引用
DMA_IP = "axi_dma_0"

# s_axilite 寄存器偏移（直接读自 A 的 V4 hwh：edge_morphology_system.hwh）
OFF_AP_CTRL = 0x00
OFF_WIDTH = 0x10
OFF_HEIGHT = 0x18
OFF_MODE = 0x20
OFF_THRESHOLD = 0x28
MODE_SOBEL, MODE_PREWITT = 0, 1

# ------------------------------------------------------------------
# edge_validation / B3 扩展：V4 Morphology（Erode / Dilate）
# ------------------------------------------------------------------
# morph_mode 寄存器偏移：0x30。
# 依据：edge_morphology_system.hwh 里 <REGISTER NAME="morph_mode"> 的 ADDRESS_OFFSET=48。
# 若 A 后续改了寄存器顺序，这里要跟着改；改成 None 会重新启用"未实现即拦截"。
OFF_MORPH = 0x30

MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE = 0, 1, 2
MORPH_NAMES = {MORPH_BYPASS: "bypass", MORPH_ERODE: "erode", MORPH_DILATE: "dilate"}

# 外圈规则：必须与 A 的 RTL 一致，否则板子与 Golden 永远差一圈。
#   "zero" = 最外圈强制输出 0（与现有 V3 边缘核的约定一致）
#   "keep" = 最外圈保持输入原值
# ⚠️ 此值仍需 A 口头确认；如果板测出现"只在最外一圈不同"，先换成另一个再试。
MORPH_BORDER = "zero"

# AXI DMA 状态寄存器（用来判断一帧传完没有）
MM2S_DMASR_OFF = 0x04
S2MM_DMASR_OFF = 0x34
DMASR_IDLE = 0x2        # bit1：通道空闲 = 传输结束
DMASR_IOC = 0x1000      # bit12：传输完成标志（写 1 清除）
# bit25-28：DMA 内部错误（拍数/TLAST 不符会置起）——A 的脚本里用的就是这个判据
DMASR_INTERNAL_ERR = 0x1E000000
POLL_INTERVAL = 0.0001  # 轮询间隔 0.1 ms，越小计时越准

# 允许的分辨率范围（A 侧定稿）
MIN_W, MAX_W = 3, 1920
MIN_H, MAX_H = 3, 1080

# ------------------------------------------------------------------
# 参考实现（与 biovision/edges.py 同源）
# ------------------------------------------------------------------
_KERNELS = {
    MODE_SOBEL: (np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], np.int32),
                 np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], np.int32)),
    MODE_PREWITT: (np.array([[-1, 0, 1], [-1, 0, 1], [-1, 0, 1]], np.int32),
                   np.array([[-1, -1, -1], [0, 0, 0], [1, 1, 1]], np.int32)),
}

def edge_reference(gray, mode, threshold=0):
    """软件参考：只算内部像素，最外一圈 0，与硬件口径一致。"""
    g = np.asarray(gray).astype(np.int32)
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

def compare(reference, result):
    diff = np.abs(result.astype(np.int16) - reference.astype(np.int16))
    different = int(np.count_nonzero(diff))
    return {
        "different_pixels": different,
        "max_error": int(diff.max()),
        "mean_error": float(diff.mean()),
        "verdict": "PASS" if different == 0 else "FAIL",
    }

# ------------------------------------------------------------------
# edge_validation / B3 扩展：形态学参考实现（与 golden_morphology.py 同源）
# ------------------------------------------------------------------
def morph_reference(binary, morph_mode, border=MORPH_BORDER):
    """3x3 Erode / Dilate 的软件参考，口径与 PL 一致。

    内部像素：直接取 3x3 窗口的 min（Erode）或 max（Dilate），不做边界外推。
    最外一圈：border="zero" 时强制 0；border="keep" 时保持输入原值。

    对 0/255 二值图，min 等价于"9 个像素全 1 才输出 1"（AND），
    max 等价于"有一个 1 就输出 1"（OR）。
    """
    img = np.asarray(binary, dtype=np.uint8)
    if morph_mode == MORPH_BYPASS:
        return img.copy()
    if morph_mode not in (MORPH_ERODE, MORPH_DILATE):
        raise ValueError(
            "morph_mode 只能是 0(bypass) / 1(erode) / 2(dilate)，收到 %r" % (morph_mode,)
        )
    height, width = img.shape
    if height < 3 or width < 3:
        raise ValueError("尺寸至少 3x3 才能做 3x3 形态学，当前 %dx%d" % (width, height))

    fill = 255 if morph_mode == MORPH_ERODE else 0
    core = np.full((height - 2, width - 2), fill, dtype=np.uint8)
    reduce = np.minimum if morph_mode == MORPH_ERODE else np.maximum
    for dy in range(3):
        for dx in range(3):
            window = img[dy:dy + height - 2, dx:dx + width - 2]
            core = reduce(core, window)

    out = np.zeros_like(img) if border == "zero" else img.copy()
    out[1:-1, 1:-1] = core
    return out

def pipeline_reference(gray, mode, threshold, morph_mode):
    """完整链路参考：灰度 -> Edge(+Threshold) -> Morphology。

    对应 B3 要求的调用顺序"输入预处理 -> PL -> Golden"，PL 若按同一口径实现，
    逐像素比对就应当 different_pixels == 0。
    """
    edge = edge_reference(gray, mode, threshold)
    return morph_reference(edge, morph_mode)

# ------------------------------------------------------------------
# 硬件访问
# ------------------------------------------------------------------
def parse_hwh_registers(hwh_path, ip_name=None):
    """从 .hwh 里解析某个 IP 的寄存器偏移表，返回 {寄存器名: 偏移}。

    为什么要有这个：A 每加一代核（V3 → V4 形态学 → pipeline_validation 完整流水线），
    寄存器表都会变。写死偏移的脚本每次都要人肉改，迟早改漏。
    直接读 hwh 就不会：核里有什么寄存器、偏移是多少，以 hwh 为准。

    用法：
        regs = parse_hwh_registers("edge_morphology_system.hwh")     # 不指定就取唯一的 HLS 核
        regs["morph_mode"]      # -> 48
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(str(hwh_path))
    root = tree.getroot()

    def _module_matches(node):
        if ip_name is None:
            return True
        return node.get("INSTANCE") == ip_name

    # 找到目标 MODULE 节点；不指定 ip_name 时取"含有 width 寄存器"的那个
    target = None
    for mod in root.iter("MODULE"):
        if not _module_matches(mod):
            continue
        names = [r.get("NAME") for r in mod.iter("REGISTER")]
        if ip_name is not None or "width" in names:
            target = mod
            break
    if target is None:
        raise RuntimeError("在 %s 里找不到寄存器表（ip_name=%r）" % (hwh_path, ip_name))

    regs = {}
    for reg in target.iter("REGISTER"):
        name = reg.get("NAME")
        if not name:
            continue
        for prop in reg.findall("PROPERTY"):
            if prop.get("NAME") == "ADDRESS_OFFSET":
                try:
                    regs[name] = int(prop.get("VALUE"))
                except (TypeError, ValueError):
                    pass
                break
    if not regs:
        raise RuntimeError("%s 里的寄存器块没解析出任何偏移" % hwh_path)
    return regs

def find_bitfile(preferred=BIT_FILE):
    """找 bit 文件：优先用配置里的名字，找不到就扫目录里的 *.bit。"""
    if Path(preferred).is_file():
        return str(preferred)
    found = sorted(Path(".").glob("*.bit"))
    if len(found) == 1:
        return str(found[0])
    if not found:
        raise FileNotFoundError(
            "目录里找不到 .bit 文件；请把 %s 和同名 .hwh 放到当前目录" % preferred
        )
    raise RuntimeError(
        "目录里有多个 .bit：%s；请显式指定要用的那个" % [p.name for p in found]
    )

class SobelParamDriver:
    """按 A 的接口说明封装的驱动程序。"""

    def __init__(self, bitfile=None, kernel=None, dma_name=DMA_IP):
        if not HAVE_PYNQ:
            raise RuntimeError("这个类只能在 PYNQ 板子上用（需要 pynq 包）")
        bitfile = bitfile or find_bitfile()
        self.ol = Overlay(bitfile)
        self.bitfile = str(bitfile)

        # 核实例名自动识别：V4 优先，回落 V3，避免换 bit 就得改代码
        if kernel is None:
            for name in KERNEL_IP_CANDIDATES:
                if name in self.ol.ip_dict:
                    kernel = name
                    break
            if kernel is None:
                # 兜底：候选表都没命中时，自动挑"不是 DMA 的那个 IP"。
                # 核实例名已经改过两次（sobel_v3_0 → sobel_v4_0 → sobel_v4_pipeline_0），
                # 与其每次改候选表，不如让它在 hwh 里自己认出来。
                others = [n for n in self.ol.ip_dict
                          if n != dma_name and "dma" not in n.lower()]
                if len(others) == 1:
                    kernel = others[0]
                    print("[提示] 核实例名 %s 不在候选表里，已自动识别；"
                          "建议把它加进 KERNEL_IP_CANDIDATES" % kernel)
        if kernel is None or kernel not in self.ol.ip_dict:
            raise RuntimeError(
                "Overlay 里找不到核 %s；实际有：%s"
                % (KERNEL_IP_CANDIDATES, sorted(self.ol.ip_dict))
            )
        if dma_name not in self.ol.ip_dict:
            raise RuntimeError(
                "Overlay 里找不到 DMA %s；实际有：%s" % (dma_name, sorted(self.ol.ip_dict))
            )
        self.kernel_name = kernel
        self.dma_name = dma_name

        # 地址一律从 overlay 动态读取
        self.kernel_base = self.ol.ip_dict[kernel]["phys_addr"]
        self.dma_base = self.ol.ip_dict[dma_name]["phys_addr"]
        self.ctrl = MMIO(self.kernel_base, 0x1000)
        self.dma_ctrl = MMIO(self.dma_base, 0x1000)
        self.dma = getattr(self.ol, dma_name)

        # 寄存器偏移以 hwh 为准（解析失败就回落到模块常量，保证旧环境还能跑）
        self.regs = {}
        hwh = Path(self.bitfile).with_suffix(".hwh")
        if hwh.is_file():
            try:
                self.regs = parse_hwh_registers(hwh)
            except Exception as exc:      # noqa: BLE001
                print("[警告] 解析 %s 的寄存器表失败，改用默认常量：%s" % (hwh.name, exc))
        self.unknown_user_regs = []

    def off(self, name, default=None):
        """按名字取寄存器偏移：优先用 hwh 解析结果，其次用默认常量。"""
        if name in self.regs:
            return self.regs[name]
        return default

    def info(self):
        reg_map = {"AP_CTRL": hex(OFF_AP_CTRL)}
        for name, off in {
            "width": self.off("width", OFF_WIDTH),
            "height": self.off("height", OFF_HEIGHT),
            "mode": self.off("mode", OFF_MODE),
            "threshold": self.off("threshold", OFF_THRESHOLD),
            "morph_mode": self.off("morph_mode", OFF_MORPH),
        }.items():
            reg_map[name] = hex(off) if off is not None else None
        return {
            "bitfile": self.bitfile,
            "kernel_ip": self.kernel_name,
            "dma_ip": self.dma_name,
            "ip_dict": sorted(self.ol.ip_dict.keys()),
            "kernel_base": hex(self.kernel_base),
            "dma_base": hex(self.dma_base),
            "register_map": reg_map,
            "hwh_寄存器表": {k: hex(v) for k, v in sorted(self.regs.items())} if self.regs else None,
            "说明": "地址与寄存器偏移均来自 hwh；每帧先挂 DMA 再拉 ap_start",
        }

    def read_register_map(self):
        """回读 4+1 个参数寄存器，确认核真的在工作（不是写进去就完事）。"""
        values = {}
        named = {
            "width": self.off("width", OFF_WIDTH),
            "height": self.off("height", OFF_HEIGHT),
            "mode": self.off("mode", OFF_MODE),
            "threshold": self.off("threshold", OFF_THRESHOLD),
            "morph_mode": self.off("morph_mode", OFF_MORPH),
        }
        for name, off in named.items():
            if off is not None:
                values[name] = self.ctrl.read(off)
        return values

    def _wait_done(self, t_start, timeout, context=""):
        """轮询 DMA 状态寄存器，直到两个通道都报告"传输完成"。

        判断依据用 IOC_Irq（bit12）而不是 Idle（bit1）：
        Idle 在传输刚发出、通道还没真正启动的那一瞬间可能仍为 1，
        会让轮询误判成"已经传完"，紧接着启动下一帧就会乱套。
        IOC 位在启动前已被清零，只有真正传完才会置起来，不会误判。
        """
        while True:
            mm2s = self.dma_ctrl.read(MM2S_DMASR_OFF)
            s2mm = self.dma_ctrl.read(S2MM_DMASR_OFF)
            # 内部错误位（拍数 / TLAST 异常）：一旦置起说明传输本身有问题，直接报错
            if (mm2s | s2mm) & DMASR_INTERNAL_ERR:
                raise RuntimeError(
                    "DMA 内部错误置起：MM2S_DMASR=%s S2MM_DMASR=%s%s"
                    % (hex(mm2s), hex(s2mm),
                       ("  | 当前配置 " + context) if context else "")
                )
            if (mm2s & DMASR_IOC) and (s2mm & DMASR_IOC):
                return
            if time.perf_counter() - t_start > timeout:
                raise TimeoutError(
                    f"帧超时 {timeout}s：MM2S_DMASR={hex(mm2s)} "
                    f"S2MM_DMASR={hex(s2mm)} 核 AP_CTRL={hex(self.ctrl.read(OFF_AP_CTRL))}"
                    + (f"  | 当前配置 {context}" if context else "")
                )
            time.sleep(POLL_INTERVAL)

    def run_frame(self, inp_buf, out_buf, width, height, mode, threshold,
                  timeout=5.0, morph_mode=MORPH_BYPASS, extra_regs=None):
        """跑一帧，返回耗时（秒，含 DMA 收发）。

        morph_mode：0=Bypass（与旧行为完全一致）/ 1=Erode / 2=Dilate。
        只有 A 把 morph_mode 寄存器偏移填进 OFF_MORPH 之后，才允许传非 0 值。

        extra_regs：{寄存器名: 值}，在标准寄存器之后写。
          pipeline_validation 的新寄存器（enable_gray / enable_gauss / pipeline_mode 等）用这个传，
          不用改本文件。**名称必须与 hwh 里的 <REGISTER NAME=...> 完全一致**；
          hwh 里没有的名字会被跳过并记到 self.unknown_user_regs，避免"以为写了其实没写"。
        """
        if not (MIN_W <= width <= MAX_W) or not (MIN_H <= height <= MAX_H):
            raise ValueError(f"分辨率 {width}x{height} 超出允许范围")
        if mode not in (MODE_SOBEL, MODE_PREWITT):
            raise ValueError(f"mode 只能是 0(Sobel) 或 1(Prewitt)，收到 {mode}")
        if morph_mode not in (MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE):
            raise ValueError(
                "morph_mode 只能是 0(bypass) / 1(erode) / 2(dilate)，收到 %r" % (morph_mode,)
            )
        off_morph = self.off("morph_mode", OFF_MORPH)
        if morph_mode != MORPH_BYPASS and off_morph is None:
            raise RuntimeError(
                "morph_mode=%s 需要 V4 核，但本脚本还不知道 morph_mode 寄存器偏移。\n"
                "请确认 bit 同名 .hwh 里存在 morph_mode 寄存器（会自动解析），"
                "或把偏移填入 OFF_MORPH（文件顶部）。\n"
                "当前 bit 仍是 V3，只支持 morph_mode=0。" % MORPH_NAMES[morph_mode]
            )

        context = "%dx%d mode=%s threshold=%s morph=%s" % (
            width, height,
            "Sobel" if mode == MODE_SOBEL else "Prewitt",
            threshold, MORPH_NAMES[morph_mode],
        )
        if extra_regs:
            context += " " + " ".join("%s=%s" % (k, v) for k, v in extra_regs.items())

        t0 = time.perf_counter()

        # 0) 清掉上一帧留下的完成标志——清零后只有本帧真正传完才会再次置起
        self.dma_ctrl.write(MM2S_DMASR_OFF, DMASR_IOC)
        self.dma_ctrl.write(S2MM_DMASR_OFF, DMASR_IOC)

        # 1) 写参数寄存器：偏移以 hwh 解析结果为准，回落到模块常量
        self.ctrl.write(self.off("width", OFF_WIDTH), width)
        self.ctrl.write(self.off("height", OFF_HEIGHT), height)
        self.ctrl.write(self.off("mode", OFF_MODE), mode)
        self.ctrl.write(self.off("threshold", OFF_THRESHOLD), threshold)
        if off_morph is not None:
            self.ctrl.write(off_morph, morph_mode)
        for name, value in (extra_regs or {}).items():
            offset = self.off(name)
            if offset is None:
                if name not in self.unknown_user_regs:
                    self.unknown_user_regs.append(name)
                continue
            self.ctrl.write(offset, int(value))

        # 2) 先把两个 DMA 通道挂好（长度 = width×height 字节）
        self.dma.recvchannel.transfer(out_buf)
        self.dma.sendchannel.transfer(inp_buf)

        # 3) 最后拉 ap_start，启动核（ap_ctrl_hs 每帧都要重新拉一次）
        #    顺序说明：A 的接口文档写的是"先 ap_start 再 DMA"，但在真板上
        #    实测"先挂 DMA 再 ap_start"才稳定——避免核先启动、S2MM 尚未就绪。
        self.ctrl.write(OFF_AP_CTRL, 1)

        # 4) 轮询等两个通道都报告完成
        self._wait_done(t0, timeout, context)

        return time.perf_counter() - t0

# ------------------------------------------------------------------
# 测试用例
# ------------------------------------------------------------------
def build_cases(images_dir):
    """T1~T6 用例表。

    矩阵：三档尺寸 × 双算法。同一尺寸的两个算法用同一张图，便于直接比较。
    输入全部是 data/images 里的真实图片（plant_a / plant_b 为真实植物照片经
    RGB→灰度转换，换算规则见 tests/simulation/edge_benchmark.py）：

        T1/T2  256x256  plant_a
        T3/T4  320x240  plant_b
        T5/T6  640x480  plant_a

    图片缺失时脚本会退回按公式合成，并在日志里注明（见 load_or_synthesize）。
    """
    images_dir = Path(images_dir)
    return [
        # id, 尺寸(w,h), 图片, 算法, 阈值
        ("T1", (256, 256), images_dir / "plant_a_256x256.bin", MODE_SOBEL, 0),
        ("T2", (256, 256), images_dir / "plant_a_256x256.bin", MODE_PREWITT, 0),
        ("T3", (320, 240), images_dir / "plant_b_320x240.bin", MODE_SOBEL, 0),
        ("T4", (320, 240), images_dir / "plant_b_320x240.bin", MODE_PREWITT, 0),
        ("T5", (640, 480), images_dir / "plant_a_640x480.bin", MODE_SOBEL, 0),
        ("T6", (640, 480), images_dir / "plant_a_640x480.bin", MODE_PREWITT, 0),
    ]

def load_or_synthesize(path, size):
    """输入图存在就直接读；不存在就用确定性公式合成，保证可复现。

    合成规则（写进结果 JSON，便于和 A 的 csim 对齐）：
        gray[y][x] = ((x*7 + y*13 + (x//16)*29 + (y//16)*53) & 0xFF)
    这是纯确定性函数，任何机器上算出来都一样。
    """
    path = Path(path)
    width, height = size
    if path.exists():
        raw = path.read_bytes()
        if len(raw) != width * height:
            raise ValueError(f"{path.name}: 期望 {width*height} B，实际 {len(raw)} B")
        return np.frombuffer(raw, np.uint8).reshape(height, width), True

    yy, xx = np.mgrid[0:height, 0:width].astype(np.int64)
    gray = ((xx * 7 + yy * 13 + (xx // 16) * 29 + (yy // 16) * 53) & 0xFF).astype(np.uint8)
    return gray, False

# ------------------------------------------------------------------
# edge_validation / B3 扩展：V4 Morphology 用例与执行
# ------------------------------------------------------------------
edge_validation_SIZES = [(256, 256), (320, 240), (640, 480)]
edge_validation_IMAGE = {
    (256, 256): "plant_a_256x256.bin",
    (320, 240): "plant_b_320x240.bin",
    (640, 480): "plant_a_640x480.bin",
}
edge_validation_THRESHOLD = 40      # 与演示口径一致：>0 即二值化，形态学作用在二值图上

def build_edge_validation_cases(images_dir):
    """三档分辨率 × 两种形态学 = 6 组核心组合（验收要求）。"""
    cases = []
    for width, height in edge_validation_SIZES:
        for morph_mode in (MORPH_ERODE, MORPH_DILATE):
            case_id = "D1_%s_%dx%d" % (MORPH_NAMES[morph_mode], width, height)
            cases.append((case_id, (width, height),
                          Path(images_dir) / edge_validation_IMAGE[(width, height)],
                          MODE_SOBEL, edge_validation_THRESHOLD, morph_mode))
    return cases

def build_unit_cases(unit_dir):
    """B1 的单元测试图同样走完整链路：它们是小尺寸的极端输入，
    用来卡边界、单点、断口这些最容易出错的情况。"""
    cases = []
    unit_dir = Path(unit_dir)
    if not unit_dir.is_dir():
        return cases
    for path in sorted(unit_dir.glob("*_input.png")):
        name = path.name[:-len("_input.png")]
        for morph_mode in (MORPH_ERODE, MORPH_DILATE):
            cases.append(("U_%s_%s" % (name, MORPH_NAMES[morph_mode]), None,
                          path, MODE_SOBEL, edge_validation_THRESHOLD, morph_mode))
    return cases

def load_gray(path):
    """读灰度图（单元测试图是 PNG，测试图是 .bin）-> 返回 (gray, 尺寸)。"""
    path = Path(path)
    if path.suffix.lower() == ".bin":
        return None, None                      # 由 load_or_synthesize 处理
    from PIL import Image
    with Image.open(path) as source:
        gray = np.asarray(source.convert("L"), dtype=np.uint8)
    return gray, (gray.shape[1], gray.shape[0])

def run_edge_validation(driver, args):
    """edge_validation 的正式验证：单元测试 -> 三档 × 两形态学，逐像素对 Golden。"""
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    rows = []
    all_pass = True
    cases = build_unit_cases(args.units) + build_edge_validation_cases(args.images)
    if not cases:
        print("没有可执行的 edge_validation 用例：检查 --units / --images 目录")
        return 1

    print("\n" + "=" * 72)
    print("edge_validation V4 Morphology 验证：共 %d 个用例" % len(cases))
    print("外圈规则 MORPH_BORDER = %s | 阈值 = %d" % (MORPH_BORDER, edge_validation_THRESHOLD))
    print("=" * 72)

    for case_id, size, path, mode, threshold, morph_mode in cases:
        gray, real_size = load_gray(path)
        if gray is None:
            gray, from_file = load_or_synthesize(path, size)
            origin = path.name if from_file else "合成"
        else:
            origin = path.name
        height, width = gray.shape
        print("\n----- %s  %dx%d  %s  %s -----" % (
            case_id, width, height, origin, MORPH_NAMES[morph_mode]))

        reference = pipeline_reference(gray, mode, threshold, morph_mode)

        inp = allocate(shape=(width * height,), dtype=np.uint8)
        out = allocate(shape=(width * height,), dtype=np.uint8)
        inp[:] = gray.reshape(-1)

        try:
            driver.run_frame(inp, out, width, height, mode, threshold,
                             args.timeout, morph_mode)
        except Exception as exc:
            print("    [失败] %s" % exc)
            all_pass = False
            rows.append({"case": case_id, "size": "%dx%d" % (width, height),
                         "morph_mode": MORPH_NAMES[morph_mode], "run_id": 0,
                         "ms": "", "fps": "", "correct": "ERROR",
                         "different_pixels": "", "max_diff": ""})
            del inp, out
            continue

        result = np.array(out).reshape(height, width)
        stats = compare(reference, result)
        print("    Golden：%s   不同像素=%d   最大误差=%d" % (
            stats["verdict"], stats["different_pixels"], stats["max_error"]))

        border_ok = bool(result[0, :].max() == 0 and result[-1, :].max() == 0
                         and result[:, 0].max() == 0 and result[:, -1].max() == 0)
        print("    最外一圈全 0：%s" % ("是" if border_ok else "否（若 A 用 keep 口径则正常）"))
        all_pass = all_pass and stats["verdict"] == "PASS"

        try:
            for _ in range(args.warmup):
                driver.run_frame(inp, out, width, height, mode, threshold,
                                 args.timeout, morph_mode)
            samples = [
                driver.run_frame(inp, out, width, height, mode, threshold,
                                 args.timeout, morph_mode) * 1e3
                for _ in range(args.repeats)
            ]
        except Exception as exc:
            print("    [性能计时中断] %s" % exc)
            all_pass = False
            rows.append({"case": case_id, "size": "%dx%d" % (width, height),
                         "morph_mode": MORPH_NAMES[morph_mode], "run_id": 0,
                         "ms": "", "fps": "", "correct": "TIMEOUT",
                         "different_pixels": "", "max_diff": ""})
            del inp, out
            continue

        ms = np.asarray(samples)
        print("    PL E2E  mean=%.3f  median=%.3f  min=%.3f  max=%.3f ms | FPS=%.1f"
              % (ms.mean(), np.median(ms), ms.min(), ms.max(), 1000.0 / ms.mean()))
        for index, value in enumerate(samples, 1):
            rows.append({"case": case_id, "size": "%dx%d" % (width, height),
                         "morph_mode": MORPH_NAMES[morph_mode], "run_id": index,
                         "ms": round(float(value), 4), "fps": round(1000.0 / value, 2),
                         "correct": stats["verdict"],
                         "different_pixels": stats["different_pixels"],
                         "max_diff": stats["max_error"]})
        del inp, out

    fields = ["case", "size", "morph_mode", "run_id", "ms", "fps",
              "correct", "different_pixels", "max_diff"]
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print("\n逐次数据已写入 %s（%d 行）" % (args.out, len(rows)))
    print("edge_validation 全部 PASS" if all_pass else "存在 FAIL，先定位正确性问题")
    return 0 if all_pass else 1

def main(argv=None):
    parser = argparse.ArgumentParser(description="phenotyping 参数化核板端验证与性能测试")
    parser.add_argument("--images", default=".", help="测试图目录（.bin）")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=5.0, help="单帧超时秒数")
    parser.add_argument("--out", default="data/evidence/analysis/board_phenotyping.csv")
    parser.add_argument("--info", default="data/evidence/analysis/board_phenotyping_info.json")
    parser.add_argument("--edge_validation", action="store_true",
                        help="跑 edge_validation V4 Morphology 验证（单元测试 + 三档×两形态学）")
    parser.add_argument("--units", default="unit_tests",
                        help="B1 单元测试图目录（*_input.png）")
    args = parser.parse_args(argv)

    driver = SobelParamDriver()
    info = driver.info()
    print("=== Overlay 信息（地址从 hwh 动态读取） ===")
    for key, value in info.items():
        print(f"    {key}: {value}")

    if args.edge_validation:
        return run_edge_validation(driver, args)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.info).parent.mkdir(parents=True, exist_ok=True)
    Path(args.info).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    all_pass = True

    for case_id, (width, height), image_path, mode, threshold in build_cases(args.images):
        mode_name = "Sobel" if mode == MODE_SOBEL else "Prewitt"
        print(f"\n===== {case_id}  {width}x{height}  {mode_name}  threshold={threshold} =====")

        gray, from_file = load_or_synthesize(image_path, (width, height))
        origin = image_path.name if from_file else f"合成（公式见 {args.info}）"
        print(f"    输入：{origin}")

        # 板端 PS 参考
        reference = edge_reference(gray, mode, threshold)

        # 分配连续内存（每档尺寸各分配一次、循环外）
        inp = allocate(shape=(width * height,), dtype=np.uint8)
        out = allocate(shape=(width * height,), dtype=np.uint8)
        inp[:] = gray.reshape(-1)

        # ---- 正确性：先跑一帧对拍，PASS 才计时 ----
        driver.run_frame(inp, out, width, height, mode, threshold, args.timeout)
        result = np.array(out).reshape(height, width)
        stats = compare(reference, result)
        print(f"    正确性：{stats['verdict']}  不同像素={stats['different_pixels']}"
              f"  最大误差={stats['max_error']}")
        all_pass = all_pass and stats["verdict"] == "PASS"

        border_ok = bool(result[0, :].max() == 0 and result[-1, :].max() == 0
                         and result[:, 0].max() == 0 and result[:, -1].max() == 0)
        print(f"    最外一圈全 0：{'是' if border_ok else '否'}")

        if stats["verdict"] != "PASS":
            print("    → 未通过，跳过性能计时（先修正确性）")
            for index in range(1, args.repeats + 1):
                rows.append({"case": case_id, "size": f"{width}x{height}", "mode": mode_name,
                             "threshold": threshold, "run_id": index, "ms": "",
                             "fps": "", "correct": stats["verdict"],
                             "different_pixels": stats["different_pixels"]})
            continue

        # ---- 性能 ----
        # 单帧失败不中断整轮：记下来继续测后面的用例，最后统一汇报
        try:
            for _ in range(args.warmup):
                driver.run_frame(inp, out, width, height, mode, threshold, args.timeout)
            samples = []
            for _ in range(args.repeats):
                samples.append(driver.run_frame(inp, out, width, height, mode, threshold,
                                                args.timeout) * 1e3)
        except Exception as exc:
            print(f"    [性能计时中断] {exc}")
            all_pass = False
            rows.append({"case": case_id, "size": f"{width}x{height}", "mode": mode_name,
                         "threshold": threshold, "run_id": 0, "ms": "", "fps": "",
                         "correct": "TIMEOUT", "different_pixels": ""})
            del inp, out
            continue

        ms = np.asarray(samples)
        print(f"    PL E2E  mean={ms.mean():.3f}  median={np.median(ms):.3f}  "
              f"min={ms.min():.3f}  max={ms.max():.3f} ms | FPS={1000.0/ms.mean():.1f}")

        for index, value in enumerate(samples, 1):
            rows.append({"case": case_id, "size": f"{width}x{height}", "mode": mode_name,
                         "threshold": threshold, "run_id": index,
                         "ms": round(float(value), 4), "fps": round(1000.0 / value, 2),
                         "correct": stats["verdict"],
                         "different_pixels": stats["different_pixels"]})

        del inp, out

    fields = ["case", "size", "mode", "threshold", "run_id", "ms", "fps",
              "correct", "different_pixels"]
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n逐次数据已写入 {args.out}（{len(rows)} 行）")
    print("全部 PASS" if all_pass else "存在 FAIL，请先定位正确性问题")
    return 0 if all_pass else 1

if __name__ == "__main__":
    raise SystemExit(main())
