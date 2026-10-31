# -*- coding: utf-8 -*-
"""
板级 Sobel Golden Reference(256x256 边界补 0)与 FPGA 结果自动比较

用法:
  python sobel_stream_ref.py <图片路径...>
  传入一张或多张 256x256 8bit 灰度图(.png/.bin),文件夹则处理其中全部图片。
  例: python sobel_stream_ref.py data/fixtures/images/plane_gray_256x256.bin

规格(edge_validation 冻结接口):输入 256x256 8bit 灰度,一帧 65536 像素;
  输出 256x256:内部 254x254 = 3x3 Sobel |Gx|+|Gy| 钳位 0~255,
  最外一圈边界统一输出 0(Python 与 HLS 规则必须一致)。
测试图一律作为外部数据经参数传入,代码中不预写任何图名。

对每张图执行:
  1) 生成板级 Reference(边界补 0 版)到 data/output/{图名}_edge_pad256.bin
  2) 自检:内部 254x254 与 data/golden/{图名}_edge_hw.bin 逐字节一致(存在时);
     与 data/golden/{图名}_edge_pad256.bin 逐字节一致(存在时);边界一圈全 0
  3) compare_fpga 自检:Reference 对自身输出 different_pixels/max_error/
     mean_error/PASS(docx 要求格式;FPGA 结果到货后同一函数直接对拍)
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import sys
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SIZE = 256
REPO = PROJECT_ROOT
GOLDEN_DIR = REPO / "data/fixtures/golden"
OUT_DIR = REPO / "data/fixtures/output"# 运行时产物(不提交,见 .gitignore)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sobel_baseline import (  # noqa: E402  复用同一套算法与读取逻辑
    sobel_hw_mimic, load_gray, collect_paths)

def sobel_reference_pad256(gray: np.ndarray) -> np.ndarray:
    """板级 Golden Reference:256x256,内部 Sobel、边界一圈 0(冻结接口)。"""
    assert gray.shape == (SIZE, SIZE) and gray.dtype == np.uint8
    _, _, mag = sobel_hw_mimic(gray)          # 254x254 内部像素
    ref = np.zeros((SIZE, SIZE), np.uint8)
    ref[1:-1, 1:-1] = mag
    return ref

def compare_fpga(reference: np.ndarray, fpga_result: np.ndarray) -> dict:
    """按 edge_validation.docx 原文公式自动比较,返回统计与结论。"""
    assert reference.shape == fpga_result.shape
    diff = np.abs(fpga_result.astype(np.int16) - reference.astype(np.int16))
    different_pixels = int(np.count_nonzero(diff))
    max_error = int(diff.max())
    mean_error = float(diff.mean())
    print("different_pixels =", different_pixels)
    print("max_error =", max_error)
    print("mean_error =", mean_error)
    print("PASS" if different_pixels == 0 else "CHECK")
    return {"different_pixels": different_pixels, "max_error": max_error,
            "mean_error": mean_error,
            "verdict": "PASS" if different_pixels == 0 else "CHECK"}

def process_image(path: str) -> None:
    path = Path(path)
    stem = path.stem
    img = load_gray(path)
    ref = sobel_reference_pad256(img)

    # 自检1:内部 254x254 与 HLS csim 用 golden 逐字节一致(golden 存在时)
    interior_golden = GOLDEN_DIR / f"{stem}_edge_hw.bin"
    if interior_golden.exists():
        interior = np.frombuffer(interior_golden.read_bytes(),
                                 np.uint8).reshape(SIZE - 2, SIZE - 2)
        assert np.array_equal(ref[1:-1, 1:-1], interior), \
            f"{stem}: interior mismatch vs {interior_golden.name}"

    # 自检2:边界一圈全 0
    assert not ref[0, :].any() and not ref[-1, :].any()
    assert not ref[:, 0].any() and not ref[:, -1].any()

    # 自检3:与仓库内 pad256 golden 逐字节一致(golden 存在时)
    pad_golden = GOLDEN_DIR / f"{stem}_edge_pad256.bin"
    if pad_golden.exists():
        pad = np.frombuffer(pad_golden.read_bytes(),
                            np.uint8).reshape(SIZE, SIZE)
        assert np.array_equal(ref, pad), f"{stem}: mismatch vs {pad_golden.name}"

    # 自检4:compare_fpga 对自身输出 PASS(演示 docx 要求的输出格式)
    stats = compare_fpga(ref, ref.copy())
    assert stats["verdict"] == "PASS"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{stem}_edge_pad256.bin").write_bytes(ref.tobytes())
    print(f"[{stem}] pad256 Reference 已生成({SIZE * SIZE} B),自检通过\n")

def main(argv=None) -> None:
    for p in collect_paths(sys.argv[1:] if argv is None else argv):
        print(f"== {p} ==")
        process_image(p)
    print("ALL PASS: 以上图片均通过板级 Reference 自检")

if __name__ == "__main__":
    main()
