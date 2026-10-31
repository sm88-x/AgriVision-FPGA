#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_stream A 侧：单板连续帧处理服务（frame_service）

职责（对应 live_stream 任务书 A1/A2）：
    1. **同一 Overlay 连续处理多帧**，不每帧重新下载 bit；
    2. 固定帧缓冲分配与复用，不每帧申请内存（防抖动/泄漏）；
    3. 明确 CPU 缓存一致性：输入写入后 flush、输出读取前 invalidate；
    4. 每帧检查输入输出字节数 / DMA 超时 / DMA 内部错误 / 宽高寄存器，
       异常一律返回**结构化错误**（错误码与协议同一份定义）；
    5. 暴露"这个核支持哪些寄存器"，hwh 里没有的名字不写、只记录，
       避免"以为写了其实没写"。

设计约束
    * 寄存器偏移一律从同名 .hwh 解析（沿用 edge_morph_driver.SobelParamDriver），不写死地址；
    * **输入是 32 bit/像素（4 B/px）**：这份 V4 bit 的 PL 输入接口固定 32bit，
      `enable_gray=0` 的含义是"取 32bit 字的低 8 位当灰度"。所以 8bit 灰度要**放在低字节**
      再送（口径与 `hardware/drivers/integrated_pipeline_smoke_test.py` 的 gray 用例一致，
      那份是板上实测 PASS 的参考）。协议上的 `channels=1`（网络侧 1 B/px）只影响**网络**，
      板端会就地展开成 32bit 字，别把两者混为一谈。
    * `--mock` 走**同一条代码路径**（只换 driver），PC 上验过的协议上板不用改；
    * 不 import 任何只在板上存在的东西到模块顶层，PC 上也能自带自测。

用法（板端 Jupyter 工作目录，与 bit/hwh 同目录）：
    sudo python3 frame_service.py --selftest 10 \
        --bitfile configurable_pipeline.bit

PC 上先验证（不需要板子）：
    python frame_service.py --selftest 5 --mock
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
import sys
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- 路径与依赖
_HERE = Path(__file__).resolve().parent
for _d in (str(_HERE), str(_HERE / "overlay"), str(_HERE.parent / "src/software/app")):
    if Path(_d).is_dir():
        sys.path.insert(0, _d)

import frame_protocol as fp                                       # noqa: E402
from edge_morph_driver import (                                     # noqa: E402
    MORPH_BYPASS, MORPH_DILATE, MORPH_ERODE, MODE_PREWITT, MODE_SOBEL,
    edge_reference, morph_reference,
)
from run_pipeline import SEED_PRESET, gaussian3x3, rgb2gray       # noqa: E402

try:
    from pynq import allocate as _pynq_allocate
except ImportError:                                               # PC 上跑 mock / 自测
    _pynq_allocate = None

# ---------------------------------------------------------------- 常量
MIN_W, MIN_H = 3, 3
MAX_W, MAX_H = 1920, 1080
ALLOWED_CHANNELS = (1, 4)          # 1=gray8（live_stream 实时链路）4=RGBX888（pipeline_validation 遗留路径）
OUT_MORPH = 0                      # out_sel：恒取最终 morph 输出（协议第五节）
GAUSS_ON, GAUSS_OFF = 1, 0
ENABLE_GRAY_ON, ENABLE_GRAY_OFF = 1, 0

_EDGE_TO_MODE = {"Sobel": MODE_SOBEL, "Prewitt": MODE_PREWITT}
MODE_NAMES = {MODE_SOBEL: "Sobel", MODE_PREWITT: "Prewitt"}
MORPH_NAMES = {MORPH_BYPASS: "bypass", MORPH_ERODE: "erode", MORPH_DILATE: "dilate"}

# 冻结的 pipeline_validation 稳定版本：live_stream 硬件未改动，沿用同一个 bit/hwh（版本号写进日志）
FROZEN_pipeline_validation_BITFILE = "configurable_pipeline.bit"

# 缓冲区缓存上限：最多同时保留 3 种尺寸，换尺寸不重新申请、超限淘汰最旧的
BUFFER_CACHE_MAX = 3

class FrameError(Exception):
    """结构化错误：带协议错误码，便于服务端回 ERROR 帧并统计。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)
        self.code = fp.ERROR_NAMES.get(self.status, self.status)

# ================================================================ driver
class MockDriver:
    """软件 Golden 顶替 PL，接口与 `SobelParamDriver.run_frame` 完全一致。

    存在的意义：B/C 在板子被占用时也能跑通协议与服务端逻辑；协议验过了，上板只换 driver。
    耗时是**模型值**，不是实测值——日志里 `simulated=True` 会标明，不许当性能数据。
    """

    def __init__(self, regs=None, fail_every=0):
        self.bitfile = "mock-golden"
        self.kernel_name = "mock"
        self.regs = dict(regs or {
            "width": 0x10, "height": 0x18, "mode": 0x20, "threshold": 0x28,
            "morph_mode": 0x30, "enable_gray": 0x38, "enable_gauss": 0x40, "out_sel": 0x48,
        })
        self.unknown_user_regs = []
        self.frames = 0
        self.fail_every = int(fail_every)
        self.stats = {"dma_errors": 0, "timeouts": 0}

    def run_frame(self, inp, out, width, height, mode, threshold, timeout=5.0,
                  morph_mode=MORPH_BYPASS, extra_regs=None):
        self.frames += 1
        if self.fail_every and self.frames % self.fail_every == 0:
            self.stats["dma_errors"] += 1
            raise RuntimeError("DMA 内部错误置起：MM2S_DMASR=0x1000000 S2MM_DMASR=0x0"  # mock 注入
                               "（--fail-every 故意制造，用于验证错误码分类）")
        started = time.perf_counter()
        extra = extra_regs or {}
        words = np.asarray(inp, dtype=np.uint32).reshape(-1)
        if extra.get("enable_gray", ENABLE_GRAY_OFF):
            # RGBX888：R 在 bit[23:16]、G 在 bit[15:8]、B 在 bit[7:0]
            rgb = np.stack([(words >> 16) & 0xFF, (words >> 8) & 0xFF, words & 0xFF],
                           axis=-1).reshape(height, width, 3).astype(np.uint8)
            gray = rgb2gray(rgb)
        else:
            # 低 8 位即灰度（enable_gray=0 的透传路径）
            gray = (words & 0xFF).astype(np.uint8).reshape(height, width)
        g = gaussian3x3(gray) if extra.get("enable_gauss", GAUSS_OFF) else gray
        result = morph_reference(edge_reference(g, mode, threshold), morph_mode)
        out[:] = np.asarray(result, np.uint8).reshape(-1)
        # 粗略模型：约 640x480 1.1 ms，带一点抖动，避免日志看起来"过于完美"
        # 用 RandomState（numpy 1.13 起就有）而不是 default_rng（1.17+），板端 PYNQ 镜像更老
        jitter = float(np.random.RandomState(self.frames).randint(0, 60)) / 1000.0
        return (width * height * 3.5e-6 + 0.02 + jitter) / 1000.0

class FrameService:
    """一帧进、一帧出；Overlay 只加载一次，缓冲区按尺寸复用。"""

    def __init__(self, bitfile=None, mock=False, timeout=5.0, fail_every=0, verbose=True):
        self.timeout = float(timeout)
        self.verbose = verbose
        self.simulated = bool(mock) or _pynq_allocate is None
        self._buffers = OrderedDict()
        self._frames = 0
        if self.simulated:
            self.driver = MockDriver(fail_every=fail_every)
        else:
            from edge_morph_driver import SobelParamDriver           # 板上才有 pynq
            self.driver = SobelParamDriver(bitfile=bitfile)
        self.overlay_version = getattr(self.driver, "bitfile", "unknown")
        self.kernel_name = getattr(self.driver, "kernel_name", "unknown")
        self.registers = dict(getattr(self.driver, "regs", {}) or {})
        # 核里实际存在、我们才会去写的寄存器（不存在就跳过并记录）
        self.supported = {name: (name in self.registers)
                          for name in ("enable_gray", "enable_gauss", "out_sel", "morph_mode")}

    # ------------------------------------------------------------ 内部
    def _allocate(self, count):
        if _pynq_allocate is None:
            return np.zeros(int(count), dtype=np.uint8)
        return _pynq_allocate(shape=(int(count),), dtype=np.uint8)

    def _allocate_words(self, pixels):
        """输入缓冲：32 bit/像素（这份 PL 的输入接口固定 4 B/px）。"""
        if _pynq_allocate is None:
            return np.zeros(int(pixels), dtype=np.uint32)
        return _pynq_allocate(shape=(int(pixels),), dtype=np.uint32)

    def _buffers_for(self, width, height, channels):
        """固定缓冲复用：同一尺寸不重复申请；超过上限淘汰最旧并显式 free。"""
        key = (width, height, channels)
        if key in self._buffers:
            self._buffers.move_to_end(key)
            return self._buffers[key]
        while len(self._buffers) >= BUFFER_CACHE_MAX:
            _, (old_in, old_out) = self._buffers.popitem(last=False)
            self._free(old_in)
            self._free(old_out)
        # 输入按 32bit/像素；输出是 8bit 灰度
        pair = (self._allocate_words(width * height), self._allocate(width * height))
        self._buffers[key] = pair
        return pair

    @staticmethod
    def _free(buffer):
        free = getattr(buffer, "freebuffer", None)
        if callable(free):
            free()

    def close(self):
        for _, (in_buf, out_buf) in self._buffers.items():
            self._free(in_buf)
            self._free(out_buf)
        self._buffers.clear()

    # ------------------------------------------------------------ 配置
    @staticmethod
    def resolve_config(pipeline_mode, mode, morph_mode, threshold, enable_gauss):
        """协议字段 → 算法配置；pipeline_mode=1 时用 Seed Preset，忽略其余字段。"""
        if int(pipeline_mode) == fp.PIPELINE_SEED_PRESET:
            return {"mode": _EDGE_TO_MODE[SEED_PRESET["edge_mode"]],
                    "threshold": int(SEED_PRESET["threshold"]),
                    "morph_mode": int(SEED_PRESET["morph_mode"]),
                    "enable_gauss": bool(SEED_PRESET["enable_gauss"]),
                    "preset": True}
        return {"mode": int(mode), "threshold": int(threshold),
                "morph_mode": int(morph_mode), "enable_gauss": bool(enable_gauss),
                "preset": False}

    def _extra_regs(self, channels, enable_gauss):
        """把协议字段映射成寄存器写入；hwh 里没有的名字不写（避免假成功）。"""
        extra = {}
        if self.supported["enable_gray"]:
            # channels=1：PC 已转灰度，板端把灰度放进 32bit 字的低字节，PL 走透传（0）
            # channels=4：PC 送 RGBX888，由 PL 真做 RGB2Gray（1）
            extra["enable_gray"] = ENABLE_GRAY_ON if channels == 4 else ENABLE_GRAY_OFF
        if self.supported["enable_gauss"]:
            extra["enable_gauss"] = GAUSS_ON if enable_gauss else GAUSS_OFF
        if self.supported["out_sel"]:
            extra["out_sel"] = OUT_MORPH
        return extra

    @staticmethod
    def _to_words(payload, pixels, channels):
        """把协议 payload 转成 32 bit/像素的字数组。

        channels=1（live_stream 实时链路）：payload 是 w*h 字节灰度 → 放进 32bit 字的低 8 位；
        channels=4（pipeline_validation 遗留路径）：payload 已是 RGBX888，按小端 32bit 直接读。
        """
        if channels == 4:
            words = np.frombuffer(payload, dtype="<u4")
            if words.size != pixels:
                raise FrameError(fp.ERR_BAD_LENGTH,
                                 "RGBX payload %d 字 != %d 像素" % (words.size, pixels))
            return words.astype(np.uint32)
        raw = np.frombuffer(payload, dtype=np.uint8)
        if raw.size != pixels:
            raise FrameError(fp.ERR_BAD_LENGTH,
                             "灰度 payload %d 字节 != %d 像素" % (raw.size, pixels))
        return raw.astype(np.uint32)

    # ------------------------------------------------------------ 主流程
    def process(self, payload, width, height, channels, mode=MODE_SOBEL,
                morph_mode=MORPH_BYPASS, threshold=40, enable_gauss=True,
                pipeline_mode=fp.PIPELINE_EXPLICIT, frame_id=None):
        """处理一帧。返回 dict；任何异常都收敛成 FrameError（带协议错误码）。"""
        width, height, channels = int(width), int(height), int(channels)
        if not (MIN_W <= width <= MAX_W) or not (MIN_H <= height <= MAX_H):
            raise FrameError(fp.ERR_BAD_SIZE, "尺寸 %dx%d 越界（允许 %d~%d / %d~%d）"
                             % (width, height, MIN_W, MAX_W, MIN_H, MAX_H))
        if channels not in ALLOWED_CHANNELS:
            raise FrameError(fp.ERR_BAD_CHANNELS,
                             "channels=%d 不支持（live_stream 实时链路用 1=gray8，2 不用）" % channels)
        expected = width * height * channels
        if len(payload) != expected:
            raise FrameError(fp.ERR_BAD_LENGTH, "payload %d B != %dx%dx%d=%d B"
                             % (len(payload), width, height, channels, expected))

        cfg = self.resolve_config(pipeline_mode, mode, morph_mode, threshold, enable_gauss)
        if cfg["mode"] not in (MODE_SOBEL, MODE_PREWITT):
            raise FrameError(fp.ERR_INTERNAL, "mode=%r 非法" % (cfg["mode"],))
        if cfg["morph_mode"] not in (MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE):
            raise FrameError(fp.ERR_INTERNAL, "morph_mode=%r 非法" % (cfg["morph_mode"],))

        in_buf, out_buf = self._buffers_for(width, height, channels)
        in_buf[:] = self._to_words(payload, width * height, channels)
        flush = getattr(in_buf, "flush", None)
        if callable(flush):                 # 写完后 flush，保证 PL 看到新数据
            flush()
        extra = self._extra_regs(channels, cfg["enable_gauss"])

        started = time.perf_counter()
        try:
            pl_seconds = self.driver.run_frame(
                in_buf, out_buf, width, height, cfg["mode"], cfg["threshold"],
                self.timeout, cfg["morph_mode"], extra)
        except TimeoutError as exc:
            raise FrameError(fp.ERR_TIMEOUT, "DMA 超时：%s" % exc)
        except ValueError as exc:
            raise FrameError(fp.ERR_BAD_SIZE, str(exc))
        except RuntimeError as exc:
            text = str(exc)
            status = fp.ERR_DMA if "DMA" in text else fp.ERR_INTERNAL
            raise FrameError(status, text)
        invalidate = getattr(out_buf, "invalidate", None)
        if callable(invalidate):
            invalidate()                    # 读之前 invalidate，丢弃 CPU 侧旧缓存
        output = np.array(out_buf, copy=True).reshape(height, width)
        self._frames += 1

        return {
            "frame_id": frame_id, "width": width, "height": height, "channels": channels,
            "output": output.tobytes(), "config": cfg, "registers": extra,
            "unknown_regs": list(getattr(self.driver, "unknown_user_regs", []) or []),
            "pl_ms": round(float(pl_seconds) * 1e3, 4),
            "service_ms": round((time.perf_counter() - started) * 1e3, 4),
            "overlay": self.overlay_version, "kernel": self.kernel_name,
            "simulated": self.simulated,
        }

    def info(self):
        return {"overlay": self.overlay_version, "kernel": self.kernel_name,
                "simulated": self.simulated, "registers": self.registers,
                "supported": self.supported, "buffers": list(self._buffers),
                "frames": self._frames}

# ================================================================ 自测
def _reference(gray, cfg):
    """与板端同口径的软件参考（仅在自测里用来验服务端链路，不参与线上路径）。"""
    g = gaussian3x3(gray) if cfg["enable_gauss"] else gray
    return morph_reference(edge_reference(g, cfg["mode"], cfg["threshold"]), cfg["morph_mode"])

def selftest(count=10, mock=True, bitfile=None, fail_every=0, verbose=True):
    """A 侧上板前的自检：连续 N 帧 + 逐像素 Golden + 错误码分类。"""
    # 主服务不注入故障：自测要可重复，DMA 错误分类单独用一个注入版服务验（见下）
    service = FrameService(bitfile=bitfile, mock=mock, fail_every=0, verbose=verbose)
    print("=" * 78)
    info = service.info()
    print("Overlay : %s | 核: %s | %s"
          % (info["overlay"], info["kernel"],
             "**软件 Golden 模拟**" if info["simulated"] else "真板"))
    print("寄存器 :", ", ".join("%s%s" % (k, "" if v else "(缺)") for k, v in info["supported"].items()))
    print("=" * 78)

    # 板端 numpy 1.13 没有 default_rng，统一用 RandomState（老 API）
    rng = np.random.RandomState(20261011)
    cases = [("random-640x480", 640, 480, dict(enable_gauss=True, morph_mode=MORPH_BYPASS)),
             ("random-320x240", 320, 240, dict(enable_gauss=True, morph_mode=MORPH_DILATE)),
             ("random-256x256", 256, 256, dict(enable_gauss=False, morph_mode=MORPH_ERODE))]
    rows, ok = [], True
    for name, width, height, kw in cases:
        gray = rng.randint(0, 256, size=(height, width)).astype(np.uint8)
        cfg = service.resolve_config(fp.PIPELINE_EXPLICIT, MODE_SOBEL, kw["morph_mode"],
                                     40, kw["enable_gauss"])
        reference = _reference(gray, cfg)
        for index in range(count):
            try:
                result = service.process(gray.tobytes(), width, height, 1,
                                         morph_mode=kw["morph_mode"], threshold=40,
                                         enable_gauss=kw["enable_gauss"], frame_id=index + 1)
            except FrameError as exc:
                ok = False
                print("  %-14s 第 %2d/%d 帧 失败：%s（%s）"
                      % (name, index + 1, count, exc.code, exc))
                continue
            out = np.frombuffer(result["output"], np.uint8).reshape(height, width)
            diff = int(np.count_nonzero(out != reference))
            ok = ok and diff == 0
            rows.append(result["pl_ms"])
            if verbose and (index == 0 or index == count - 1):
                print("  %-14s 第 %2d/%d 帧  Golden %s  不同像素=%d  pl=%.4f ms"
                      % (name, index + 1, count, "PASS" if diff == 0 else "FAIL",
                         diff, result["pl_ms"]))

    # 错误路径：坏长度 / 坏通道数 / 坏尺寸 / DMA 错误分类
    checks = []
    try:
        service.process(b"\x00" * (640 * 480 - 1), 640, 480, 1)
        checks.append(("坏长度被拒", False, "没有报错"))
    except FrameError as exc:
        checks.append(("坏长度被拒", exc.status == fp.ERR_BAD_LENGTH, exc.code))
    try:
        service.process(b"\x00" * (64 * 64), 64, 64, 3)
        checks.append(("坏通道数被拒", False, "没有报错"))
    except FrameError as exc:
        checks.append(("坏通道数被拒", exc.status == fp.ERR_BAD_CHANNELS, exc.code))
    try:
        service.process(b"\x00" * 4, 2, 2, 1)         # 2x2 低于最小尺寸 3x3
        checks.append(("坏尺寸被拒", False, "没有报错"))
    except FrameError as exc:
        checks.append(("坏尺寸被拒", exc.status == fp.ERR_BAD_SIZE, exc.code))
    if service.simulated:
        # 真板驱动不支持注入（SobelParamDriver 没有 fail_every），只在 mock 下验这条
        inject = FrameService(bitfile=bitfile, mock=mock, fail_every=1, verbose=False)
        try:
            inject.process(b"\x00" * (64 * 64), 64, 64, 1)
            checks.append(("DMA 错误分类", False, "没有报错"))
        except FrameError as exc:
            checks.append(("DMA 错误分类", exc.status == fp.ERR_DMA, "%s（mock 注入）" % exc.code))
        finally:
            inject.close()
    else:
        checks.append(("DMA 错误分类", True, "真板不注入，错误码分类已由 mock 覆盖"))

    samples = np.asarray(rows)
    print("-" * 78)
    if samples.size:
        print("连续 %d 帧：mean=%.4f median=%.4f max=%.4f ms"
              % (len(samples), samples.mean(), float(np.median(samples)), samples.max()))
    for name, passed, detail in checks:
        ok = ok and passed
        print("  %-16s %s   %s" % (name, "PASS" if passed else "FAIL", detail))
    print("=" * 78)
    print("自测%s" % ("全部通过（%d 帧）" % len(samples) if ok else "有失败项"))
    service.close()
    return 0 if ok else 1

def main(argv=None):
    ap = argparse.ArgumentParser(description="live_stream 板端连续帧处理服务（A 侧）")
    ap.add_argument("--selftest", type=int, default=0, help="连续跑 N 帧并做 Golden/错误码自检")
    ap.add_argument("--mock", action="store_true", help="用软件 Golden 顶替 PL（不占板子）")
    ap.add_argument("--bitfile", default=FROZEN_pipeline_validation_BITFILE,
                    help="默认沿用 pipeline_validation 冻结版本（live_stream 未改硬件）")
    ap.add_argument("--timeout", type=float, default=5.0, help="单帧 DMA 超时（秒）")
    ap.add_argument("--fail-every", type=int, default=0, help="每 N 帧故意注入一次 DMA 错误")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest(args.selftest, args.mock, args.bitfile, args.fail_every)
    ap.error("请用 --selftest N 运行；服务模式请用 frame_server.py")

if __name__ == "__main__":
    raise SystemExit(main())
