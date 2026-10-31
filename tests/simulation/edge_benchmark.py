# -*- coding: utf-8 -*-
"""边缘检测软件基线：统一读图 + Sobel/Prewitt 参考 + 基准测试 + golden 生成

把软件侧的四件事合在一个文件里，风格与 tests/simulation/sobel_baseline.py 保持一致：

  1) 统一输入   任意格式/通道/尺寸 → uint8 灰度
                .bin 按裸数据直读；.png/.jpg 走 Pillow 或 OpenCV(装一个即可)
                RGB/RGBA 按 ITU-R BT.601 转灰度：0.299R + 0.587G + 0.114B
  2) 参考实现   Sobel / Prewitt 双算法，幅值 = |Gx| + |Gy|，最外一圈补 0
  3) 正确性     与 Golden 逐像素对拍，输出 PASS / FAIL
  4) 性能        warm-up + 重复 N 次，输出 Mean/Median/Min/Max/FPS 和逐次原始 CSV

与硬件核一致的口径（对应 src/hls/sobel_parameterized）：
    threshold == 0 → 饱和输出 min(幅值, 255)
    threshold  > 0 → 二值化：幅值 >= threshold 输出 255，否则输出 0
    输出恒为 8-bit，与图像尺寸无关；最外一圈为 0

常用命令（在仓库根目录执行）：
    # 1) 生成 golden：Sobel 只校验已提交的文件，Prewitt 与边界补 0 版新建
    python tests/simulation/edge_benchmark.py --make-golden \
        --images data/images --golden data/golden

    # 2) 正确性 + 性能（Sobel）
    python tests/simulation/edge_benchmark.py --images data/images --golden data/golden \
        --mode sobel --warmup 2 --repeats 20 \
        --out results/cpu_sobel.csv --summary results/cpu_sobel_summary.csv

    # 3) 换 Prewitt
    python tests/simulation/edge_benchmark.py --images data/images --mode prewitt \
        --repeats 20 --out results/cpu_prewitt.csv

计时口径：读图与分配内存在计时之外；每张图单独 warm-up；原始逐次数据全部保留。
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
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# =============================================================
# 卷积核：两种算法只差"中间权重"
#   Sobel   : Gx 的中间行权重 2，Gy 的中间列权重 2
#   Prewitt : 全部为 1
# 硬件核用 KW = (mode==0) ? 2 : 1 合并成一条通路，这里保持同样的写法。
# =============================================================
KERNELS = {
    "sobel": (
        np.array([[-1, 0, 1],
                  [-2, 0, 2],
                  [-1, 0, 1]], np.int32),
        np.array([[-1, -2, -1],
                  [0, 0, 0],
                  [1, 2, 1]], np.int32),
    ),
    "prewitt": (
        np.array([[-1, 0, 1],
                  [-1, 0, 1],
                  [-1, 0, 1]], np.int32),
        np.array([[-1, -1, -1],
                  [0, 0, 0],
                  [1, 1, 1]], np.int32),
    ),
}

MODE_ID = {"sobel": 0, "prewitt": 1}   # 与硬件 mode 寄存器取值一致

# =============================================================
# 1) 统一输入
# =============================================================
def to_gray(arr):
    """任意通道数的图像数组 → uint8 灰度。"""
    arr = np.asarray(arr)
    if arr.ndim == 2:
        return arr.astype(np.uint8, copy=False)
    if arr.ndim == 3:
        channels = arr.shape[2]
        if channels == 1:
            return arr[:, :, 0].astype(np.uint8, copy=False)
        if channels in (3, 4):
            r = arr[:, :, 0].astype(np.float32)
            g = arr[:, :, 1].astype(np.float32)
            b = arr[:, :, 2].astype(np.float32)
            return np.clip(np.rint(0.299 * r + 0.587 * g + 0.114 * b), 0, 255).astype(np.uint8)
        raise ValueError(f"不支持的通道数：{channels}")
    raise ValueError(f"不支持的数组维度：{arr.shape}")

def _decode_image(path):
    """图片格式解码：先试 Pillow，再试 OpenCV，都没有就给出明确提示。"""
    try:
        from PIL import Image
        return np.array(Image.open(str(path)))
    except ImportError:
        pass
    try:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"OpenCV 无法解码：{path}")
        if img.ndim == 3 and img.shape[2] >= 3:
            img = img[:, :, ::-1]          # BGR → RGB
        return img
    except ImportError as exc:
        raise ImportError("读取 PNG/JPG 需要 Pillow 或 OpenCV，例如 pip install Pillow") from exc

def load_gray(path, size=None):
    """读一张图并统一成 uint8 灰度；size=(width, height) 可选，给了就校验。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix.lower() == ".bin":
        raw = path.read_bytes()
        n = len(raw)
        if size is not None:
            width, height = size
            if n != width * height:
                raise ValueError(f"{path.name}: 期望 {width * height} B，实际 {n} B")
            return np.frombuffer(raw, np.uint8).reshape(height, width)
        side = int(round(n ** 0.5))
        if side * side != n:
            raise ValueError(f"{path.name}: 非方形裸数据（{n} B），必须用 size=(w, h) 指定")
        return np.frombuffer(raw, np.uint8).reshape(side, side)

    gray = to_gray(_decode_image(path))
    if size is not None and gray.shape != (size[1], size[0]):
        raise ValueError(
            f"{path.name}: 期望 {size[0]}x{size[1]}，实际 {gray.shape[1]}x{gray.shape[0]}"
        )
    return gray

# =============================================================
# 2) 参考实现
# =============================================================
def gradients(gray, mode="sobel"):
    """内部像素的 (gx, gy)，形状 (H-2, W-2)。"""
    if mode not in KERNELS:
        raise ValueError(f"未知算法 {mode!r}，可选 {sorted(KERNELS)}")
    g = np.asarray(gray).astype(np.int32)
    kx, ky = KERNELS[mode]

    def conv(k):
        return (k[0, 0] * g[:-2, :-2] + k[0, 1] * g[:-2, 1:-1] + k[0, 2] * g[:-2, 2:]
                + k[1, 0] * g[1:-1, :-2] + k[1, 1] * g[1:-1, 1:-1] + k[1, 2] * g[1:-1, 2:]
                + k[2, 0] * g[2:, :-2] + k[2, 1] * g[2:, 1:-1] + k[2, 2] * g[2:, 2:])

    return conv(kx), conv(ky)

def edge_reference(gray, mode="sobel", threshold=0):
    """软件 Golden Reference：与输入同尺寸，最外一圈 0。"""
    gray = np.asarray(gray)
    if gray.ndim != 2 or gray.shape[0] < 3 or gray.shape[1] < 3:
        raise ValueError(f"需要至少 3x3 的二维灰度图，实际 {gray.shape}")
    gx, gy = gradients(gray, mode)
    mag = np.abs(gx) + np.abs(gy)
    if threshold and threshold > 0:
        mag = np.where(mag >= threshold, 255, 0)     # 与硬件一致：二值化
    out = np.zeros(gray.shape, dtype=np.uint8)
    out[1:-1, 1:-1] = np.clip(mag, 0, 255).astype(np.uint8)
    return out

# =============================================================
# 3) 正确性判定
# =============================================================
def compare(reference, result):
    """逐像素对拍；different_pixels = 0 才算 PASS。"""
    reference = np.asarray(reference)
    result = np.asarray(result)
    if reference.shape != result.shape:
        raise ValueError(f"尺寸不一致：{reference.shape} vs {result.shape}")
    diff = np.abs(result.astype(np.int16) - reference.astype(np.int16))
    different = int(np.count_nonzero(diff))
    return {
        "different_pixels": different,
        "max_error": int(diff.max()),
        "mean_error": float(diff.mean()),
        "total_pixels": int(diff.size),
        "verdict": "PASS" if different == 0 else "FAIL",
    }

# =============================================================
# 4) 基准测试
# =============================================================
SAMPLE_FIELDS = ["mode", "image", "resolution", "threshold", "run_id",
                 "ms", "fps", "correct", "different_pixels"]
SUMMARY_FIELDS = ["mode", "image", "resolution", "threshold", "runs",
                  "mean_ms", "median_ms", "min_ms", "max_ms", "stdev_ms",
                  "fps_mean", "fps_median", "correct"]

def time_call(fn, warmup=2, repeats=20):
    """warm-up 若干次后正式计时，返回毫秒列表。"""
    for _ in range(max(0, warmup)):
        fn()
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e3)
    return samples

def summarize(samples):
    ms = np.asarray(samples, np.float64)
    mean = float(ms.mean())
    median = float(np.median(ms))
    return {
        "runs": int(ms.size),
        "mean_ms": mean, "median_ms": median,
        "min_ms": float(ms.min()), "max_ms": float(ms.max()),
        "stdev_ms": float(ms.std(ddof=1)) if ms.size > 1 else 0.0,
        "fps_mean": 1000.0 / mean if mean > 0 else float("inf"),
        "fps_median": 1000.0 / median if median > 0 else float("inf"),
    }

def write_csv(rows, path, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})
    return path

def summarize_rows(rows):
    """把逐次记录按 (mode, image, resolution, threshold) 合并成汇总行。"""
    buckets = {}
    for row in rows:
        key = (row["mode"], row["image"], row["resolution"], row["threshold"])
        buckets.setdefault(key, []).append(row)

    out = []
    for (mode, image, resolution, threshold), group in buckets.items():
        stats = summarize([r["ms"] for r in group])
        correct = "PASS" if all(r["correct"] == "PASS" for r in group) else "FAIL"
        out.append({
            "mode": mode, "image": image, "resolution": resolution,
            "threshold": threshold,
            **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in stats.items()},
            "correct": correct,
        })
    return out

# =============================================================
# 命令行
# =============================================================
def parse_size(text):
    if not text:
        return None
    width, height = text.lower().split("x")
    return int(width), int(height)

def collect_images(patterns):
    """收集图片；同名同时存在 .bin 与 .png 时优先 .bin（免解码、零误差）。"""
    seen = {}
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_dir():
            found = sorted(candidate.glob("*.bin")) + sorted(candidate.glob("*.png"))
        else:
            found = [candidate]
        for path in found:
            if path.suffix.lower() not in (".bin", ".png", ".jpg", ".jpeg"):
                continue
            if path.stem not in seen or path.suffix.lower() == ".bin":
                seen[path.stem] = path
    return [seen[key] for key in sorted(seen)]

def golden_path(golden_dir, name, kind):
    """kind: 'hw'(内部 254x254) / 'prewitt'(内部) / 'pad256'(全尺寸)"""
    if not golden_dir:
        return None
    return Path(golden_dir) / f"{name}_edge_{kind}.bin"

def do_make_golden(images, golden_dir, size):
    """生成/校验 golden。Sobel 已有文件只校验不覆盖。"""
    golden_dir = Path(golden_dir)
    golden_dir.mkdir(parents=True, exist_ok=True)
    problems = 0

    for path in images:
        name = path.stem
        gray = load_gray(path, size)
        sobel = edge_reference(gray, "sobel")
        prewitt = edge_reference(gray, "prewitt")

        existing = golden_path(golden_dir, name, "hw")
        if existing.exists():
            ref = np.frombuffer(existing.read_bytes(), np.uint8).reshape(gray.shape[0] - 2, gray.shape[1] - 2)
            same = np.array_equal(ref, sobel[1:-1, 1:-1])
            print(f"[{name}] Sobel 与已有 golden：{'一致' if same else '不一致！'}")
            problems += (not same)
        else:
            print(f"[{name}] Sobel golden 缺失，本次生成")

        existing.write_bytes(sobel[1:-1, 1:-1].tobytes())
        golden_path(golden_dir, name, "prewitt").write_bytes(prewitt[1:-1, 1:-1].tobytes())
        # 全尺寸（边界补 0）版只在 256x256 时写出：文件名 _edge_pad256 与既有约定一致，
        # 其它尺寸由板端脚本现场算参考值，不必再存一份大文件。
        if gray.shape == (256, 256):
            golden_path(golden_dir, name, "pad256").write_bytes(sobel.tobytes())
        print(f"[{name}] 写出内部 {sobel[1:-1, 1:-1].size} B")

    print("\n完成。" + ("" if problems == 0 else f" 有 {problems} 处不一致，请检查！"))
    return 0 if problems == 0 else 1

def do_benchmark(args, images, size):
    print(f"算法 = {args.mode}   阈值 = {args.threshold}   "
          f"warm-up = {args.warmup}   正式 = {args.repeats} 次")
    print(f"共 {len(images)} 张图\n")

    rows = []
    all_pass = True

    for path in images:
        try:
            gray = load_gray(path, size)
        except (ImportError, ValueError) as exc:
            print(f"== {path.stem} ==  跳过：{exc}\n")
            continue

        resolution = f"{gray.shape[1]}x{gray.shape[0]}"
        name = path.stem
        print(f"== {name}  ({resolution} uint8) ==")

        reference = edge_reference(gray, args.mode, args.threshold)

        # 正确性：mode=sobel/threshold=0 时与仓库 golden 对拍
        correct = "PASS"
        gold = golden_path(args.golden, name, "hw" if args.mode == "sobel" else "prewitt")
        if gold is not None and gold.exists() and args.threshold == 0:
            ref = np.frombuffer(gold.read_bytes(), np.uint8).reshape(gray.shape[0] - 2, gray.shape[1] - 2)
            stats = compare(ref, reference[1:-1, 1:-1])
            print(f"    对拍 {gold.name}：{stats['verdict']}  "
                  f"不同像素={stats['different_pixels']}  最大误差={stats['max_error']}")
            correct = stats["verdict"]
            all_pass = all_pass and correct == "PASS"
        else:
            print("    （无 golden 或带阈值，跳过对拍）")

        border_ok = bool(reference[0, :].max() == 0 and reference[-1, :].max() == 0
                         and reference[:, 0].max() == 0 and reference[:, -1].max() == 0)
        print(f"    最外一圈全 0：{'是' if border_ok else '否'}")

        samples = time_call(lambda: edge_reference(gray, args.mode, args.threshold),
                            args.warmup, args.repeats)
        stats = summarize(samples)
        print(f"    PS 基线  mean={stats['mean_ms']:.3f}  median={stats['median_ms']:.3f}  "
              f"min={stats['min_ms']:.3f}  max={stats['max_ms']:.3f} ms  "
              f"| FPS={stats['fps_mean']:.1f}\n")

        for index, value in enumerate(samples, 1):
            rows.append({
                "mode": args.mode, "image": name, "resolution": resolution,
                "threshold": args.threshold, "run_id": index,
                "ms": round(value, 4), "fps": round(1000.0 / value, 2),
                "correct": correct, "different_pixels": 0 if correct == "PASS" else "",
            })

    if args.out:
        write_csv(rows, args.out, SAMPLE_FIELDS)
        print(f"逐次数据 → {args.out}  ({len(rows)} 行)")
    if args.summary:
        write_csv(summarize_rows(rows), args.summary, SUMMARY_FIELDS)
        print(f"汇总数据 → {args.summary}")

    print("\n" + ("全部 PASS" if all_pass else "存在 FAIL，请先修正确性"))
    return 0 if all_pass else 1

def main(argv=None):
    parser = argparse.ArgumentParser(description="边缘检测软件基线：读图 / 参考 / 验证 / 基准")
    parser.add_argument("--images", nargs="+", required=True, help="图片路径或目录，可给多个")
    parser.add_argument("--golden", default=None, help="golden 目录（*.bin）")
    parser.add_argument("--mode", default="sobel", choices=["sobel", "prewitt"])
    parser.add_argument("--threshold", type=int, default=0,
                        help="0 = 饱和输出；>0 = 二值化（梯度 >= 阈值输出 255，否则 0）")
    parser.add_argument("--size", default=None, help="例如 256x256；不给则用图片自身尺寸")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--out", default=None, help="逐次数据 CSV")
    parser.add_argument("--summary", default=None, help="汇总 CSV")
    parser.add_argument("--make-golden", action="store_true",
                        help="生成/校验 golden（需要 --golden 与 --size）")
    args = parser.parse_args(argv)

    size = parse_size(args.size)
    images = collect_images(args.images)
    if not images:
        print("没有找到图片", file=sys.stderr)
        return 2

    if args.make_golden:
        if not args.golden:
            print("--make-golden 需要同时给出 --golden", file=sys.stderr)
            return 2
        return do_make_golden(images, args.golden, size)

    return do_benchmark(args, images, size)

if __name__ == "__main__":
    raise SystemExit(main())
