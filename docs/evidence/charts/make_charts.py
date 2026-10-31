# -*- coding: utf-8 -*-
"""pipeline_validation 性能证据链图表生成脚本

数据源：hardware/build/reports/summary.csv（由 hardware/build/reports/make_summary.py 从各次
        正式板测的原始 CSV 聚合而来）——**不手工抄写任何数字**。

输出（两张，可直接用于 PPT / 报告）：
    latency_fps.png   延迟与吞吐：PS / V0 / V3 / V3-param 对照 + 多尺寸
    speedup.png       加速比与单像素成本：四组核心对比的结论图

配色与 report/charts 保持一致（蓝 = 基线，橙 = 优化版），
沿用仓库既有调色板槽位，不引入新色。
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
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = PROJECT_ROOT
SUMMARY = REPO / "hardware/build/reports/summary.csv"
OUT_DIR = Path(__file__).resolve().parent

# ---- 调色板（与 docs/evidence/charts/hls_optimization.png 同一套槽位） ----
C_BASE = "#2a78d6"     # 基线 / 软件
C_PIPE = "#eb6834"     # 优化 / 硬件
C_MUTED = "#8a8a80"    # 次要柱
C_GOOD = "#3f8f4a"     # 提示性强调
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

# 中文字体：与 edge_validation 一致
font_manager.fontManager.addfont("C:/Windows/Fonts/msyh.ttc")
plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.edgecolor"] = AXIS
plt.rcParams["xtick.color"] = INK2
plt.rcParams["ytick.color"] = INK2

def load():
    """读 summary.csv，只取「统一口径」那一批（2026-10-05）。

    其他批次是用当时的脚本测的，口径不同，放进同一张图会误导。
    """
    rows = [r for r in csv.DictReader(SUMMARY.open(encoding="utf-8"))
            if r["batch"].startswith("2026-10-05")]
    out = {"rows": rows, "v3p": {}, "v0": [], "v3": [], "ps": []}
    for r in rows:
        ver, res, algo = r["version"], r["resolution"], r["algorithm"]
        pl = float(r["pl_median_ms"]) if r["pl_median_ms"] else None
        ps = float(r["ps_median_ms"]) if r["ps_median_ms"] else None
        if ver == "v3_param" and pl is not None:
            out["v3p"][(res, algo)] = {"ms": pl, "fps": float(r["pl_fps_median"])}
        elif ver == "v0" and pl is not None:
            out["v0"].append(pl)
        elif ver == "v3" and pl is not None:
            out["v3"].append(pl)
        # PS 只取 256x256 Sobel，与 v0/v3 的口径对齐
        if ps is not None and (res, algo) == ("256x256", "sobel"):
            out["ps"].append(ps)
    return out

def style(ax):
    ax.set_facecolor(SURFACE)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, color=GRID, lw=0.8, zorder=0)
    ax.set_axisbelow(True)

def bars(ax, labels, values, colors, fmt="{:.3f}", unit=""):
    x = range(len(labels))
    rects = ax.bar(list(x), values, width=0.55, color=colors, zorder=2)
    top = max(values) if values else 1
    for rect, v in zip(rects, values):
        ax.text(rect.get_x() + rect.get_width() / 2, v + 0.02 * top,
                fmt.format(v) + unit, ha="center", va="bottom",
                fontsize=9.5, color=INK, fontweight="bold")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, color=INK, fontsize=10)
    ax.set_ylim(0, top * 1.18)
    return rects

def chart_latency_fps(d):
    v3p, v0, v3, ps = d["v3p"], d["v0"], d["v3"], d["ps"]
    mean = lambda xs: sum(xs) / len(xs)

    fig, axes = plt.subplots(2, 2, figsize=(12, 8.6))
    fig.patch.set_facecolor(SURFACE)
    for ax in axes.flat:
        style(ax)

    # (a) 四代实现的 256x256 延迟
    labels = ["PS 软件", "V0 流式", "V3 流式", "V3 参数化"]
    vals = [mean(ps), mean(v0), mean(v3), v3p[("256x256", "sobel")]["ms"]]
    bars(axes[0, 0], labels, vals,
         [C_MUTED, C_BASE, C_PIPE, C_GOOD], "{:.2f}", " ms")
    axes[0, 0].set_yscale("log")
    axes[0, 0].set_ylabel("延迟 (ms, 对数刻度)", fontsize=10, color=INK2)
    axes[0, 0].set_title("(a) 256×256 单帧延迟：软件 → 硬件 → 架构优化", color=INK, fontsize=12, loc="left")
    axes[0, 0].text(0.02, 0.90, f"PS {mean(ps):.1f} ms  →  V3 {mean(v3):.2f} ms",
                    transform=axes[0, 0].transAxes, fontsize=10.5, color=INK)

    # (b) 多尺寸延迟（V3 参数化核，双算法）
    all_sizes = ["256x256", "320x240", "640x480"]
    sizes = [s for s in all_sizes if (s, "sobel") in v3p]                 # 有 Sobel 的
    sizes_both = [s for s in sizes if (s, "prewitt") in v3p]              # 两种算法都有的
    x = range(len(sizes))
    w = 0.35
    s_ms = [v3p[(s, "sobel")]["ms"] for s in sizes]
    b1 = axes[0, 1].bar([i - w / 2 for i in x], s_ms, width=w, color=C_BASE, zorder=2, label="Sobel")
    b2 = axes[0, 1].bar([i + w / 2 for i in x if sizes[i] in sizes_both],
                        [v3p[(s, "prewitt")]["ms"] for s in sizes_both],
                        width=w, color=C_PIPE, zorder=2, label="Prewitt")
    p_ms = [v3p[(s, "prewitt")]["ms"] for s in sizes_both]
    for rects in (b1, b2):
        for rect in rects:
            axes[0, 1].text(rect.get_x() + rect.get_width() / 2, rect.get_height() + 0.05,
                            f"{rect.get_height():.2f}", ha="center", va="bottom",
                            fontsize=9.5, color=INK, fontweight="bold")
    axes[0, 1].set_xticks(list(x))
    axes[0, 1].set_xticklabels([s.replace("x", "×") for s in sizes], color=INK, fontsize=10)
    axes[0, 1].set_ylim(0, max(s_ms + p_ms) * 1.2)
    axes[0, 1].set_ylabel("PL E2E 延迟 (ms)", fontsize=10, color=INK2)
    axes[0, 1].set_title("(b) 三档尺寸 × 双算法延迟（V3 参数化核）", color=INK, fontsize=12, loc="left")
    axes[0, 1].legend(frameon=False, fontsize=10, loc="upper left")

    # (c) FPS
    bars(axes[1, 0], [s.replace("x", "×") for s in sizes],
         [v3p[(s, "sobel")]["fps"] for s in sizes],
         [C_BASE, C_BASE, C_BASE], "{:.0f}", " FPS")
    axes[1, 0].set_ylabel("FPS（Sobel，中位数）", fontsize=10, color=INK2)
    axes[1, 0].set_title("(c) 吞吐随尺寸变化", color=INK, fontsize=12, loc="left")
    axes[1, 0].text(0.02, 0.86, f"256×256 达 {v3p[('256x256', 'sobel')]['fps']:.0f} FPS",
                    transform=axes[1, 0].transAxes, fontsize=10.5, color=INK)

    # (d) 单像素成本
    ns = []
    for s in sizes:
        wpx, hpx = (int(v) for v in s.split("x"))
        ns.append(v3p[(s, "sobel")]["ms"] * 1e6 / (wpx * hpx))
    bars(axes[1, 1], [s.replace("x", "×") for s in sizes], ns,
         [C_MUTED, C_MUTED, C_GOOD], "{:.2f}", " ns")
    axes[1, 1].set_ylabel("单像素成本 (ns/像素)", fontsize=10, color=INK2)
    axes[1, 1].set_title("(d) 单像素成本：大尺寸更划算", color=INK, fontsize=12, loc="left")
    axes[1, 1].text(0.02, 0.86, "固定开销被摊薄", transform=axes[1, 1].transAxes,
                    fontsize=10.5, color=INK)

    fig.suptitle("AgriVision-FPGA 性能证据：延迟与吞吐（PYNQ-Z2，xc7z020clg400-1 @ 100 MHz）",
                 fontsize=14, color=INK, y=0.985)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = OUT_DIR / "latency_fps.png"
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    print(f"[OK] {out}")

def chart_speedup(d):
    v3p, v0, v3, ps = d["v3p"], d["v0"], d["v3"], d["ps"]
    mean = lambda xs: sum(xs) / len(xs)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.9))
    fig.patch.set_facecolor(SURFACE)
    for ax in axes.flat:
        style(ax)

    # (a) 加速比（相对 PS 软件基线）
    labels = ["V0\n(未流水)", "V3\n(流水线 II=1)", "V3 参数化\n(本代)"]
    vals = [mean(ps) / mean(v0), mean(ps) / mean(v3),
            mean(ps) / v3p[("256x256", "sobel")]["ms"]]
    bars(axes[0], labels, vals, [C_MUTED, C_PIPE, C_GOOD], "{:.1f}×")
    axes[0].set_ylabel("相对 PS 软件的加速比", fontsize=10, color=INK2)
    axes[0].set_title("(a) 256×256 加速比（PS 基线 = 1×）", color=INK, fontsize=12, loc="left")
    axes[0].text(0.03, 0.86, f"V3 相对 V0 提升 {mean(v0)/mean(v3):.1f}×",
                 transform=axes[0].transAxes, fontsize=10.5, color=INK, fontweight="bold")

    # (b) 双算法一致性
    all_sizes = ["256x256", "320x240", "640x480"]
    sizes = [s for s in all_sizes if (s, "sobel") in v3p]
    sizes_both = [s for s in sizes if (s, "prewitt") in v3p]
    x = range(len(sizes))
    w = 0.35
    s_fps = [v3p[(s, "sobel")]["fps"] for s in sizes]
    p_fps = [v3p[(s, "prewitt")]["fps"] for s in sizes_both]
    b1 = axes[1].bar([i - w / 2 for i in x], s_fps, width=w, color=C_BASE, zorder=2, label="Sobel")
    b2 = axes[1].bar([i + w / 2 for i in x if sizes[i] in sizes_both], p_fps,
                     width=w, color=C_PIPE, zorder=2, label="Prewitt")
    for rects in (b1, b2):
        for rect in rects:
            axes[1].text(rect.get_x() + rect.get_width() / 2, rect.get_height() + 8,
                         f"{rect.get_height():.0f}", ha="center", va="bottom",
                         fontsize=9.5, color=INK, fontweight="bold")
    axes[1].set_xticks(list(x))
    axes[1].set_xticklabels([s.replace("x", "×") for s in sizes], color=INK, fontsize=10)
    axes[1].set_ylim(0, max(s_fps + p_fps) * 1.2)
    axes[1].set_ylabel("FPS（中位数）", fontsize=10, color=INK2)
    axes[1].set_title("(b) 双算法吞吐几乎相同（共用一条数据通路）", color=INK, fontsize=12, loc="left")
    axes[1].legend(frameon=False, fontsize=10, loc="upper right")

    fig.suptitle("AgriVision-FPGA 性能证据：加速比与算法一致性", fontsize=14, color=INK, y=0.98)
    # 口径提示：本图所有数字来自同一次统一口径测量
    fig.text(0.012, 0.015,
             "数据来源：board/results/board_benchmark/（2026-10-05，四个后端同一输入、"
             "同一尺寸、同一完成判定方法，可横向比较）",
             fontsize=9, color=INK2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.90))
    out = OUT_DIR / "speedup.png"
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    print(f"[OK] {out}")

def main():
    d = load()
    print(f"[读入] {SUMMARY.name}：{len(d['rows'])} 组")
    chart_latency_fps(d)
    chart_speedup(d)

if __name__ == "__main__":
    main()
