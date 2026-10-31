#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""benchmark —— Generic timing benchmark for any callable.

适用范围
--------
对任意可调用处理函数做统一性能统计,不绑定任何算法。
计时范围 = fn 实际执行的代码,由调用者定义:
  - 只想测 PS 算法: fn 里只放算法;
  - 想测 PL E2E: fn 里放寄存器写 + DMA 收发 + wait 全流程;
不要把 PS 算法时间和 PL E2E 混在同一个 fn 里比较 —— 口径不同,混测无意义。

输入
----
fn       : 被测函数(配合 args/kwargs 调用)
args     : 位置参数 tuple,每次迭代原样传入 fn
kwargs   : 关键字参数字典,每次迭代原样传入 fn
warmup   : 预热次数(不计入统计),默认 2
repeat   : 正式测量次数,默认 20
name     : 名称/标签,写入输出与 CSV
csv_path : CSV 路径;None 则不写文件(追加模式,列见 README)

输出
----
dict: mean/median/min/max(ms)、fps、samples(原始样本)、name、csv_path。
stdout 打印一行式报告;CSV 追加一行。

FPS 口径: FPS = 1000 / median(ms),与本项目板测 CSV(r1~r6)口径一致。
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import csv
import os
import statistics
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows GBK 控制台防乱码
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

CSV_HEADER = ["name", "mean_ms", "median_ms", "min_ms", "max_ms",
              "fps", "repeat", "warmup", "timestamp"]

def benchmark(fn, args=(), kwargs=None, warmup=2, repeat=20,
              name="unnamed", csv_path=None):
    """对 fn 做统一性能统计。详见模块 docstring 与 README。

    返回 dict:
      name      名称/标签
      mean_ms   平均耗时
      median_ms 中位耗时(FPS 口径以此为准)
      min_ms    最小耗时
      max_ms    最大耗时
      stdev_ms  样本标准差(repeat 为 1 时是 0)
      fps       每秒次数 = 1000 / median_ms
      samples   全部正式样本(ms,完整精度)
      repeat    实际样本数
      warmup    预热次数
      csv_path  CSV 路径(未写文件时为 None)
    """
    if not callable(fn):
        raise ValueError("benchmark: fn 必须是可调用对象")
    kwargs = kwargs or {}
    warmup = max(0, int(warmup))
    repeat = max(1, int(repeat))

    # 预热(不计入统计)
    for _ in range(warmup):
        fn(*args, **kwargs)

    # 正式测量
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn(*args, **kwargs)
        samples.append((time.perf_counter() - t0) * 1000.0)

    mean = statistics.mean(samples)
    median = statistics.median(samples)
    mn = min(samples)
    mx = max(samples)
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    fps = 1000.0 / median if median > 0 else float("inf")

    result = {
        "name": name,
        "mean_ms": mean,
        "median_ms": median,
        "min_ms": mn,
        "max_ms": mx,
        "stdev_ms": stdev,
        "fps": fps,
        "samples": samples,
        "repeat": len(samples),
        "warmup": warmup,
        "csv_path": csv_path,
    }

    if csv_path:
        _append_csv(csv_path, result)

    print("[benchmark] %s: mean=%.3f ms, median=%.3f ms, "
          "min=%.3f ms, max=%.3f ms, fps=%.3f (N=%d, warmup=%d)"
          % (name, mean, median, mn, mx, fps, len(samples), warmup))
    return result

def _append_csv(csv_path, r):
    """追加一行;文件不存在时先写表头。"""
    new = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(CSV_HEADER)
        w.writerow([r["name"],
                    "%.3f" % r["mean_ms"],
                    "%.3f" % r["median_ms"],
                    "%.3f" % r["min_ms"],
                    "%.3f" % r["max_ms"],
                    "%.3f" % r["fps"],
                    r["repeat"],
                    r["warmup"],
                    time.strftime("%Y-%m-%d %H:%M:%S")])

if __name__ == "__main__":
    # 无需 PYNQ 的最小自检:测一个 Python 函数
    def _demo():
        time.sleep(0.001)

    benchmark(_demo, warmup=1, repeat=5, name="demo_sleep_1ms")
    print("benchmark 可在板端/PC 通用, 详见 skill/benchmark/README.md。")
