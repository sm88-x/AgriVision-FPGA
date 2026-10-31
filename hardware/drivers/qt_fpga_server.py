#!/usr/bin/env python3
"""Authenticated HTTP bridge for the AgriVision Qt client, run on PYNQ-Z2.

Start from the Jupyter terminal in the directory containing this file,
edge_morph_driver.py and edge_morphology_system.bit/.hwh:

  python3 qt_fpga_server.py --bitfile edge_morphology_system.bit --token YOUR_RANDOM_TOKEN

It uses the real V4 DMA driver and performs a software Golden comparison for
every request.  It intentionally does not expose a shell or file browser.
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
import base64
import hmac
import io
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

from edge_morph_driver import (
    MODE_PREWITT, MODE_SOBEL, MORPH_BYPASS, MORPH_DILATE, MORPH_ERODE,
    SobelParamDriver, compare, edge_reference, pipeline_reference,
)

MAX_BODY = 16 * 1024 * 1024
ALLOWED_SIZES = {(256, 256), (320, 240), (640, 480)}

def png_base64(array):
    image = Image.fromarray(array)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")

def annotate(rgb, morphology):
    """Draw component outlines from the real PL morphology output."""
    canvas = Image.fromarray(rgb.copy())
    draw = ImageDraw.Draw(canvas)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (morphology > 0).astype(np.uint8), connectivity=8
    )
    item = 0
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 12:
            continue
        item += 1
        draw.rectangle((int(x), int(y), int(x + width - 1), int(y + height - 1)),
                       outline=(42, 166, 92), width=2)
        draw.text((int(x) + 2, max(0, int(y) - 13)), str(item), fill=(25, 95, 52))
    return np.asarray(canvas), item

class FpgaRunner:
    def __init__(self, bitfile):
        self.driver = SobelParamDriver(bitfile=bitfile)
        self.input_buffer = None
        self.output_buffer = None
        self.shape = None

    def run(self, payload):
        width = int(payload["width"])
        height = int(payload["height"])
        threshold = int(payload["threshold"])
        morph_mode = int(payload["morph_mode"])
        edge_name = payload["edge_mode"]
        if (width, height) not in ALLOWED_SIZES:
            raise ValueError("仅支持 256x256、320x240、640x480")
        if edge_name not in {"sobel", "prewitt"}:
            raise ValueError("当前 V4 bitstream 仅支持 sobel 或 prewitt，不支持 edge_mode=off")
        if not 0 <= threshold <= 255 or morph_mode not in {
            MORPH_BYPASS, MORPH_ERODE, MORPH_DILATE
        }:
            raise ValueError("threshold 或 morph_mode 无效")
        source = base64.b64decode(payload["image_base64"], validate=True)
        if len(source) > 12 * 1024 * 1024:
            raise ValueError("图像超过 12 MiB")
        with Image.open(io.BytesIO(source)) as opened:
            rgb = np.asarray(ImageOps.exif_transpose(opened).convert("RGB").resize(
                (width, height), Image.Resampling.LANCZOS
            ))
        gray = np.asarray(Image.fromarray(rgb).convert("L"), dtype=np.uint8)
        from pynq import allocate
        if self.shape != gray.shape:
            if self.input_buffer is not None:
                self.input_buffer.freebuffer()
                self.output_buffer.freebuffer()
            self.input_buffer = allocate(shape=(width * height,), dtype=np.uint8)
            self.output_buffer = allocate(shape=(width * height,), dtype=np.uint8)
            self.shape = gray.shape
        mode = MODE_SOBEL if edge_name == "sobel" else MODE_PREWITT
        self.input_buffer[:] = gray.reshape(-1)
        self.input_buffer.flush()
        pl_seconds = self.driver.run_frame(
            self.input_buffer, self.output_buffer, width, height, mode, threshold,
            timeout=5.0, morph_mode=morph_mode,
        )
        self.output_buffer.invalidate()
        morphology = np.array(self.output_buffer, copy=True).reshape(height, width)
        edge = edge_reference(gray, mode, threshold)
        reference = pipeline_reference(gray, mode, threshold, morph_mode)
        metrics = compare(reference, morphology)
        annotated, component_count = annotate(rgb, morphology)
        return {
            "schema_version": 1, "ok": True, "execution": "fpga",
            "fpga_online": True, "dma_ready": True,
            "pl_e2e_ms": round(float(pl_seconds) * 1000, 6),
            "fps": round(1.0 / float(pl_seconds), 4),
            "golden": {"correct": metrics["different_pixels"] == 0,
                       "different_pixel_count": metrics["different_pixels"],
                       "max_diff": metrics["max_error"]},
            "images_base64": {"edge": png_base64(edge),
                              "morphology": png_base64(morphology),
                              "annotated": png_base64(annotated)},
            "bitstream_id": self.driver.bitfile,
            "kernel_ip": self.driver.kernel_name,
            "register_map": self.driver.read_register_map(),
            "component_count": component_count,
            "timing_scope": "PL parameters + DMA send/receive + completion poll; excludes upload and PNG encoding",
        }

class Handler(BaseHTTPRequestHandler):
    runner = None
    token = ""

    def log_message(self, fmt, *args):
        print("[Qt bridge] " + fmt % args, flush=True)

    def reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        candidate = self.headers.get("X-AgriVision-Token", "")
        return hmac.compare_digest(candidate, self.token)

    def do_GET(self):
        if self.path != "/health":
            self.reply(404, {"ok": False, "error": "not found"})
            return
        self.reply(200, {"ok": True, "service": "AgriVision Qt bridge", "bitstream": self.runner.driver.bitfile})

    def do_POST(self):
        if self.path != "/process":
            self.reply(404, {"ok": False, "error": "not found"})
            return
        if not self.authorized():
            self.reply(401, {"ok": False, "error": "invalid token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if payload.get("schema_version") != 1:
                raise ValueError("unsupported schema_version")
            started = time.monotonic()
            response = self.runner.run(payload)
            response["server_total_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.reply(200, response)
        except Exception as exc:
            self.reply(400, {"schema_version": 1, "ok": False, "error": str(exc)})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bitfile", default="edge_morphology_system.bit")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    if len(args.token) < 16:
        parser.error("token 至少 16 个字符")
    Handler.runner = FpgaRunner(args.bitfile)
    Handler.token = args.token
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Qt bridge ready: http://%s:%d/health" % (args.host, args.port), flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
