
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
import json
import socket
import sys
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT / "src/software/adapters/live_chain"))
sys.path.insert(0, str(ROOT / "hardware/drivers"))

import frame_protocol as fp
from integrated_pipeline_smoke_test import pipeline_golden

def recv_exact(sock, size):
    parts = []
    while size:
        part = sock.recv(size)
        if not part:
            raise ConnectionError("Peer closed before complete frame")
        parts.append(part)
        size -= len(part)
    return b"".join(parts)

class BoardError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)

class detectionClient:
    """单线程调用；一条连接同时只允许一个请求。"""

    def __init__(self, host, port=9000, timeout=8.0):
        self.session_id = uuid.uuid4().hex
        self.frame_id = 0
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self):
        self.sock.close()

    def process(self, bgr, capture_time_ns=None):
        if bgr.dtype != np.uint8 or bgr.ndim != 3 or bgr.shape[2] != 3:
            raise ValueError("Expected uint8 HxWx3 BGR")
        if self.frame_id > 0xffffffff:
            raise RuntimeError("Start a new session before frame_id wraps")

        raw = np.ascontiguousarray(bgr).copy()
        height, width = raw.shape[:2]
        if (width, height) not in [(256, 256), (320, 240), (640, 480)]:
            raise ValueError("Use one of the three verified resolutions")

        a = raw.astype(np.uint32)
        gray = ((29*a[:, :, 0] + 150*a[:, :, 1]
                 + 77*a[:, :, 2] + 128) >> 8).astype(np.uint8)

        fid = self.frame_id
        self.frame_id += 1

        # v1 是 uint32；必须截断，不能直接塞入完整单调时钟值。
        timestamp = (time.monotonic_ns() // 1_000_000) & 0xffffffff
        body = gray.tobytes()
        header = fp.pack_message(
            fp.MSG_REQUEST, fid, timestamp, width, height, 1,
            mode=0, morph_mode=2,
            flags=fp.FLAG_ENABLE_GAUSS,
            pipeline_mode=0, threshold=40,
            payload_len=len(body), status=0)

        t0 = time.perf_counter()
        try:
            self.sock.sendall(header + body)
            head = fp.unpack_message(recv_exact(self.sock, fp.HEADER_SIZE))

            if head["magic"] != fp.MAGIC or head["version"] != fp.VERSION:
                raise RuntimeError("Bad response magic/version")
            if head["frame_id"] != fid:
                raise RuntimeError("Response frame_id mismatch")
            if not 0 <= head["payload_len"] <= fp.MAX_PAYLOAD:
                raise RuntimeError("Invalid payload length")

            if head["msg_type"] == fp.MSG_ERROR:
                message = recv_exact(self.sock, head["payload_len"])
                raise BoardError(
                    head["status"], message.decode("utf-8", errors="replace"))

            fp.validate(head, head["payload_len"], fp.MSG_RESPONSE)
            if head["status"] != 0:
                raise BoardError(head["status"], "Nonzero response status")
            if (head["width"], head["height"], head["channels"]) != (
                    width, height, 1):
                raise RuntimeError("Response dimensions/channels mismatch")
            if head["timestamp_ms"] != timestamp:
                raise RuntimeError("Response timestamp mismatch")
            if (head["mode"], head["morph_mode"], head["threshold"],
                    head["pipeline_mode"], head["flags"]) != (
                    0, 2, 40, 0, fp.FLAG_ENABLE_GAUSS):
                raise RuntimeError("Response configuration mismatch")

            payload = recv_exact(self.sock, head["payload_len"])
        except Exception:
            # 发生超时或协议异常后，不继续复用可能已错位的字节流。
            self.close()
            raise

        rtt_ms = (time.perf_counter() - t0) * 1000
        processed = np.frombuffer(payload, np.uint8).reshape(
            height, width).copy()

        return {
            "session_id": self.session_id,
            "frame_id": fid,
            "capture_time_ns": capture_time_ns,
            "timestamp_ms": timestamp,
            "width": width,
            "height": height,
            "raw_bgr": raw,
            "input_gray": gray,
            "pl_gray": processed,
            "rtt_ms": rtt_ms,
        }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["image", "live"])
    ap.add_argument("--host", default="192.168.2.99")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--images")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--count", type=int)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    count = args.count or (30 if args.mode == "image" else 300)
    if count < 1:
        raise ValueError("count must be positive")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)

    paths = []
    if args.mode == "image":
        if not args.images:
            raise ValueError("--images is required")
        paths = sorted(Path(args.images).glob("*.png"))
        if len(paths) < 10:
            raise RuntimeError("Need at least 10 PNG images")
        paths = paths[:10]

    cap = None
    client = None
    ok = 0
    attempted = 0
    fields = [
        "session_id", "frame_id", "source", "width", "height",
        "capture_time_ns", "rtt_ms", "different_pixels",
        "status", "error",
    ]

    try:
        if args.mode == "live":
            backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
            cap = cv2.VideoCapture(args.camera, backend)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
            if not cap.isOpened():
                raise RuntimeError("Camera open failed")

        client = detectionClient(args.host, args.port)
        with (out / "pc_frames.csv").open(
                "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()

            for index in range(count):
                row = dict.fromkeys(fields, "")
                row.update(session_id=client.session_id,
                           frame_id=client.frame_id,
                           width=args.width, height=args.height)
                attempted += 1
                try:
                    if cap is not None:
                        good, bgr = cap.read()
                        if not good:
                            raise RuntimeError("Camera read failed")
                        source = "camera:{}".format(args.camera)
                    else:
                        path = paths[index % len(paths)]
                        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
                        if bgr is None:
                            raise RuntimeError("Cannot read " + str(path))
                        source = path.name

                    capture_ns = time.time_ns()
                    row.update(source=source, capture_time_ns=capture_ns)

                    # 在同一次 resize 后分流，原帧/PL帧保持共同坐标系。
                    bgr = cv2.resize(
                        bgr, (args.width, args.height),
                        interpolation=cv2.INTER_AREA)
                    packet = client.process(bgr, capture_ns)

                    reference = pipeline_golden(
                        packet["input_gray"], 0, 40, 2, 1, 0)
                    diff = int(np.count_nonzero(
                        packet["pl_gray"] != reference))
                    row.update(
                        rtt_ms=round(packet["rtt_ms"], 4),
                        different_pixels=diff,
                        status="PASS" if diff == 0 else "GOLDEN_FAIL")
                    ok += int(diff == 0)

                    # 保存前10帧，按不可变帧号配对；避免 latest 文件竞争。
                    if index < 10:
                        for key in ("raw_bgr", "pl_gray"):
                            target = out / "{}_{:06d}.png".format(
                                key, packet["frame_id"])
                            if not cv2.imwrite(str(target), packet[key]):
                                raise IOError("Cannot write " + str(target))

                except Exception as exc:
                    status = (
                        "BOARD_" + str(exc.status)
                        if isinstance(exc, BoardError)
                        else type(exc).__name__
                    )
                    row.update(status=status, error=str(exc))
                    writer.writerow(row)
                    fh.flush()
                    raise

                writer.writerow(row)
                fh.flush()

    finally:
        if client is not None:
            client.close()
        if cap is not None:
            cap.release()
        summary = {
            "mode": args.mode,
            "requested": count,
            "attempted": attempted,
            "passed": ok,
            "pass_rate_of_requested": ok / count,
            "completed": attempted == count and ok == count,
            "note": "PC RTT excludes local Golden and image saving; "
                    "PL timing must come from board CSV",
        }
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))

    return 0 if ok == count else 1

if __name__ == "__main__":
    sys.exit(main())
