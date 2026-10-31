
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

# -*- coding: utf-8 -*-
import argparse
import base64
import hashlib
import io
import json
import socket
import time
import traceback
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import cv2
import numpy as np
from PIL import Image, ImageOps

from run_pipeline import rgb2gray, SEED_PRESET

BASE = Path(__file__).resolve().parent
BIT = BASE / "configurable_pipeline.bit"
HWH = BIT.with_suffix(".hwh")

SIZES = {(256, 256), (320, 240), (640, 480)}
MAX_BODY = 20 * 1024 * 1024

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def encode_png(array):
    stream = io.BytesIO()
    Image.fromarray(array).save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")

def decode_rgb(text, width, height):
    raw = base64.b64decode(text, validate=True)
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("Source image exceeds 12 MiB")

    with Image.open(io.BytesIO(raw)) as source:
        image = (
            ImageOps.exif_transpose(source)
            if hasattr(ImageOps, "exif_transpose")
            else source.copy()
        )
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(background, image).convert("RGB")

        # 保持比例，白底 letterbox；Golden 使用同一个实际输入。
        sw, sh = image.size
        scale = min(float(width) / sw, float(height) / sh)
        nw = min(width, max(1, int(round(sw * scale))))
        nh = min(height, max(1, int(round(sh * scale))))

        resized = cv2.resize(
            np.asarray(image),
            (nw, nh),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
        )

    rgb = np.full((height, width, 3), 255, dtype=np.uint8)
    left, top = (width - nw) // 2, (height - nh) // 2
    rgb[top:top + nh, left:left + nw] = resized
    return rgb

def annotate_candidates(rgb, output):
    """
    PS 后处理：从真实 FPGA 最终输出提取候选轮廓。
    candidate_count 是候选数量，不等同于真实种子数量。
    不修改 FPGA 原始输出或用于 Golden 的输出。
    """
    mask = np.where(output > 0, 255, 0).astype(np.uint8)

    # 忽略边界两圈，避免零填充造成的亮背景边框。
    mask[:2, :] = 0
    mask[-2:, :] = 0
    mask[:, :2] = 0
    mask[:, -2:] = 0

    found = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    contours = found[-2]

    minimum = max(20, int(output.size * 0.0005))
    boxes = []

    for contour in contours:
        if cv2.contourArea(contour) < minimum:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        boxes.append((x, y, w, h))

    boxes.sort(key=lambda box: (box[1], box[0]))
    annotated = rgb.copy()

    for number, (x, y, w, h) in enumerate(boxes, 1):
        cv2.rectangle(
            annotated, (x, y), (x + w - 1, y + h - 1),
            (30, 160, 75), 1,
        )
        cv2.putText(
            annotated, str(number), (x, max(12, y - 3)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.4,
            (30, 160, 75), 1, cv2.LINE_AA,
        )

    return annotated, len(boxes)

class Runner:
    def __init__(self, mock=False):
        self.mock = mock
        self.fault = False
        self.inp = None
        self.out = None
        self.shape = None
        self.driver = None

        if mock:
            self.version = "SIMULATED / B MockBackend"
            self.bit_sha = None
            self.hwh_sha = None
            return

        from edge_morph_driver import SobelParamDriver

        if not BIT.is_file() or not HWH.is_file():
            raise RuntimeError("Missing matching pipeline_validation bit/hwh in " + str(BASE))

        print("[pipeline_validation] Loading Overlay: " + str(BIT), flush=True)
        self.driver = SobelParamDriver(bitfile=str(BIT))

        required = {
            "width", "height", "mode", "threshold", "morph_mode",
            "enable_gray", "enable_gauss", "out_sel",
        }
        missing = required - set(self.driver.regs)
        if missing:
            raise RuntimeError("Missing pipeline_validation registers: " + str(sorted(missing)))

        self.version = BIT.name
        self.bit_sha = sha256(BIT)
        self.hwh_sha = sha256(HWH)
        print("[pipeline_validation] Overlay ready", flush=True)

    def run(self, payload):
        if self.fault:
            raise RuntimeError(
                "Previous DMA failure: restart the service before another run"
            )

        width = int(payload["width"])
        height = int(payload["height"])
        config = payload["config"]

        if (width, height) not in SIZES:
            raise ValueError("Unsupported resolution")

        enable_gray = config.get("enable_gray")
        enable_gauss = config.get("enable_gauss")
        edge = config.get("edge_mode")
        threshold = int(config["threshold"])
        morph = int(config["morph_mode"])

        if enable_gray is not True:
            raise ValueError("RGB upload requires enable_gray=True")
        if not isinstance(enable_gauss, bool):
            raise ValueError("enable_gauss must be boolean")
        if edge not in ("Sobel", "Prewitt"):
            raise ValueError("edge_mode must be Sobel or Prewitt")
        if not 0 <= threshold <= 255 or morph not in (0, 1, 2):
            raise ValueError("Invalid threshold or morph_mode")

        config = {
            "enable_gray": True,
            "enable_gauss": enable_gauss,
            "edge_mode": edge,
            "threshold": threshold,
            "morph_mode": morph,
            "out_sel": 0,
        }

        rgb = decode_rgb(payload["image_base64"], width, height)
        started = time.perf_counter()

        print(
            "[pipeline_validation] Run {}x{} {}".format(width, height, config),
            flush=True,
        )

        registers = {}
        different = None
        maximum = None
        pl_ms = None
        fps = None

        if self.mock:
            from run_pipeline import run_pipeline

            response = run_pipeline(
                rgb, width, height, config=config, backend="mock"
            )
            if response.get("error"):
                raise RuntimeError(response["error"])

            output = np.asarray(response["output_image"], dtype=np.uint8)
            reference = output.copy()
            golden_pass = None
        else:
            from pynq import allocate
            from integrated_pipeline_smoke_test import pipeline_golden

            if self.shape != (height, width):
                # 上一帧已经成功结束才允许释放、重分配。
                if self.inp is not None:
                    self.inp.freebuffer()
                    self.inp = None
                if self.out is not None:
                    self.out.freebuffer()
                    self.out = None
                self.shape = None

                self.inp = allocate(
                    shape=(width * height * 4,), dtype=np.uint8
                )
                self.out = allocate(
                    shape=(width * height,), dtype=np.uint8
                )
                self.shape = (height, width)

            # 每像素 32bit：R[23:16]、G[15:8]、B[7:0]。
            packed = (
                (rgb[:, :, 0].astype(np.uint32) << 16)
                | (rgb[:, :, 1].astype(np.uint32) << 8)
                | rgb[:, :, 2].astype(np.uint32)
            ).astype("<u4").reshape(-1).view(np.uint8)

            self.inp[:] = packed
            self.inp.flush()

            mode = 0 if edge == "Sobel" else 1
            extra = {
                "enable_gray": 1,
                "enable_gauss": int(enable_gauss),
                "out_sel": 0,
            }

            print("[pipeline_validation] DMA start", flush=True)

            try:
                seconds = self.driver.run_frame(
                    self.inp, self.out, width, height,
                    mode, threshold,
                    timeout=5.0,
                    morph_mode=morph,
                    extra_regs=extra,
                )
                self.out.invalidate()
                output = np.array(self.out, copy=True).reshape(height, width)

                expected = {
                    "width": width,
                    "height": height,
                    "mode": mode,
                    "threshold": threshold,
                    "morph_mode": morph,
                    "enable_gray": 1,
                    "enable_gauss": int(enable_gauss),
                    "out_sel": 0,
                }
                for name, value in expected.items():
                    actual = int(
                        self.driver.ctrl.read(self.driver.regs[name])
                    )
                    registers[name] = actual
                    if actual != value:
                        raise RuntimeError(
                            "Register mismatch {}: expected {}, got {}".format(
                                name, value, actual
                            )
                        )
            except Exception:
                # DMA 异常时保留缓冲区，拒绝继续提交，等服务重启。
                self.fault = True
                raise

            print("[pipeline_validation] DMA done; registers verified", flush=True)

            # 使用仓库 A 侧逐拍参考，包含级联窗口对齐与旁路语义。
            gray = rgb2gray(rgb)
            reference = pipeline_golden(
                gray, mode, threshold, morph, int(enable_gauss), 0
            )
            delta = np.abs(
                output.astype(np.int16) - reference.astype(np.int16)
            )
            different = int(np.count_nonzero(delta))
            maximum = int(delta.max())
            golden_pass = different == 0
            pl_ms = float(seconds) * 1000.0
            fps = 1.0 / float(seconds)

        diff = np.abs(
            output.astype(np.int16) - reference.astype(np.int16)
        ).astype(np.uint8)

        annotated, count = annotate_candidates(rgb, output)
        print("[pipeline_validation] Encoding response", flush=True)

        result = {
            "schema_version": 2,
            "ok": True,
            "simulated": self.mock,
            "overlay_version": self.version,
            "bit_sha256": self.bit_sha,
            "hwh_sha256": self.hwh_sha,
            "config": config,
            "registers": registers,
            "fpga_online": not self.mock,
            "dma_ready": not self.mock,
            "golden_pass": golden_pass,
            "different_pixel_count": different,
            "max_abs_diff": maximum,
            "pl_e2e_ms": pl_ms,
            "fps": fps,
            "candidate_count": count,
            "annotation_method": "PS external contours; border 2 excluded",
            "timing_scope": "driver parameters + DMA + completion polling",
            "input_bytes": width * height * 4,
            "output_bytes": width * height,
            "images_base64": {
                "original": encode_png(rgb),
                "output": encode_png(output),
                "annotated": encode_png(annotated),
                "golden": encode_png(reference),
                "diff": encode_png(diff),
            },
            "error": None,
        }
        result["server_ms"] = (time.perf_counter() - started) * 1000.0
        return result

class SerialServer(HTTPServer):
    allow_reuse_address = True

    def get_request(self):
        connection, address = super().get_request()
        # 浏览器空闲连接或不完整请求不能无限占用单线程服务。
        connection.settimeout(8.0)
        return connection, address

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    runner = None

    def reply(self, code, obj):
        raw = json.dumps(
            obj, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self):
        if self.path != "/health":
            self.reply(404, {"ok": False, "error": "Not found"})
            return
        self.reply(200, {
            "ok": True,
            "simulated": self.runner.mock,
            "overlay_version": self.runner.version,
            "fault": self.runner.fault,
            "seed_preset": SEED_PRESET,
        })

    def do_POST(self):
        print("[pipeline_validation] Received POST " + self.path, flush=True)
        if self.path != "/run":
            self.reply(404, {"ok": False, "error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid request length")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("schema_version") != 2:
                raise ValueError("Expected schema_version=2")
            self.reply(200, self.runner.run(payload))
        except Exception as error:
            traceback.print_exc()
            try:
                self.reply(500, {
                    "ok": False,
                    "error": "{}: {}".format(type(error).__name__, error),
                })
            except (OSError, socket.timeout):
                print("[pipeline_validation] Client disconnected", flush=True)

    def log_message(self, fmt, *args):
        print("[pipeline_validation] " + fmt % args, flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.2.99")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    Handler.runner = Runner(mock=args.mock)
    server = SerialServer((args.host, args.port), Handler)
    print(
        "[pipeline_validation] READY http://{}:{}/health".format(args.host, args.port),
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[pipeline_validation] Stopped", flush=True)
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
