
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
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np

ROOT = PROJECT_ROOT
for directory in [
    ROOT / "board",
    ROOT / "hardware/drivers",
    ROOT / "src/software/adapters/live_chain",
    ROOT / "hardware",
]:
    sys.path.insert(0, str(directory))

import edge_morph_driver as old_driver
from integrated_pipeline_smoke_test import pipeline_golden

BaseDriver = old_driver.SobelParamDriver
LOG_PATH = None

OFFSETS = {
    "width": 0x10,
    "height": 0x18,
    "mode": 0x20,
    "threshold": 0x28,
    "morph_mode": 0x30,
    "enable_gray": 0x38,
    "enable_gauss": 0x40,
    "out_sel": 0x48,
    "roi_x0": 0x50,
    "roi_y0": 0x58,
    "roi_x1": 0x60,
    "roi_y1": 0x68,
    "foreground_count": 0x70,
    "roi_count": 0x80,
}

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def log_record(record):
    if LOG_PATH is not None:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

class StatsDriver(BaseDriver):
    def __init__(self, bitfile=None, **kwargs):
        # 不允许退回 Mock。
        from pynq import allocate
        super().__init__(bitfile=bitfile, **kwargs)

        if self.kernel_name != "sobel_v4_pipeline_0":
            raise RuntimeError("Unexpected kernel: " + self.kernel_name)

        # 某些 HLS 版本给标量数据寄存器附加 _V。
        for name, expected in OFFSETS.items():
            matches = [
                key for key in (name, name + "_V")
                if key in self.regs
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "Missing/ambiguous register {}: {}".format(
                        name, sorted(self.regs)))
            actual = self.regs[matches[0]]
            if actual != expected:
                raise RuntimeError(
                    "{} offset {} != expected {}".format(
                        name, hex(actual), hex(expected)))
            self.regs[name] = actual

        self.poisoned = False
        self.sequence = 0
        self.context = {}
        self.last_stats = None

        log_record({
            "event": "firmware",
            "bit": str(bitfile),
            "bit_sha256": digest(bitfile),
            "hwh_sha256": digest(Path(bitfile).with_suffix(".hwh")),
            "registers": {k: self.regs[k] for k in OFFSETS},
        })

    def _wait_done(self, t_start, timeout, context=""):
        core_done = False
        while True:
            mm2s = self.dma_ctrl.read(0x04)
            s2mm = self.dma_ctrl.read(0x34)
            core_done = core_done or bool(self.ctrl.read(0x00) & 0x02)

            if (mm2s | s2mm) & 0x4770:
                raise RuntimeError(
                    "DMA error MM2S=0x{:08x} S2MM=0x{:08x}".format(
                        mm2s, s2mm))

            if (mm2s & 0x1000) and (s2mm & 0x1000) and core_done:
                return

            if time.perf_counter() - t_start > timeout:
                raise TimeoutError(
                    "DMA/ap_done timeout MM2S=0x{:08x} "
                    "S2MM=0x{:08x} core_done={}".format(
                        mm2s, s2mm, core_done))
            time.sleep(0.0001)

    def run_frame(
            self, inp_buf, out_buf, width, height, mode, threshold,
            timeout=5.0, morph_mode=0, extra_regs=None):
        if self.poisoned:
            raise RuntimeError("Driver stopped after prior failure; restart")

        extra = dict(extra_regs or {})
        extra.setdefault("enable_gray", 0)
        extra.setdefault("enable_gauss", 0)
        extra.setdefault("out_sel", 0)

        defaults = {
            "roi_x0": 0, "roi_y0": 0,
            "roi_x1": width, "roi_y1": height,
        }
        for key, value in defaults.items():
            extra.setdefault(key, value)

        x0, y0 = extra["roi_x0"], extra["roi_y0"]
        x1, y1 = extra["roi_x1"], extra["roi_y1"]
        if not (0 <= x0 <= x1 <= width and 0 <= y0 <= y1 <= height):
            raise ValueError("ROI outside frame")

        self.sequence += 1
        row = {
            "event": "frame",
            "sequence": self.sequence,
            "width": width,
            "height": height,
            "mode": mode,
            "threshold": threshold,
            "morph_mode": morph_mode,
            "extra": extra,
            "context": dict(self.context),
        }
        t0 = time.perf_counter()

        try:
            # 清除可能残留的 ap_done；禁止 auto_restart。
            control = self.ctrl.read(0x00)
            if control & 0x80:
                raise RuntimeError("auto_restart must be disabled")

            seconds = super().run_frame(
                inp_buf, out_buf, width, height, mode, threshold,
                timeout=timeout, morph_mode=morph_mode,
                extra_regs=extra)

            out_buf.invalidate()
            output = np.asarray(out_buf).reshape(height, width)

            fg = int(self.ctrl.read(self.regs["foreground_count"]))
            roi = int(self.ctrl.read(self.regs["roi_count"]))
            sw_fg = int(np.count_nonzero(output))
            sw_roi = int(np.count_nonzero(output[y0:y1, x0:x1]))

            row.update(
                foreground_count=fg,
                roi_count=roi,
                software_foreground_count=sw_fg,
                software_roi_count=sw_roi,
                pl_e2e_ms=seconds * 1000,
                status="PASS" if (fg == sw_fg and roi == sw_roi)
                else "STAT_FAIL",
            )
            if row["status"] != "PASS":
                raise RuntimeError("PL/software count mismatch")

            self.last_stats = dict(row)
            log_record(row)
            return seconds

        except Exception as exc:
            self.poisoned = True
            row.update(
                status="ERROR",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                mm2s_dmasr=hex(self.dma_ctrl.read(0x04)),
                s2mm_dmasr=hex(self.dma_ctrl.read(0x34)),
            )
            log_record(row)

            # 出错后停止通道，不继续复用未知状态。
            for channel in (self.dma.sendchannel, self.dma.recvchannel):
                try:
                    channel.stop()
                except Exception:
                    pass
            raise

def regression(args):
    import pipeline_regression as regression_module

    regression_module.SobelParamDriver = StatsDriver
    sys.argv = [
        "pipeline_regression.py",
        "--bit", args.bit,
        "--images", args.images,
        "--out", args.out,
    ]
    result = regression_module.main()
    shutil.copyfile(
        Path(args.out) / "pipeline_regression.csv",
        Path(args.out) / "phenotyping_fpga_regression.csv")
    return result

def roi_test(args):
    import cv2
    from pynq import allocate

    rows = json.loads(Path(args.rois).read_text(encoding="utf-8-sig"))
    if len(rows) < 10:
        raise RuntimeError("Need at least 10 ROI records")

    driver = StatsDriver(bitfile=args.bit)
    fields = [
        "id", "image", "image_sha256", "width", "height",
        "x0", "y0", "x1", "y1", "pl_count", "sw_count",
        "count_error", "different_pixels", "pl_e2e_ms", "status",
    ]

    with (Path(args.out) / "roi_comparison.csv").open(
            "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()

        all_pass = True
        for item in rows:
            name = item["image"]
            if Path(name).name != name:
                raise ValueError("image must be a filename, not a path")
            image_path = Path(args.images) / name
            width, height = int(item["width"]), int(item["height"])
            if (width, height) not in [(256,256), (320,240), (640,480)]:
                raise ValueError("Unsupported test resolution")

            box = item["bbox_xyxy"]
            if len(box) != 4 or any(int(v) != v for v in box):
                raise ValueError("ROI coordinates must be four integers")
            x0, y0, x1, y1 = [int(v) for v in box]

            bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
            if bgr is None:
                raise RuntimeError("Cannot read " + str(image_path))
            bgr = cv2.resize(
                bgr, (width, height), interpolation=cv2.INTER_AREA)
            a = bgr.astype(np.uint32)
            gray = ((29*a[:,:,0] + 150*a[:,:,1]
                     + 77*a[:,:,2] + 128) >> 8).astype(np.uint8)
            expected = pipeline_golden(gray, 0, 40, 2, 1, 0)

            inp = allocate(shape=(width*height,), dtype=np.uint32)
            dst = allocate(shape=(width*height,), dtype=np.uint8)
            try:
                inp[:] = gray.reshape(-1).astype(np.uint32)
                inp.flush()
                driver.context = {"roi_id": item["id"], "image": name}
                seconds = driver.run_frame(
                    inp, dst, width, height, 0, 40,
                    morph_mode=2,
                    extra_regs={
                        "enable_gray": 0,
                        "enable_gauss": 1,
                        "out_sel": 0,
                        "roi_x0": x0, "roi_y0": y0,
                        "roi_x1": x1, "roi_y1": y1,
                    })
                dst.invalidate()
                actual = np.array(dst).reshape(height, width)
                different = int(np.count_nonzero(actual != expected))
                stat = driver.last_stats
                good = different == 0
                all_pass = all_pass and good

                record = {
                    "id": item["id"],
                    "image": name,
                    "image_sha256": digest(image_path),
                    "width": width, "height": height,
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "pl_count": stat["roi_count"],
                    "sw_count": stat["software_roi_count"],
                    "count_error": stat["roi_count"]
                                   - stat["software_roi_count"],
                    "different_pixels": different,
                    "pl_e2e_ms": seconds * 1000,
                    "status": "PASS" if good else "GOLDEN_FAIL",
                }
                writer.writerow(record)
                fh.flush()
                print(record)
            finally:
                inp.freebuffer()
                dst.freebuffer()

    return 0 if all_pass else 1

def serve(args):
    # FrameService 内部延迟导入此类，因此在构造服务前替换。
    old_driver.SobelParamDriver = StatsDriver

    import frame_server
    from frame_service import FrameService

    class phenotypingService(FrameService):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            if self.simulated:
                raise RuntimeError("Real PYNQ required; mock is forbidden")

        def process(self, *a, **kw):
            # frame_server 在 service_lock 内调用，统计和帧不会交叉。
            self.driver.context = {"frame_id": kw.get("frame_id")}
            return super().process(*a, **kw)

    frame_server.FrameService = phenotypingService
    result = frame_server.serve(
        port=args.port,
        bitfile=args.bit,
        mock=False,
        out_dir=args.out,
        log_name="phenotyping_300frame_stability.log",
        csv_name="board_frames.csv",
        max_frames=args.frames,
    )
    return 0 if (
        result["frames"] == args.frames and
        result["ok"] == args.frames and
        result["error"] == 0
    ) else 1

def main():
    global LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["regression", "roi", "serve"])
    ap.add_argument("--bit", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--images")
    ap.add_argument("--rois")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        raise RuntimeError("Use a NEW output directory: " + str(out))
    if args.command in ("regression", "roi") and not args.images:
        raise ValueError("--images required")
    if args.command == "roi" and not args.rois:
        raise ValueError("--rois required")
    if args.frames <= 0:
        raise ValueError("--frames must be positive")

    # 原 detection 回归程序负责创建其输出目录。
    if args.command != "regression":
        out.mkdir(parents=True)
    LOG_PATH = out / "roi_statistics.jsonl"

    if args.command == "regression":
        return regression(args)
    if args.command == "roi":
        return roi_test(args)
    return serve(args)

if __name__ == "__main__":
    sys.exit(main())
