# -*- coding: utf-8 -*-
"""
PS 软件 Sobel 验证工具(cv2 基线 + 硬件拟真 Reference)

用法:
  python sobel_baseline.py <图片路径...>
  传入一张或多张 256x256 8bit 灰度图(.png/.bin),文件夹则处理其中全部图片。
  例: python sobel_baseline.py data/fixtures/images/plane_gray_256x256.bin

规格(edge_validation 冻结接口):只接受 256x256 8bit 灰度图,本工具不做任何尺寸/灰度转换,
不符合规格的输入直接报错提示。测试图一律作为外部数据经参数传入,代码中不预写任何图名。

对每张图执行:
  1) cv2 标准版 Sobel(视觉对照)
  2) 硬件拟真版:3x3 滑窗卷积,仅内部像素,G = |Gx|+|Gy| 钳位 0~255,
     与 HLS 核逐像素一致
  3) 自检:两版内部像素逐像素一致
  4) golden 对拍:若 data/golden/{图名}_edge_hw.bin 存在则逐字节比对
     (golden 是外部测试数据,不存在就跳过)
  5) 输出三面板对比图到 data/output/{图名}_compare.png
     (Input | cv2 | 硬件拟真版补边),不提交
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
import time
from pathlib import Path

import numpy as np
import cv2

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SIZE = 256
REPO = PROJECT_ROOT
GOLDEN_DIR = REPO / "data/fixtures/golden"
OUT_DIR = REPO / "data/fixtures/output"# 运行时产物(不提交,见 .gitignore)

# Sobel 3x3 卷积核(与 HLS 核中的常量一致)
KX = np.array([[-1, 0, 1],
               [-2, 0, 2],
               [-1, 0, 1]], np.int32)          # 横向梯度 Gx
KY = np.array([[-1, -2, -1],
               [0, 0, 0],
               [1, 2, 1]], np.int32)           # 纵向梯度 Gy

def sobel_hw_mimic(gray: np.ndarray):
    """硬件拟真版 Sobel:3x3 滑窗卷积,逐像素等价于 HLS 核的双层循环。

    返回 (gx, gy, mag):
      gx/gy : int32, 内部像素梯度,形状 (H-2, W-2),范围 [-1020, 1020]
      mag   : uint8, |Gx|+|Gy| 钳位到 0~255,形状 (H-2, W-2)
    """
    win = np.lib.stride_tricks.sliding_window_view(gray, (3, 3)).astype(np.int32)
    gx = np.einsum("ijkl,kl->ij", win, KX)
    gy = np.einsum("ijkl,kl->ij", win, KY)
    mag = np.clip(np.abs(gx) + np.abs(gy), 0, 255).astype(np.uint8)
    return gx, gy, mag

def load_gray(path: Path) -> np.ndarray:
    """读取一张测试图(.bin 原样读 / 其他格式 cv2 解码为灰度),校验规格。

    只接受 256x256 8bit 灰度;不做任何转换,不符直接报错。
    """
    path = Path(path)
    if path.suffix.lower() == ".bin":
        raw = path.read_bytes()
        if len(raw) != SIZE * SIZE:
            raise SystemExit(
                f"[FAIL] {path.name}: bin 大小 {len(raw)} B,"
                f" 规格应为 {SIZE * SIZE} B({SIZE}x{SIZE} 8bit 灰度)")
        img = np.frombuffer(raw, np.uint8).reshape(SIZE, SIZE)
    else:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise SystemExit(f"[FAIL] 无法读取图片: {path}")
    if img.shape != (SIZE, SIZE) or img.dtype != np.uint8:
        raise SystemExit(
            f"[FAIL] {path.name}: 规格 {img.shape}/{img.dtype} 不符,"
            f" 只接受 {SIZE}x{SIZE} 8bit 灰度图,请先转换尺寸/灰度后再传入")
    return img

def process_image(path: str) -> None:
    """对任意一张 256x256 灰度图跑完整软件验证链。"""
    path = Path(path)
    stem = path.stem
    img = load_gray(path)

    # ---- cv2 标准版(视觉对照) ----
    t0 = time.perf_counter()
    gx_cv = cv2.Sobel(img, cv2.CV_16S, 1, 0, ksize=3)
    gy_cv = cv2.Sobel(img, cv2.CV_16S, 0, 1, ksize=3)
    edge_cv2 = cv2.addWeighted(cv2.convertScaleAbs(gx_cv), 0.5,
                               cv2.convertScaleAbs(gy_cv), 0.5, 0)
    t1 = time.perf_counter()

    # ---- 硬件拟真版(严格 Reference) ----
    gx, gy, edge_hw = sobel_hw_mimic(img)
    t2 = time.perf_counter()

    # ---- 自检:滑窗结果与 cv2 内部像素逐像素一致 ----
    gx_ref = gx_cv.astype(np.int32)[1:-1, 1:-1]
    gy_ref = gy_cv.astype(np.int32)[1:-1, 1:-1]
    assert np.array_equal(gx, gx_ref), f"{stem}: Gx mismatch vs cv2"
    assert np.array_equal(gy, gy_ref), f"{stem}: Gy mismatch vs cv2"

    # ---- golden 对拍:golden 是外部测试数据,存在同名文件才比对 ----
    golden_path = GOLDEN_DIR / f"{stem}_edge_hw.bin"
    if golden_path.exists():
        golden = np.frombuffer(golden_path.read_bytes(),
                               np.uint8).reshape(edge_hw.shape)
        assert np.array_equal(edge_hw, golden), \
            f"{stem}: mismatch vs golden {golden_path.name}"
        verdict = "PASS(golden 一致)"
    else:
        verdict = "PASS(无 golden,跳过对拍)"

    # ---- 运行时输出(不提交) ----
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(OUT_DIR / f"{stem}_edge_cv2.png"), edge_cv2)
    cv2.imwrite(str(OUT_DIR / f"{stem}_edge_hw.png"), edge_hw)
    (OUT_DIR / f"{stem}_edge_hw.bin").write_bytes(edge_hw.tobytes())
    edge_hw_pad = cv2.copyMakeBorder(edge_hw, 1, 1, 1, 1,
                                     cv2.BORDER_CONSTANT, value=0)
    cmp_img = cv2.hconcat([cv2.cvtColor(img, cv2.COLOR_GRAY2BGR),
                           cv2.cvtColor(edge_cv2, cv2.COLOR_GRAY2BGR),
                           cv2.cvtColor(edge_hw_pad, cv2.COLOR_GRAY2BGR)])
    cv2.imwrite(str(OUT_DIR / f"{stem}_compare.png"), cmp_img)

    print(f"  [{stem}] {verdict}  (cv2 对照: {(t1 - t0) * 1e3:7.2f} ms | "
          f"硬件拟真: {(t2 - t1) * 1e3:7.2f} ms, 仅作参考)")

def collect_paths(args: list) -> list:
    """展开命令行参数:文件直接收,文件夹收其中全部 .png/.bin。"""
    if not args:
        raise SystemExit(
            "用法: python <脚本名> <图片路径...>\n"
            "  传入一张或多张 256x256 8bit 灰度图(.png/.bin),"
            " 文件夹则处理其中全部图片。")
    paths = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            paths += sorted(list(p.glob("*.png")) + list(p.glob("*.bin")))
        elif p.is_file():
            paths.append(p)
        else:
            raise SystemExit(f"[FAIL] 路径不存在: {p}")
    if not paths:
        raise SystemExit("[FAIL] 参数中没有找到图片")
    return paths

def main(argv=None) -> None:
    for p in collect_paths(sys.argv[1:] if argv is None else argv):
        print(f"== {p} ==")
        process_image(p)
    print("ALL PASS: 以上图片均通过软件验证链")

if __name__ == "__main__":
    main()
