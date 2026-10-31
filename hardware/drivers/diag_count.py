# -*- coding: utf-8 -*-
"""计数诊断工具：同一张图，换参数跑，看哪一组能数对。

用途：队友说"计数不对"，但不知道是阈值、面积门槛还是 profile 造成的。
本脚本把这几组参数一次性跑完并列表，直接看出差异。

用法（在 board/overlay/ 目录下，与 agripheno.py 同目录）：
    python3 diag_count.py 图片1.png 图片2.jpg ...
或指定"应该是几个"，脚本会标出哪组参数答对：
    python3 diag_count.py --truth 5,8,6,10,14 图片1.png ...

注意：本脚本只跑软件路径（backend=PS），不需要板卡，不上 FPGA。
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
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agripheno  # noqa: E402

# 要对比的参数组合：(说明, 自动?, 阈值, 最小面积, profile)
COMBOS = [
    ("仪表盘默认(自动+种子)",   True,  40, 1000, "Seeds"),
    ("仪表盘默认(自动+绿叶)",   True,  40, 1000, "Green leaves"),
    ("关闭自动 thr=40 area=100 种子", False, 40, 100, "Seeds"),
    ("关闭自动 thr=40 area=100 绿叶", False, 40, 100, "Green leaves"),
    ("关闭自动 thr=40 area=1000 种子", False, 40, 1000, "Seeds"),
    ("关闭自动 thr=80 area=100 种子", False, 80, 100, "Seeds"),
]

def load64(path):
    im = Image.open(path).convert("RGB")
    return np.asarray(im.resize((640, 480), Image.BILINEAR), np.uint8)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--truth", default=None, help="正确的计数，逗号分隔，与图片一一对应")
    args = ap.parse_args()

    truth = None
    if args.truth:
        truth = [int(v) for v in args.truth.split(",")]
        if len(truth) != len(args.images):
            print("--truth 的个数(%d)与图片数(%d)不一致" % (len(truth), len(args.images)))
            return 1

    engine = agripheno.AgriEngine()
    print("按 640x480 缩放后分析（与仪表盘一致）；只跑软件路径，不占板卡\n")

    for idx, name in enumerate(args.images):
        path = Path(name)
        if not path.is_file():
            print("找不到文件:", path)
            continue
        src = load64(path)
        want = truth[idx] if truth else None
        print("=" * 84)
        print("图片: %s%s" % (path.name, ("   正确计数 = %d" % want) if want else ""))
        print("  %-30s %-9s %-24s %s" % ("参数组合", "Count", "自动选出的参数", "判断"))
        print("  " + "-" * 80)
        for label, auto, thr, area, prof in COMBOS:
            try:
                r = engine.analyze(src, (640, 480), "Sobel", "PS",
                                   "Plant Phenotyping", automatic=auto,
                                   threshold=thr, min_area=area, profile=prof)
                ph = r["phenotype_result"]
                cnt = ph["count"] if ph else -1
                par = r["parameters"]
                used = "thr=%s area=%s" % (par.get("threshold"),
                                           round(par.get("min_area", 0), 1))
                mark = "" if want is None else ("✅ 对" if cnt == want else "❌ 差 %+d" % (cnt - want))
            except Exception as exc:
                cnt, used, mark = -1, "-", "异常 %s" % type(exc).__name__
            print("  %-30s %-9s %-24s %s" % (label, cnt, used, mark))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
