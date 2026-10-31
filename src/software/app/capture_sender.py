#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""network B：电脑1 摄像头采集 → JPEG → TCP 发送。

职责边界：
  - 电脑1 只做：内置摄像头取帧、JPEG 编码、TCP 发送、可选本地预览。
  - 不做 YOLO / FPGA / Qt；这些全部在电脑2。
  - 发送端也保留“最新帧”槽位：网络慢时丢旧帧并计数，不无界排队。

协议：docs/frame_protocol.md（4B 大端长度 + 20B 头 + JPEG）。
本文件与 network_frame_receiver.py 共用 timestamped_frame_protocol.py 的常量。

常用命令：
    # 电脑1：向电脑2（假设 192.168.1.20）发送
    python capture_sender.py --host 192.168.1.20 --port 9100

    # 没有摄像头时，本机自测（合成图案，不用于现场演示）
    python capture_sender.py --host 127.0.0.1 --synthetic --frames 60 --no-show

    # 不知道内置摄像头是哪个 index 时
    python capture_sender.py --probe

    # 只看本地画面，不发送
    python capture_sender.py --show-only
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
import json
import socket
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import timestamped_frame_protocol as proto  # noqa: E402

BACKENDS = {
    "dshow": getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY),
    "msmf": getattr(cv2, "CAP_MSMF", cv2.CAP_ANY),
    "any": cv2.CAP_ANY,
}

def synthetic_frame(frame_id, width, height):
    """无摄像头自测图案；画面里明确写 SYNTHETIC TEST，避免误当真实数据。"""
    img = np.full((height, width, 3), 24, np.uint8)
    step = 80
    for x in range(0, width, step):
        cv2.line(img, (x, 0), (x, height - 1), (48, 48, 48), 1)
    for y in range(0, height, step):
        cv2.line(img, (0, y), (width - 1, y), (48, 48, 48), 1)

    cx = 50 + int((frame_id * 7) % max(width - 100, 1))
    cy = 50 + int((frame_id * 5) % max(height - 100, 1))
    cv2.circle(img, (cx, cy), 34, (70, 180, 95), -1)
    cv2.circle(img, (cx, cy), 34, (230, 255, 230), 2)

    cv2.putText(img, "SYNTHETIC TEST", (12, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 220, 120), 2,
                cv2.LINE_AA)
    cv2.putText(img, f"frame {frame_id}", (12, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (230, 230, 230), 2,
                cv2.LINE_AA)
    cv2.putText(img, time.strftime("%H:%M:%S"), (12, height - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1,
                cv2.LINE_AA)
    return img

class CaptureSender:
    """采集线程 + 发送线程；read 侧只保留最新一帧。"""

    def __init__(self, host="127.0.0.1", port=proto.DEFAULT_PORT,
                 camera=0, backend="dshow", width=640, height=480,
                 fps=15.0, fourcc="MJPG", jpeg_quality=80,
                 synthetic=False, show=True, reconnect_delay=1.0,
                 timeout=5.0, max_frames=0, send_enabled=True):
        self.host = str(host)
        self.port = int(port)
        self.camera = int(camera)
        self.backend = str(backend).lower()
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps) if float(fps) > 0 else 15.0
        self.fourcc = str(fourcc)
        self.jpeg_quality = int(jpeg_quality)
        self.synthetic = bool(synthetic)
        self.show = bool(show)
        self.reconnect_delay = float(reconnect_delay)
        self.timeout = float(timeout)
        self.max_frames = int(max_frames)
        self.send_enabled = bool(send_enabled)

        self._cap = None
        self._sock = None
        self._slot = {"frame": None, "frame_id": 0, "ts_ns": 0,
                      "fresh": False}
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._capture_thread = None
        self._send_thread = None

        self._captured = 0
        self._sent = 0
        self._capture_dropped = 0
        self._send_errors = 0
        self._encode_errors = 0
        self._connect_attempts = 0
        self._connect_failures = 0
        self._connect_success = 0
        self._reconnects = 0
        self._bytes_sent = 0
        self._last_sent_id = 0
        self._last_error = ""
        self._capture_error = ""
        self._t0 = None

    # ------------------------------------------------------------ 生命周期
    def _open_camera(self):
        if self.synthetic:
            return None
        api = BACKENDS.get(self.backend, cv2.CAP_ANY)
        cap = cv2.VideoCapture(self.camera, api)
        if not cap.isOpened():
            raise RuntimeError(
                f"摄像头 index={self.camera} backend={self.backend} 打不开；"
                f"先用 --probe 查可用 index")
        cap.set(cv2.CAP_PROP_FOURCC,
                cv2.VideoWriter_fourcc(*self.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        try:
            # 尽量减小驱动缓冲；不支持的后端会忽略
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:                       # noqa: BLE001
            pass
        return cap

    def start(self):
        try:
            self._cap = self._open_camera()
        except RuntimeError as exc:
            self._capture_error = str(exc)
            return False
        self._t0 = time.monotonic()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="capture", daemon=True)
        self._capture_thread.start()
        if self.send_enabled:
            self._send_thread = threading.Thread(
                target=self._send_loop, name="send", daemon=True)
            self._send_thread.start()
        return True

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._close_socket()
        for th in (self._capture_thread, self._send_thread):
            if th is not None:
                th.join(timeout=2.0)
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    # ------------------------------------------------------------ 采集
    def _capture_loop(self):
        next_t = time.monotonic()
        while not self._stop.is_set():
            if self.synthetic:
                now = time.monotonic()
                if next_t > now:
                    if self._stop.wait(next_t - now):
                        break
                next_t += 1.0 / self.fps
                frame = synthetic_frame(self._captured + 1,
                                        self.width, self.height)
            else:
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    self._capture_error = "摄像头读帧失败/断开"
                    break
            if frame is None or getattr(frame, "size", 0) == 0:
                self._capture_error = "摄像头返回空帧"
                break
            ts_ns = time.time_ns()
            self._captured += 1
            with self._cond:
                if self._slot["fresh"] and self.send_enabled:
                    self._capture_dropped += 1
                self._slot.update(frame=frame, frame_id=self._captured,
                                  ts_ns=ts_ns, fresh=True)
                self._cond.notify_all()
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------ 发送
    def _take_latest(self, timeout=0.5):
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._slot["fresh"]:
                if self._stop.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            item = (self._slot["frame"], self._slot["frame_id"],
                    self._slot["ts_ns"])
            self._slot["fresh"] = False
            return item

    def _ensure_socket(self):
        if self._sock is not None:
            return
        self._connect_attempts += 1
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.timeout)
        except OSError:
            self._connect_failures += 1
            raise
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._connect_success += 1
        if self._connect_success > 1:
            self._reconnects += 1

    def _close_socket(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _send_loop(self):
        while not self._stop.is_set():
            item = self._take_latest(timeout=0.5)
            if item is None:
                continue
            frame, frame_id, ts_ns = item
            ok, buf = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            if not ok:
                self._encode_errors += 1
                continue
            h, w = frame.shape[:2]
            try:
                packet = proto.pack_frame(frame_id, ts_ns, w, h,
                                          buf.tobytes())
            except ValueError as exc:
                self._encode_errors += 1
                self._last_error = str(exc)
                continue

            sent = False
            for _ in range(2):
                try:
                    self._ensure_socket()
                    self._sock.sendall(packet)
                    sent = True
                    break
                except OSError as exc:
                    self._last_error = str(exc)
                    self._close_socket()
                    self._send_errors += 1
                    if self._stop.wait(self.reconnect_delay):
                        break
            if sent:
                self._sent += 1
                self._bytes_sent += len(packet)
                self._last_sent_id = frame_id
                if self.max_frames and self._sent >= self.max_frames:
                    self._stop.set()
                    with self._cond:
                        self._cond.notify_all()

    # ------------------------------------------------------------ 统计/运行
    def stats(self):
        elapsed = (max(time.monotonic() - self._t0, 1e-6)
                   if self._t0 else 0.0)
        return {
            "role": "computer1_sender",
            "target": (f"{self.host}:{self.port}" if self.send_enabled
                       else "local-preview"),
            "source": "synthetic" if self.synthetic
                      else f"camera:{self.camera}/{self.backend}",
            "width": self.width,
            "height": self.height,
            "fps_target": self.fps,
            "frames_captured": self._captured,
            "frames_sent": self._sent,
            "capture_dropped": self._capture_dropped,
            "send_errors": self._send_errors,
            "encode_errors": self._encode_errors,
            "connect_attempts": self._connect_attempts,
            "connect_failures": self._connect_failures,
            "reconnects": self._reconnects,
            "bytes_sent": self._bytes_sent,
            "last_sent_frame_id": self._last_sent_id,
            "fps_capture": round(self._captured / elapsed, 1)
                            if elapsed else 0.0,
            "fps_send": round(self._sent / elapsed, 1) if elapsed else 0.0,
            "last_error": self._last_error,
            "capture_error": self._capture_error,
        }

    def run(self, duration=0.0, print_every=2.0):
        t0 = time.monotonic()
        last_print = t0
        show_ok = self.show
        while not self._stop.is_set():
            now = time.monotonic()
            if duration and now - t0 >= duration:
                break
            if self._capture_error:
                print(f"[tx] 采集停止: {self._capture_error}", flush=True)
                break

            if show_ok:
                with self._cond:
                    frame = self._slot["frame"]
                if frame is not None:
                    try:
                        cv2.imshow("capture_sender - computer 1", frame)
                        key = cv2.waitKey(1) & 0xFF
                        if key in (27, ord("q")):
                            break
                    except cv2.error as exc:
                        print(f"[tx] 预览窗口不可用，改为无界面: {exc}",
                              flush=True)
                        show_ok = False

            if print_every > 0 and now - last_print >= print_every:
                st = self.stats()
                print(f"[tx] sent={st['frames_sent']} "
                      f"captured={st['frames_captured']} "
                      f"drop={st['capture_dropped']} "
                      f"fps={st['fps_send']} "
                      f"reconnect={st['reconnects']} "
                      f"err={st['last_error'][:60]}", flush=True)
                last_print = now
            time.sleep(0.01)
        self.stop()

# ------------------------------------------------------------------
# 摄像头探测
# ------------------------------------------------------------------
def probe_cameras(max_index=5):
    rows = []
    for index in range(max_index + 1):
        for name in ("dshow", "msmf", "any"):
            cap = cv2.VideoCapture(index, BACKENDS[name])
            if not cap.isOpened():
                cap.release()
                continue
            ok, frame = cap.read()
            if ok and frame is not None:
                rows.append({
                    "index": index,
                    "backend": name,
                    "width": int(frame.shape[1]),
                    "height": int(frame.shape[0]),
                })
                print(f"  OK  index={index} backend={name} "
                      f"{frame.shape[1]}x{frame.shape[0]}", flush=True)
            cap.release()
    if not rows:
        print("  没有找到可用摄像头；请检查系统隐私设置/驱动，"
              "或先用 --synthetic 自测链路。", flush=True)
    return rows

# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="network 电脑1：摄像头采集 + JPEG + TCP 发送")
    ap.add_argument("--host", default="127.0.0.1",
                    help="电脑2 的 IP（同一局域网）")
    ap.add_argument("--port", type=int, default=proto.DEFAULT_PORT)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--backend", choices=tuple(BACKENDS), default="dshow")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--fourcc", default="MJPG")
    ap.add_argument("--jpeg-quality", type=int, default=80)
    ap.add_argument("--synthetic", action="store_true",
                    help="无摄像头合成图案，仅用于链路自测")
    ap.add_argument("--show", dest="show", action="store_true", default=True)
    ap.add_argument("--no-show", dest="show", action="store_false")
    ap.add_argument("--show-only", action="store_true",
                    help="只预览，不连接电脑2")
    ap.add_argument("--frames", type=int, default=0,
                    help="成功发送 N 帧后退出；0=直到 Ctrl+C")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="运行秒数；0=直到 Ctrl+C")
    ap.add_argument("--reconnect-delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--print-every", type=float, default=2.0)
    ap.add_argument("--probe", action="store_true",
                    help="探测 index 0..N 的摄像头，然后退出")
    ap.add_argument("--probe-max-index", type=int, default=5)
    args = ap.parse_args()

    if args.probe:
        print("探测摄像头（每个可用组合读一帧）：", flush=True)
        probe_cameras(args.probe_max_index)
        return 0

    if args.show_only:
        args.host = ""
        args.frames = 0

    sender = CaptureSender(
        host=args.host or "127.0.0.1", port=args.port,
        camera=args.camera, backend=args.backend,
        width=args.width, height=args.height, fps=args.fps,
        fourcc=args.fourcc, jpeg_quality=args.jpeg_quality,
        synthetic=args.synthetic, show=args.show,
        reconnect_delay=args.reconnect_delay, timeout=args.timeout,
        max_frames=args.frames, send_enabled=not args.show_only)

    if not sender.start():
        print(json.dumps({"ok": False,
                          "error": sender._capture_error},
                         ensure_ascii=False), flush=True)
        return 1

    target = "仅本地预览" if args.show_only else f"{args.host}:{args.port}"
    print(f"[tx] 启动：source="
          f"{'synthetic' if args.synthetic else 'camera'} "
          f"target={target} {args.width}x{args.height}@{args.fps:g}fps "
          f"jpeg={args.jpeg_quality}", flush=True)
    try:
        sender.run(duration=args.duration,
                   print_every=args.print_every)
    except KeyboardInterrupt:
        print("[tx] Ctrl+C，停止", flush=True)
    finally:
        sender.stop()
        if args.show:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
    st = sender.stats()
    st["ok"] = not bool(st["capture_error"])
    print(json.dumps(st, ensure_ascii=False), flush=True)
    return 0 if st["ok"] else 1

if __name__ == "__main__":
    sys.exit(main())
