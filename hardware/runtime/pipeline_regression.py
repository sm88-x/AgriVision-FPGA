
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
import hashlib
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT / "hardware/drivers"))

from edge_morph_driver import SobelParamDriver
from integrated_pipeline_smoke_test import pipeline_golden

# name, out_sel, enable_gauss, edge_mode, threshold, morph_mode
CASES = [
    ("rgb2gray", 1, 0, 0, 0, 0),
    ("gaussian", 2, 1, 0, 0, 0),
    ("sobel",    3, 0, 0, 0, 0),
    ("prewitt",  3, 0, 1, 0, 0),
    ("threshold", 3, 0, 0, 40, 0),
    ("erode",   0, 1, 0, 40, 1),
    ("dilate",  0, 1, 0, 40, 2),
]
SIZES = [(256, 256), (320, 240), (640, 480)]

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def save_png(path, image):
    if not cv2.imwrite(str(path), image):
        raise IOError("Cannot write: " + str(path))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # 禁止静默使用 Mock。
    from pynq import allocate

    bit = Path(args.bit).resolve()
    hwh = bit.with_suffix(".hwh")
    if not bit.is_file() or not hwh.is_file():
        raise RuntimeError("Missing bit/hwh pair")

    # 固定清单：一旦选定，不要在当天测试中替换文件。
    names = [
        "leaf_01_dense_640x480.png",
        "leaf_02_640x480.png",
        "leaf_03_640x480.png",
        "seed_01_dense_640x480.png",
        "seed_02_640x480.png",
        "seed_03_640x480.png",
        "seed_04_640x480.png",
        "seed_05_sparse_640x480.png",
        "seed_06_noisy_640x480.png",
        "seed_07_partial_640x480.png",
    ]
    paths = [Path(args.images) / n for n in names]
    for path in paths:
        if not path.is_file():
            raise RuntimeError("Missing input: " + str(path))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    failures = out / "failures"
    failures.mkdir()

    with (out / "manifest.csv").open(
            "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["file", "sha256"])
        for path in [bit, hwh, Path(__file__),
                     ROOT / "hardware/drivers/edge_morph_driver.py",
                     ROOT / "hardware/drivers/"
                     "integrated_pipeline_smoke_test.py"] + paths:
            writer.writerow([str(path), sha256(path)])

    driver = SobelParamDriver(bitfile=str(bit))
    required = {
        "width", "height", "mode", "threshold", "morph_mode",
        "enable_gray", "enable_gauss", "out_sel",
    }
    missing = required - set(driver.regs)
    if missing:
        raise RuntimeError("Missing HWH registers: " + str(sorted(missing)))
    if driver.kernel_name != "sobel_v4_pipeline_0":
        raise RuntimeError("Unexpected kernel: " + driver.kernel_name)

    fields = [
        "image", "width", "height", "case", "different_pixels",
        "max_abs_diff", "pl_e2e_ms", "status", "error",
    ]
    passed = 0
    total = 0

    with (out / "pipeline_regression.csv").open(
            "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()

        for width, height in SIZES:
            inp = allocate(shape=(width * height,), dtype=np.uint32)
            dst = allocate(shape=(width * height,), dtype=np.uint8)
            try:
                for path in paths:
                    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
                    if bgr is None:
                        raise RuntimeError("Cannot read: " + str(path))
                    bgr = cv2.resize(
                        bgr, (width, height), interpolation=cv2.INTER_AREA)

                    b = bgr[:, :, 0].astype(np.uint32)
                    g = bgr[:, :, 1].astype(np.uint32)
                    r = bgr[:, :, 2].astype(np.uint32)
                    words = (r << 16) | (g << 8) | b
                    gray = ((77*r + 150*g + 29*b + 128) >> 8).astype(
                        np.uint8)

                    inp[:] = words.reshape(-1)
                    inp.flush()

                    for name, sel, gauss, mode, threshold, morph in CASES:
                        row = dict.fromkeys(fields, "")
                        row.update(image=path.name, width=width,
                                   height=height, case=name)
                        try:
                            reference = pipeline_golden(
                                gray, mode, threshold, morph, gauss, sel)
                            seconds = driver.run_frame(
                                inp, dst, width, height, mode, threshold,
                                timeout=5.0, morph_mode=morph,
                                extra_regs={
                                    "enable_gray": 1,
                                    "enable_gauss": gauss,
                                    "out_sel": sel,
                                })
                            dst.invalidate()
                            actual = np.array(dst).reshape(height, width)
                            diff = np.abs(actual.astype(np.int16)
                                          - reference.astype(np.int16))
                            count = int(np.count_nonzero(diff))
                            row.update(
                                different_pixels=count,
                                max_abs_diff=int(diff.max()),
                                pl_e2e_ms=round(seconds * 1000, 4),
                                status="PASS" if count == 0 else "FAIL",
                            )
                            total += 1
                            passed += int(count == 0)
                            if count:
                                stem = "{}_{}x{}_{}".format(
                                    path.stem, width, height, name)
                                save_png(failures / (stem + "_actual.png"),
                                         actual)
                                save_png(failures / (stem + "_golden.png"),
                                         reference)
                                save_png(failures / (stem + "_diff.png"),
                                         diff.astype(np.uint8))
                        except Exception as exc:
                            row.update(status="ERROR", error=repr(exc))
                            writer.writerow(row)
                            fh.flush()
                            raise
                        writer.writerow(row)
                        fh.flush()
                        print(path.name, width, height, name, row["status"])
            finally:
                inp.freebuffer()
                dst.freebuffer()

    print("PASS {}/{}".format(passed, total))
    return 0 if total == 210 and passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
