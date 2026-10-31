#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""network：电脑1 帧发送端（摄像头/图片 → JPEG → TCP 长度前缀），用于现场与本地自测。

协议：4 字节大端长度 + JPEG 负载，逐帧发送（与 `network_backend.py` 配套）。
断线自动重连；`--frames 0` 持续发送直到 Ctrl+C。

用法（电脑1）：
    python src/software/app/timestamped_frame_sender.py --host 192.168.1.20 --port 9100 \
        --camera 0 --width 640 --height 480 --fps 10

本地单机自测（不用第二台电脑）：
    python src/software/app/timestamped_frame_sender.py --port 9100 \
        --images data/fixtures/agricultural_images/seed_01_dense_640x480.png --fps 8 --frames 50
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
import socket
import struct
import time
from pathlib import Path

import cv2

def frame_source(args):
    if args.images:
        p = Path(args.images)
        paths = sorted(q for q in (p.glob("*") if p.is_dir() else [p])
                       if q.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"))
        if not paths:
            raise SystemExit("没有找到图片: " + args.images)
        i = 0
        while True:
            img = cv2.imread(str(paths[i % len(paths)]))
            if img is not None:
                yield cv2.resize(img, (args.width, args.height))
            i += 1
    else:
        cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise SystemExit("打不开摄像头 %d" % args.camera)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        try:
            while True:
                ok, img = cap.read()
                if ok:
                    yield img
                else:
                    time.sleep(0.01)
        finally:
            cap.release()

def connect(host, port, retries=30):
    for attempt in range(retries):
        try:
            sock = socket.create_connection((host, port), timeout=3.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("已连接 %s:%d" % (host, port), flush=True)
            return sock
        except OSError as exc:
            if attempt == 0:
                print("连接失败（%s），重试中…" % exc, flush=True)
            time.sleep(1.0)
    raise SystemExit("连不上 %s:%d" % (host, port))

def main():
    ap = argparse.ArgumentParser(description="network 帧发送端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--images", default="", help="图片或目录（给了就不用摄像头）")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--frames", type=int, default=0, help="0 = 一直发")
    ap.add_argument("--quality", type=int, default=80)
    args = ap.parse_args()

    sock = connect(args.host, args.port)
    sent, start, last_report = 0, time.time(), time.time()
    interval = 1.0 / max(args.fps, 0.1)
    try:
        for frame in frame_source(args):
            ok, buf = cv2.imencode(".jpg", frame,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), args.quality])
            if not ok:
                continue
            payload = buf.tobytes()
            try:
                sock.sendall(struct.pack(">I", len(payload)) + payload)
            except OSError:
                print("发送中断，重连…", flush=True)
                try:
                    sock.close()
                except OSError:
                    pass
                sock = connect(args.host, args.port)
                continue
            sent += 1
            now = time.time()
            if now - last_report >= 2.0:
                print("已发送 %d 帧 | %.1f fps | %.1f KB/帧"
                      % (sent, sent / max(1e-6, now - start), len(payload) / 1024), flush=True)
                last_report = now
            if args.frames and sent >= args.frames:
                break
            time.sleep(max(0.0, interval - (time.time() - now)))
    finally:
        try:
            sock.close()
        except OSError:
            pass
    print("发送结束：%d 帧，用时 %.1f 秒（%.2f fps）"
          % (sent, time.time() - start, sent / max(1e-6, time.time() - start)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
