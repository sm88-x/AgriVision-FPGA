# -*- coding: utf-8 -*-
"""性能数据接口：给 Dashboard 读正式 Benchmark 结果

Dashboard 的性能卡片必须走这里，不允许手写数字。

数据源只有一个：hardware/build/reports/summary.csv，
而 summary.csv 又由 hardware/build/reports/make_summary.py 从各次板测的原始 CSV 聚合而来。
整条链路是：原始逐次 CSV -> summary.csv -> 本模块 -> Dashboard 卡片，
任何一层都能往前追到板上的原始测量记录。

用法（板端 Notebook 或 PC 上都能跑）：

    from perf_data import get_card, available, format_line, card_to_html

    print(available())
    card = get_card("256x256", "sobel")
    print(format_line(card))
    from IPython.display import HTML
    HTML(card_to_html(card))

返回的卡片字段（与任务书要求的性能卡片一致）：
    ps_ms        PS 软件延迟（中位数）
    pl_ms        FPGA E2E 延迟（中位数）
    pl_fps       FPGA 帧率
    speedup      加速比 = ps_ms / pl_ms
    golden       PASS / 其他
    resolution / algorithm / backend / source  溯源信息
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
from pathlib import Path

DEFAULT_BATCH_PREFIX = "2026-10-05"
DEFAULT_BACKEND_VERSION = "v3_param"

def _find_summary(explicit=None):
    """定位 summary.csv：优先用显式路径，否则从本文件向上找。"""
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    here = Path(__file__).resolve()
    for base in [here.parent, *here.parents]:
        cand = base / "hardware/build/reports/summary.csv"
        if cand.exists():
            return cand
    raise FileNotFoundError(
        "找不到 hardware/build/reports/summary.csv；请把它和本模块放在同一个仓库里，"
        "或用 path 参数显式指定"
    )

def load_summary(path=None):
    """读入 summary.csv 的全部行（原样返回，不做筛选）。"""
    with _find_summary(path).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def _num(v):
    """空字符串转 None，其余转 float。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None

def available(path=None, batch_prefix=DEFAULT_BATCH_PREFIX):
    """列出有哪些 (resolution, algorithm, version) 组合可用。"""
    out = []
    for r in load_summary(path):
        if not r["batch"].startswith(batch_prefix):
            continue
        out.append((r["resolution"], r["algorithm"], r["version"]))
    return sorted(set(out))

def get_card(resolution, algorithm="sobel", version=DEFAULT_BACKEND_VERSION,
             path=None, batch_prefix=DEFAULT_BATCH_PREFIX):
    """取一组性能数据，返回 Dashboard 卡片用的 dict。

    resolution 形如 "256x256"；algorithm 是 sobel / prewitt。
    找不到时抛 KeyError，并把可用组合列出来，方便排查。
    """
    resolution = resolution.lower().replace("×", "x")
    algorithm = algorithm.lower()

    rows = [r for r in load_summary(path)
            if r["batch"].startswith(batch_prefix)
            and r["resolution"] == resolution
            and r["algorithm"] == algorithm]

    # summary.csv 里 PS 与 PL 是两行：PL 那行的 version 是后端名，PS 那行的 version 是 "ps"。
    # 这里把两行合起来，卡片才能同时给出 PS 延迟和加速比。
    pl_row = next((r for r in rows if r["version"] == version), None)
    ps_row = next((r for r in rows if r["version"] == "ps"), None)
    if pl_row is None:
        raise KeyError(
            "没有 %s / %s / %s 的正式数据；可用组合：%s"
            % (resolution, algorithm, version, available(path, batch_prefix))
        )

    pl_ms = _num(pl_row["pl_median_ms"])
    ps_ms = _num(ps_row["ps_median_ms"]) if ps_row else None
    speed = round(ps_ms / pl_ms, 2) if (ps_ms and pl_ms) else None

    return {
        "resolution": resolution,
        "algorithm": algorithm,
        "backend": version,
        "ps_ms": ps_ms,
        "pl_ms": pl_ms,
        "pl_fps": _num(pl_row["pl_fps_median"]),
        "speedup": speed,
        "golden": pl_row["correct"],
        "runs": int(pl_row["runs"]) if pl_row["runs"] else None,
        "batch": pl_row["batch"],
        "source": pl_row["source"],
    }

def _f(v, nd=3, unit=""):
    return "-" if v is None else ("%.*f%s" % (nd, v, unit))

def format_line(card):
    """一行文字摘要，适合打印到 Notebook 输出里。"""
    return ("[%s %s] PS %s -> FPGA %s ms (%s FPS)  加速比 %sx  Golden %s"
            % (card["resolution"], card["algorithm"],
               _f(card["ps_ms"], 2, " ms"),
               _f(card["pl_ms"], 3),
               _f(card["pl_fps"], 1),
               _f(card["speedup"], 2),
               card["golden"]))

def card_to_html(card, title="性能（正式板测数据）"):
    """渲染成一块 HTML 卡片，可直接被 ipywidgets / IPython.display 使用。"""
    items = [
        ("PS 延迟", _f(card["ps_ms"], 2, " ms")),
        ("FPGA E2E", _f(card["pl_ms"], 3, " ms")),
        ("FPS", _f(card["pl_fps"], 1)),
        ("加速比", _f(card["speedup"], 2, "x")),
        ("Golden", card["golden"]),
    ]
    cells = "".join(
        '<div style="flex:1;min-width:110px;padding:10px 14px;border-radius:8px;'
        'background:#f4f4f1;margin:4px">'
        '<div style="font-size:12px;color:#52514e">%s</div>'
        '<div style="font-size:20px;font-weight:600;color:#0b0b0b">%s</div></div>'
        % (k, v)
        for k, v in items
    )
    return (
        '<div style="font-family:Microsoft YaHei,sans-serif">'
        '<div style="font-size:13px;color:#52514e;margin-bottom:6px">'
        '%s - %s %s - %s 次</div>'
        '<div style="display:flex;flex-wrap:wrap">%s</div>'
        '<div style="font-size:11px;color:#8a8a80;margin-top:6px">'
        '来源：%s（%s）</div></div>'
        % (title, card["resolution"], card["algorithm"], card["runs"],
           cells, card["source"], card["batch"])
    )

if __name__ == "__main__":
    print("可用的性能数据组合：")
    for res, algo, ver in available():
        print("  %9s  %-8s %s" % (res, algo, ver))
    print()
    for res in ("256x256", "320x240", "640x480"):
        for algo in ("sobel", "prewitt"):
            try:
                print(format_line(get_card(res, algo)))
            except KeyError:
                pass
