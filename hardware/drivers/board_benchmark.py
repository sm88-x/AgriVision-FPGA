#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一板端 Benchmark：PS 软件 / PL V0 / PL V3 / PL V3 参数化，同口径对比

为什么要有这个脚本：V0、V3 与 V3 参数化版的等待方式曾经不一致
（早期用 PYNQ 自带 wait()，参数化版用 DMA 完成标志轮询），
跨批次数字不能直接比较。本脚本用同一套计时方法测所有后端，
保证 PS/PL、V0/V3、多尺寸、Sobel/Prewitt 四组对比口径统一。

四个后端：
    ps   板上 PS 软件（numpy 参考实现，只计算法本身）
    v0   live_stream 流式核：无寄存器、固定 256x256、ap_ctrl_none（自由流）
    v3   detection 流式核：同上，但内部已流水线化
    v3p  phenotyping 参数化核：s_axilite 配置 width/height/mode/threshold

统一口径：
    PL E2E = 挂 DMA + 收发一帧 + 等完成，全程计时
    完成判定 = 读 DMA 状态寄存器的 IOC 位（不用 channel.running，
               实测该属性在本板不反映真实状态）
    PS     = 只计软件算法本身，读图与内存在计时外
    FPS    = 1000 / 中位数

用法（在板的 Jupyter 里）：
    %run board_benchmark.py --image plant_a_256x256.bin --repeats 20
    %run board_benchmark.py --backends ps,v0,v3,v3p --image plant_a_256x256.bin
    %run board_benchmark.py --backends v3p --image plant_a_640x480.bin --size 640x480

板端需要放好的文件（与脚本同目录）：
    parameterized_edge.bit / .hwh          V3 参数化核（phenotyping）
    sobel_axis_wrapper.bit / sobel_axis.hwh          V0 内核（live_stream）
    sobel_v3_wrapper.bit / sobel_v3_wrapper.hwh      V3 内核（detection，上传时改成这个名字）
    plant_a_256x256.bin 等测试图
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

try:
    from pynq import Overlay, MMIO, allocate
    HAVE_PYNQ = True
except ImportError:            # 在 PC 上只能跑 PS 后端
    Overlay = MMIO = allocate = None
    HAVE_PYNQ = False

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# =============================================================
# 硬件配置：只写文件名和实例名，地址一律从 hwh 动态读取
# =============================================================
BACKENDS = {
    "ps":  dict(kind="software", label="PS 软件"),
    "v0":  dict(kind="stream", label="PL V0 流式",
                bit="sobel_axis_wrapper.bit", kernel="sobel_axis_1"),
    "v3":  dict(kind="stream", label="PL V3 流式",
                bit="sobel_v3_wrapper.bit", kernel="sobel_v3_0"),
    "v3p": dict(kind="axilite", label="PL V3 参数化",
                bit="parameterized_edge.bit", kernel="sobel_v3_0"),
}
DMA_NAME = "axi_dma_0"

# s_axilite 寄存器偏移（A 侧接口说明）
OFF_AP_CTRL, OFF_WIDTH, OFF_HEIGHT, OFF_MODE, OFF_THRESHOLD = 0x00, 0x10, 0x18, 0x20, 0x28
MODE_ID = {"sobel": 0, "prewitt": 1}

# DMA 状态寄存器
MM2S_DMASR, S2MM_DMASR = 0x04, 0x34
DMASR_IOC = 0x1000          # bit12 传输完成（写 1 清除）
POLL_INTERVAL = 0.0001      # 0.1 ms 轮询，减小计时量化误差

# 流式核（V0/V3）固定 256x256；参数化核支持任意尺寸
STREAM_SIZE = (256, 256)

# =============================================================
# PS 参考实现（与 tests/simulation/edge_benchmark.py 同源）
# =============================================================
KERNELS = {
    "sobel": (np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], np.int32),
              np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], np.int32)),
    "prewitt": (np.array([[-1, 0, 1], [-1, 0, 1], [-1, 0, 1]], np.int32),
                np.array([[-1, -1, -1], [0, 0, 0], [1, 1, 1]], np.int32)),
}

def edge_reference(gray, mode="sobel", threshold=0):
    """PS 软件参考实现。

    卷积只展开非零项（6 项而非 9 项）——两种核的中间行/列本来就是 0，
    写成 9 项会白白多做三次乘以 0 的运算，让 PS 基线虚高。
    结果与 9 项写法完全一致。
    """
    g = np.asarray(gray).astype(np.int32)
    if mode == "sobel":
        gx = (-g[:-2, :-2] + g[:-2, 2:]
              - 2 * g[1:-1, :-2] + 2 * g[1:-1, 2:]
              - g[2:, :-2] + g[2:, 2:])
        gy = (-g[:-2, :-2] - 2 * g[:-2, 1:-1] - g[:-2, 2:]
              + g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:])
    elif mode == "prewitt":
        gx = (-g[:-2, :-2] + g[:-2, 2:]
              - g[1:-1, :-2] + g[1:-1, 2:]
              - g[2:, :-2] + g[2:, 2:])
        gy = (-g[:-2, :-2] - g[:-2, 1:-1] - g[:-2, 2:]
              + g[2:, :-2] + g[2:, 1:-1] + g[2:, 2:])
    else:
        raise ValueError(f"未知算法 {mode!r}")

    mag = np.abs(gx) + np.abs(gy)
    if threshold > 0:
        mag = np.where(mag >= threshold, 255, 0)
    out = np.zeros(g.shape, np.uint8)
    out[1:-1, 1:-1] = np.clip(mag, 0, 255).astype(np.uint8)
    return out

def compare(reference, result):
    diff = np.abs(result.astype(np.int16) - reference.astype(np.int16))
    different = int(np.count_nonzero(diff))
    return {"different_pixels": different, "max_error": int(diff.max()),
            "verdict": "PASS" if different == 0 else "FAIL"}

def load_gray(path, size=None):
    path = Path(path)
    raw = path.read_bytes()
    if size is None:
        side = int(round(len(raw) ** 0.5))
        if side * side != len(raw):
            raise ValueError(f"{path.name}: 非方形裸数据，请用 --size 指定尺寸")
        return np.frombuffer(raw, np.uint8).reshape(side, side)
    width, height = size
    if len(raw) != width * height:
        raise ValueError(f"{path.name}: 期望 {width*height} B，实际 {len(raw)} B")
    return np.frombuffer(raw, np.uint8).reshape(height, width)

# =============================================================
# PL 后端
# =============================================================
class PlRunner:
    """加载一个 overlay，并用统一方式跑帧。"""

    def __init__(self, spec):
        if not HAVE_PYNQ:
            raise RuntimeError("PL 后端只能在 PYNQ 板上运行")
        bit = Path(spec["bit"])
        if not bit.exists():
            raise FileNotFoundError(f"找不到 {bit.name}（请确认已上传到脚本同目录）")
        self.ol = Overlay(str(bit))
        self.kernel = spec["kernel"]
        self.kind = spec["kind"]
        if DMA_NAME not in self.ol.ip_dict:
            raise RuntimeError(f"{bit.name} 里没有 {DMA_NAME}")
        self.dma = getattr(self.ol, DMA_NAME)
        self.dma_ctrl = MMIO(self.ol.ip_dict[DMA_NAME]["phys_addr"], 0x1000)
        if self.kind == "axilite":
            # 参数化核有 s_axilite，PYNQ 会把它登记进 ip_dict，可以直接取地址
            if self.kernel not in self.ol.ip_dict:
                raise RuntimeError(f"{bit.name} 里没有 {self.kernel}；实际有 {sorted(self.ol.ip_dict)}")
            self.ctrl = MMIO(self.ol.ip_dict[self.kernel]["phys_addr"], 0x1000)
            kernel_base = hex(self.ol.ip_dict[self.kernel]["phys_addr"])
        else:
            # 流式核是 ap_ctrl_none，没有寄存器接口，PYNQ 不会登记它——
            # 也不需要：数据全靠 DMA 走，脚本不碰核。
            self.ctrl = None
            kernel_base = "无寄存器 (ap_ctrl_none)"
        self.info = {
            "bitfile": bit.name,
            "kernel": self.kernel,
            "kernel_base": kernel_base,
            "dma_base": hex(self.ol.ip_dict[DMA_NAME]["phys_addr"]),
        }

    def _wait(self, t0, timeout):
        while True:
            m = self.dma_ctrl.read(MM2S_DMASR)
            s = self.dma_ctrl.read(S2MM_DMASR)
            if (m & DMASR_IOC) and (s & DMASR_IOC):
                return
            if time.perf_counter() - t0 > timeout:
                raise TimeoutError(f"DMA 超时 {timeout}s：MM2S={hex(m)} S2MM={hex(s)}")
            time.sleep(POLL_INTERVAL)

    def run(self, inp, out, width, height, mode, threshold, timeout):
        t0 = time.perf_counter()
        # 清完成标志，避免误判
        self.dma_ctrl.write(MM2S_DMASR, DMASR_IOC)
        self.dma_ctrl.write(S2MM_DMASR, DMASR_IOC)

        if self.kind == "axilite":
            self.ctrl.write(OFF_WIDTH, width)
            self.ctrl.write(OFF_HEIGHT, height)
            self.ctrl.write(OFF_MODE, MODE_ID[mode])
            self.ctrl.write(OFF_THRESHOLD, threshold)

        # 先挂两个 DMA 通道
        self.dma.recvchannel.transfer(out)
        self.dma.sendchannel.transfer(inp)

        # 参数化核需要 ap_start；流式核（ap_ctrl_none）自由运行，不需要
        if self.kind == "axilite":
            self.ctrl.write(OFF_AP_CTRL, 1)

        self._wait(t0, timeout)
        return time.perf_counter() - t0

# =============================================================
# 主流程
# =============================================================
def measure_ps(gray, mode, threshold, warmup, repeats):
    for _ in range(warmup):
        edge_reference(gray, mode, threshold)
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        edge_reference(gray, mode, threshold)
        samples.append((time.perf_counter() - t0) * 1e3)
    return samples, edge_reference(gray, mode, threshold)

def measure_pl(runner, gray, mode, threshold, warmup, repeats, timeout):
    height, width = gray.shape
    inp = allocate(shape=(width * height,), dtype=np.uint8)
    out = allocate(shape=(width * height,), dtype=np.uint8)
    inp[:] = gray.reshape(-1)

    runner.run(inp, out, width, height, mode, threshold, timeout)   # 第一帧用于正确性
    result = np.array(out).reshape(height, width)

    for _ in range(warmup):
        runner.run(inp, out, width, height, mode, threshold, timeout)
    samples = []
    for _ in range(repeats):
        samples.append(runner.run(inp, out, width, height, mode, threshold, timeout) * 1e3)
    del inp, out
    return samples, result

def summarize(samples):
    ms = np.asarray(samples, np.float64)
    med = float(np.median(ms))
    return {"runs": int(ms.size), "mean_ms": float(ms.mean()), "median_ms": med,
            "min_ms": float(ms.min()), "max_ms": float(ms.max()),
            "stdev_ms": float(ms.std(ddof=1)) if ms.size > 1 else 0.0,
            "fps_median": (1000.0 / med) if med > 0 else float("inf")}

def parse_size(text):
    if not text:
        return None
    w, h = text.lower().split("x")
    return int(w), int(h)

def main(argv=None):
    ap = argparse.ArgumentParser(description="统一板端 Benchmark")
    ap.add_argument("--image", required=True, help="灰度 .bin 测试图")
    ap.add_argument("--size", default=None, help="例如 256x256；不给则按方形推断")
    ap.add_argument("--mode", default="sobel", choices=["sobel", "prewitt"])
    ap.add_argument("--threshold", type=int, default=0)
    ap.add_argument("--backends", default="ps,v0,v3,v3p",
                    help="逗号分隔，可选 ps / v0 / v3 / v3p")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--repeats", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--out", default="data/evidence/analysis/board_benchmark.csv")
    args = ap.parse_args(argv)

    size = parse_size(args.size)
    gray = load_gray(args.image, size)
    height, width = gray.shape
    resolution = f"{width}x{height}"

    print(f"输入图 {Path(args.image).name}  {resolution}  算法 {args.mode}  "
          f"阈值 {args.threshold}  warm-up {args.warmup}  正式 {args.repeats} 次")

    names = [n.strip() for n in args.backends.split(",") if n.strip()]
    rows, runners = [], {}

    for name in names:
        if name not in BACKENDS:
            print(f"[跳过] 未知后端 {name}")
            continue
        spec = BACKENDS[name]
        label = spec["label"]

        if spec["kind"] == "stream" and (width, height) != STREAM_SIZE:
            print(f"[跳过] {label}：流式核固定 {STREAM_SIZE[0]}x{STREAM_SIZE[1]}，"
                  f"当前输入是 {resolution}")
            continue

        # V0 / V3 只有 Sobel 通路，没有 Prewitt；跑了只会拿 Sobel 结果去比
        # Prewitt 参考值，必然 FAIL，所以直接跳过并说明原因
        if spec["kind"] == "stream" and args.mode != "sobel":
            print(f"[跳过] {label}：V0/V3 流式核只有 Sobel 通路，不支持 {args.mode}")
            continue

        print(f"\n===== {name}  {label} =====")
        try:
            reference = edge_reference(gray, args.mode, args.threshold)
            if spec["kind"] == "software":
                samples, result = measure_ps(gray, args.mode, args.threshold,
                                             args.warmup, args.repeats)
                base = {"bitfile": "-", "kernel": "-"}
            else:
                if name not in runners:
                    runners[name] = PlRunner(spec)
                runner = runners[name]
                base = runner.info
                print(f"    {base['bitfile']}  核 {base['kernel']} @ {base['kernel_base']}"
                      f"  DMA @ {base['dma_base']}")
                samples, result = measure_pl(runner, gray, args.mode, args.threshold,
                                             args.warmup, args.repeats, args.timeout)
            stats = summarize(samples)
            ver = compare(reference, result)
            print(f"    正确性 {ver['verdict']}  不同像素={ver['different_pixels']}"
                  f"  最大误差={ver['max_error']}")
            print(f"    延迟 mean={stats['mean_ms']:.3f}  median={stats['median_ms']:.3f}"
                  f"  min={stats['min_ms']:.3f}  max={stats['max_ms']:.3f} ms"
                  f"  | FPS={stats['fps_median']:.1f}")

            for i, ms in enumerate(samples, 1):
                rows.append({
                    "backend": name, "label": label, "image": Path(args.image).stem,
                    "resolution": resolution, "algorithm": args.mode,
                    "threshold": args.threshold, "run_id": i,
                    "ms": round(float(ms), 4), "fps": round(1000.0 / ms, 2),
                    "correct": ver["verdict"], "different_pixels": ver["different_pixels"],
                    "source": base["bitfile"], "kernel": base["kernel"],
                })
        except Exception as exc:
            print(f"    [失败] {type(exc).__name__}: {exc}")
            rows.append({
                "backend": name, "label": label, "image": Path(args.image).stem,
                "resolution": resolution, "algorithm": args.mode,
                "threshold": args.threshold, "run_id": 0, "ms": "", "fps": "",
                "correct": f"ERROR:{type(exc).__name__}", "different_pixels": "",
                "source": BACKENDS[name].get("bit", "-"),
                "kernel": BACKENDS[name].get("kernel", "-"),
            })

    fields = ["backend", "label", "image", "resolution", "algorithm", "threshold",
              "run_id", "ms", "fps", "correct", "different_pixels", "source", "kernel"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\n逐次数据 → {out}（{len(rows)} 行）")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
