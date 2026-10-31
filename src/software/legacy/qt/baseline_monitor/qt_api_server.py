
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
import io
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent

BITFILE_PATH = BASE_DIR / "edge_morphology_system.bit"

from edge_morph_driver import (
    SobelParamDriver,
    MODE_SOBEL,
    MODE_PREWITT,
    MORPH_BYPASS,
    MORPH_ERODE,
    MORPH_DILATE,
    edge_reference,
    pipeline_reference,
)

driver = None

def image_to_png_base64(image):
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("PNG 编码失败")
    return base64.b64encode(encoded.tobytes()).decode("ascii")

def decode_image(image_base64, width, height):
    raw = base64.b64decode(image_base64)
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    image = image.resize((width, height), Image.LANCZOS)
    return np.array(image)

def make_annotated(rgb):
    hsv = cv2.cvtColor(
        rgb,
        cv2.COLOR_RGB2HSV,
    )

    lower_green = np.array(
        [25, 35, 35],
        dtype=np.uint8,
    )

    upper_green = np.array(
        [95, 255, 255],
        dtype=np.uint8,
    )

    leaf_mask = cv2.inRange(
        hsv,
        lower_green,
        upper_green,
    )

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (3, 3),
    )

    # 只去除小噪点；不要做 Close，
    # 否则相邻叶片可能被再次粘连。
    leaf_mask = cv2.morphologyEx(
        leaf_mask,
        cv2.MORPH_OPEN,
        kernel,
        iterations=1,
    )

    height, width = leaf_mask.shape

    min_area = max(
        20,
        int(width * height * 0.0004),
    )

    component_count, labels, stats, _ = (
        cv2.connectedComponentsWithStats(
            leaf_mask,
            connectivity=8,
        )
    )

    annotated = rgb.copy()
    object_count = 0

    for index in range(1, component_count):
        x, y, w, h, area = stats[index]

        if area < min_area:
            continue

        object_count += 1

        cv2.rectangle(
            annotated,
            (int(x), int(y)),
            (int(x + w), int(y + h)),
            (0, 220, 90),
            2,
        )

        cv2.putText(
            annotated,
            str(object_count),
            (int(x), max(18, int(y - 5))),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 220, 90),
            2,
            cv2.LINE_AA,
        )

    return annotated, object_count

def process_request(request):
    global driver

    width = int(request["width"])
    height = int(request["height"])
    threshold = int(request["threshold"])
    edge_mode = request["edge_mode"].lower()
    morph_mode = int(request["morph_mode"])
    
    print(
    "[Qt API] begin:",
    f"{width}x{height}",
    f"edge={edge_mode}",
    f"morph={morph_mode}",
    flush=True,
)

    if (width, height) not in {
        (256, 256),
        (320, 240),
        (640, 480),
    }:
        raise ValueError("只支持 256x256、320x240、640x480")

    if edge_mode == "sobel":
        mode = MODE_SOBEL
    elif edge_mode == "prewitt":
        mode = MODE_PREWITT
    else:
        raise ValueError("当前 FPGA 仅支持 Sobel 或 Prewitt，不支持 Edge Off")

    if morph_mode not in {
        MORPH_BYPASS,
        MORPH_ERODE,
        MORPH_DILATE,
    }:
        raise ValueError("morph_mode 必须为 0、1 或 2")

    rgb = decode_image(
        request["image_base64"],
        width,
        height,
    )

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    if driver is None:
        print(
            "[Qt API] loading Overlay...",
            flush=True,
        )

        driver = SobelParamDriver(
            bitfile=str(BITFILE_PATH)
        )
        print(
            "[Qt API] Overlay ready",
            flush=True,
        )

    input_buffer = None
    output_buffer = None

    try:
        from pynq import allocate

        input_buffer = allocate(
            shape=(width * height,),
            dtype=np.uint8,
        )

        output_buffer = allocate(
            shape=(width * height,),
            dtype=np.uint8,
        )

        input_buffer[:] = gray.reshape(-1)
        input_buffer.flush()

        started = time.perf_counter()

        print(
            "[Qt API] DMA/FPGA start",
            flush=True,
        )
        driver.run_frame(
            input_buffer,
            output_buffer,
            width,
            height,
            mode,
            threshold,
            timeout=5.0,
            morph_mode=morph_mode,
        )
        print(
            "[Qt API] DMA/FPGA done",
            flush=True,
        )

        output_buffer.invalidate()

        pl_e2e_ms = (
            time.perf_counter() - started
        ) * 1000.0

        fpga_result = np.array(
            output_buffer,
            copy=True,
        ).reshape(height, width)

    finally:
        if input_buffer is not None:
            input_buffer.freebuffer()

        if output_buffer is not None:
            output_buffer.freebuffer()

    edge = edge_reference(
        gray,
        mode,
        threshold,
    )

    golden = pipeline_reference(
        gray,
        mode,
        threshold,
        morph_mode,
    )

    diff = cv2.absdiff(
        fpga_result,
        golden,
    )

    different_pixel_count = int(
        np.count_nonzero(diff)
    )

    max_diff = int(diff.max())

    annotated, object_count = make_annotated(
        rgb,
    )

    return {
        "schema_version": 1,
        "ok": True,
        "execution": "fpga",
        "fpga_online": True,
        "dma_ready": True,
        "pl_e2e_ms": round(pl_e2e_ms, 3),
        "fps": round(
            1000.0 / pl_e2e_ms,
            3,
        ),
        "object_count": object_count,
        "golden": {
            "correct": different_pixel_count == 0,
            "different_pixel_count": different_pixel_count,
            "max_diff": max_diff,
        },
        "images_base64": {
            "edge": image_to_png_base64(edge),
            "morphology": image_to_png_base64(
                fpga_result
            ),
            "annotated": image_to_png_base64(
                cv2.cvtColor(
                    annotated,
                    cv2.COLOR_RGB2BGR,
                )
            ),
        },
    }

class ApiHandler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "AgriVision FPGA API",
                },
            )
            return

        self.send_json(
            404,
            {"ok": False, "error": "Not found"},
        )

    def do_POST(self):
        print(
            "[Qt API] received POST: {}".format(
                self.path
            ),
            flush=True,
        )

        if self.path != "/run":
            self.send_json(
                404,
                {
                    "ok": False,
                    "error": "Not found",
                },
            )
            return

        try:
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            raw = self.rfile.read(length)

            request = json.loads(
                raw.decode("utf-8")
            )

            response = process_request(request)

            self.send_json(
                200,
                response,
            )

        except Exception as error:
            import traceback

            print(
                "[Qt API] ERROR: {!r}".format(
                    error
                ),
                flush=True,
            )

            traceback.print_exc()

            self.send_json(
                500,
                {
                    "schema_version": 1,
                    "ok": False,
                    "error": str(error),
                },
            )
            
    def log_message(self, format, *args):
        print(
            "[Qt API]",
            format % args,
        )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        default="192.168.2.99",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
    )

    args = parser.parse_args()

    server = HTTPServer(
        (args.host, args.port),
        ApiHandler,
    )

    print(
        "AgriVision API started:",
        f"http://{args.host}:{args.port}",
    )

    server.serve_forever()

if __name__ == "__main__":
    main()
