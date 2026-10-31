#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""network B：电脑2 网络帧接收 → JPEG 解码 → 最新帧槽位。

与 computer1 的 capture_sender.py 配套，协议见 docs/frame_protocol.md：
    [4B 大端 body_len][20B 头][JPEG]
本文件先独立跑通“电脑1→电脑2 原始视频”；接入 YOLO/FPGA/Qt 由 B2 卡完成。

关键纪律：
  - **绝不假设一次 recv 拿到整帧**：先循环收满 4B 长度，再循环收满 body。
  - 只保留最新一帧；消费者慢时覆盖旧帧并计数，不无界排队。
  - 长度/魔数不合法 → 断开当前连接，回到 accept 等发送端重连。
  - 接收端不做 YOLO/FPGA/Qt；它只提供与 camera_capture.CameraCapture
    相同的 read()/stats() 接口，方便 B2 卡直接替换本地摄像头。

CLI：
    # 电脑2：监听 9100，弹出窗口证明连续收到画面
    python network_frame_receiver.py --port 9100

    # 只收不显示，保存第一帧作证据
    python network_frame_receiver.py --port 9100 --no-show \
        --save-dir samples/network_network --save-first --stats-json

    # 本机自测（不需要摄像头，不需要第二台电脑）
    python network_frame_receiver.py --self-test

    # 接收端监听端口 0 表示让系统分配，启动日志会打印实际端口
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
import random
import socket
import struct
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import timestamped_frame_protocol as proto  # noqa: E402

def bgr_to_gray(bgr):
    """BGR(A) → 灰度，使用与板端 golden 相同的定点式。

    Y = (77R + 150G + 29B + 128) >> 8
    与 camera_capture.CameraCapture._to_gray / run_pipeline.rgb2gray 同口径；
    不要改成 cv2.cvtColor，否则和板端逐像素对比会差 ±1。
    """
    if bgr is None:
        return None
    if bgr.ndim == 2:
        return bgr
    rgb = bgr[:, :, 2::-1]          # BGR(A) → RGB
    a = rgb.astype(np.int32)
    y = (77 * a[:, :, 0] + 150 * a[:, :, 1] + 29 * a[:, :, 2] + 128) >> 8
    return np.clip(y, 0, 255).astype(np.uint8)

def recv_exact(sock, nbytes, timeout):
    """循环收满 nbytes；超时抛 TimeoutError，对端关闭抛 ConnectionError。"""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < nbytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"收帧超时：只收到 {len(buf)}/{nbytes} 字节")
        sock.settimeout(remaining)
        chunk = sock.recv(nbytes - len(buf))
        if not chunk:
            raise ConnectionError(
                f"对端关闭：只收到 {len(buf)}/{nbytes} 字节")
        buf.extend(chunk)
    return bytes(buf)

class NetworkFrameReceiver:
    """TCP 监听 + 最新帧槽位；接口尽量与 CameraCapture 保持一致。

    read() 返回 (gray, color_bgr, frame_id, capture_ts_ns, recv_mono_ns)。
    注意：网络帧的最后一个值是本机“收到帧”的单调钟，不是发送端采集时刻；
    跨机单调钟不可比。采集→接收延迟看 stats()["latency"]。
    """

    def __init__(self, listen_host="0.0.0.0", listen_port=proto.DEFAULT_PORT,
                 timeout=5.0, max_frame_bytes=proto.MAX_FRAME_BYTES,
                 width=0, height=0):
        self.listen_host = str(listen_host)
        self.listen_port = int(listen_port)
        self._port = int(listen_port)
        self.timeout = float(timeout)
        self.max_frame_bytes = int(max_frame_bytes)
        self.width = int(width) if width else 0
        self.height = int(height) if height else 0
        self.is_file = False

        self._listen_sock = None
        self._client_sock = None
        self._accept_thread = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._slot = {"frame": None, "color": None, "seq": 0,
                      "ts_ns": 0, "ts_mono_ns": 0, "fresh": False}

        self._t0 = None
        self._frames_received = 0
        self._frames_delivered = 0
        self._frames_dropped = 0
        self._decode_errors = 0
        self._protocol_errors = 0
        self._timeouts = 0
        self._frame_gaps = 0
        self._out_of_order = 0
        self._connections = 0
        self._size_mismatch = 0
        self._last_frame_id = -1
        self._last_error = ""
        self._open_error = ""
        self._client_connected = False
        self._client_addr = ""
        self._lat_c2r = deque(maxlen=30)      # 采集→接收（墙钟，需两机对时）
        self._lat_decode = deque(maxlen=30)   # JPEG 解码耗时（本机）

    # ------------------------------------------------------------ 生命周期
    @property
    def port(self):
        # 缓存实际端口：stop() 关闭 socket 后 stats() 仍要能读到端口。
        return self._port

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.listen_host, self.listen_port))
            sock.listen(1)
        except OSError as exc:
            self._open_error = str(exc)
            try:
                sock.close()
            except OSError:
                pass
            return False
        self._listen_sock = sock
        self._port = int(sock.getsockname()[1])
        self._t0 = time.monotonic()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="net-accept", daemon=True)
        self._accept_thread.start()
        return True

    def stop(self):
        self._stop.set()
        for sock in (self._client_sock, self._listen_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------ 接收
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._listen_sock.accept()
            except OSError as exc:
                if not self._stop.is_set():
                    self._last_error = f"accept 失败: {exc}"
                break
            self._client_sock = conn
            self._client_connected = True
            self._client_addr = f"{addr[0]}:{addr[1]}"
            self._connections += 1
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._recv_loop(conn)
            finally:
                self._client_connected = False
                try:
                    conn.close()
                except OSError:
                    pass
                self._client_sock = None
        with self._cond:
            self._cond.notify_all()

    def _recv_loop(self, conn):
        last_id = None
        while not self._stop.is_set():
            try:
                prefix = recv_exact(conn, proto.LENGTH_SIZE, self.timeout)
                body_len = proto.LENGTH.unpack(prefix)[0]
                body_len = proto.validate_body_len(body_len)
                if body_len > self.max_frame_bytes:
                    raise proto.FrameProtocolError(
                        f"body_len={body_len} 超过本端上限 "
                        f"{self.max_frame_bytes}")
                body = recv_exact(conn, body_len, self.timeout)
                head = proto.parse_header(body)
            except proto.FrameProtocolError as exc:
                self._protocol_errors += 1
                self._last_error = str(exc)
                return                     # 流已不可信，断开等重连
            except TimeoutError as exc:
                self._timeouts += 1
                self._last_error = str(exc)
                return
            except ConnectionError:
                return
            except OSError as exc:
                # stop() 会主动关闭 socket，这是正常退出，别记成故障
                if not self._stop.is_set():
                    self._last_error = str(exc)
                return

            recv_ts_ns = time.time_ns()
            recv_mono_ns = time.monotonic_ns()
            jpeg = body[proto.HEADER_SIZE:]
            t0 = time.perf_counter()
            bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                               cv2.IMREAD_COLOR)
            decode_ms = (time.perf_counter() - t0) * 1000.0
            if bgr is None:
                self._decode_errors += 1
                self._last_error = "JPEG 解码失败"
                continue

            got_w, got_h = int(bgr.shape[1]), int(bgr.shape[0])
            if (got_w, got_h) != (head["width"], head["height"]):
                self._size_mismatch += 1
            if self.width and self.height \
                    and (got_w, got_h) != (self.width, self.height):
                # B2 接入时按 Qt/后端请求尺寸归一，避免下游拿到意外尺寸
                bgr = cv2.resize(bgr, (self.width, self.height),
                                 interpolation=cv2.INTER_AREA)

            gray = bgr_to_gray(bgr)
            with self._cond:
                if self._slot["fresh"]:
                    self._frames_dropped += 1
                self._slot.update(frame=gray, color=bgr,
                                  seq=head["frame_id"],
                                  ts_ns=head["capture_ts_ns"],
                                  ts_mono_ns=recv_mono_ns,
                                  fresh=True)
                self._cond.notify_all()

            self._frames_received += 1
            self._last_frame_id = head["frame_id"]
            if last_id is not None:
                if head["frame_id"] > last_id + 1:
                    self._frame_gaps += head["frame_id"] - last_id - 1
                elif head["frame_id"] <= last_id:
                    self._out_of_order += 1
            last_id = head["frame_id"]

            # 两机墙钟差会直接进入这个数；现场两台 Windows 都要开自动对时。
            self._lat_c2r.append(
                (recv_ts_ns - head["capture_ts_ns"]) / 1e6)
            self._lat_decode.append(decode_ms)

    # ------------------------------------------------------------ 取帧
    def read(self, timeout=1.0):
        """返回 (gray, color_bgr, frame_id, capture_ts_ns, recv_mono_ns)。

        超时/停止返回 None。最后一值是本机收到帧的单调钟（跨机不可比）。
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._slot["fresh"]:
                if self._stop.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            slot = dict(self._slot)
            self._slot["fresh"] = False
        self._frames_delivered += 1
        return (slot["frame"], slot["color"], slot["seq"],
                slot["ts_ns"], slot["ts_mono_ns"])

    # ------------------------------------------------------------ 统计
    def stats(self):
        elapsed = (max(time.monotonic() - self._t0, 1e-6)
                   if self._t0 else 0.0)
        c2r = list(self._lat_c2r)
        dec = list(self._lat_decode)
        return {
            "role": "computer2_receiver",
            "listen": f"{self.listen_host}:{self.port}",
            "client_connected": self._client_connected,
            "client_addr": self._client_addr,
            "connections": self._connections,
            "reconnects": max(self._connections - 1, 0),
            "frames_received": self._frames_received,
            "frames_delivered": self._frames_delivered,
            "frames_dropped": self._frames_dropped,
            "decode_errors": self._decode_errors,
            "protocol_errors": self._protocol_errors,
            "timeouts": self._timeouts,
            "frame_gaps": self._frame_gaps,
            "out_of_order": self._out_of_order,
            "size_mismatch": self._size_mismatch,
            "last_frame_id": self._last_frame_id,
            "fps_network": round(self._frames_received / elapsed, 1)
                           if elapsed else 0.0,
            "fps_delivery": round(self._frames_delivered / elapsed, 1)
                            if elapsed else 0.0,
            "latency": {
                "capture_to_recv_ms_last":
                    round(c2r[-1], 3) if c2r else None,
                "capture_to_recv_ms_avg":
                    round(sum(c2r) / len(c2r), 3) if c2r else None,
                "decode_ms_last": round(dec[-1], 3) if dec else None,
                "decode_ms_avg":
                    round(sum(dec) / len(dec), 3) if dec else None,
            },
            "last_error": self._last_error,
            "open_error": self._open_error,
        }

# ------------------------------------------------------------------
# 自测
# ------------------------------------------------------------------
def _test_frame(frame_id, width=320, height=240):
    img = np.full((height, width, 3), 22, np.uint8)
    cv2.circle(img, (40 + frame_id * 3 % (width - 80),
                     height // 2), 28, (70, 180, 95), -1)
    cv2.putText(img, f"NET TEST {frame_id}", (10, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (230, 230, 230), 2,
                cv2.LINE_AA)
    return img

def _send_frames(port, n, start_id=1, chunked=False, delay=0.0):
    """向本机接收端发 n 帧；chunked=True 时故意拆成 1~1024B 小块。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=3.0)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        for i in range(n):
            fid = start_id + i
            img = _test_frame(fid)
            ok, buf = cv2.imencode(
                ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                raise RuntimeError("测试图 JPEG 编码失败")
            packet = proto.pack_frame(fid, time.time_ns(),
                                      img.shape[1], img.shape[0],
                                      buf.tobytes())
            if chunked:
                rng = random.Random(fid)
                pos = 0
                while pos < len(packet):
                    step = rng.choice((1, 2, 3, 5, 8, 13, 64, 256, 1024))
                    sock.sendall(packet[pos:pos + step])
                    pos += step
            else:
                sock.sendall(packet)
            if delay:
                time.sleep(delay)
    finally:
        sock.close()

def _wait_for(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False

def _new_test_receiver():
    rx = NetworkFrameReceiver(listen_host="127.0.0.1", listen_port=0,
                              timeout=2.0)
    if not rx.start():
        raise RuntimeError(f"自测接收端启动失败: {rx._open_error}")
    return rx

def _consume(rx, n, out, timeout=2.0):
    """后台消费者：连续 read n 帧，把 frame_id 放进 out。"""
    deadline = time.monotonic() + timeout * max(n, 1)
    while len(out) < n and time.monotonic() < deadline:
        item = rx.read(timeout=0.5)
        if item is not None:
            out.append(item[2])

def run_self_test():
    print("network 网络帧自测（本机 loopback，不需要摄像头）", flush=True)

    # 1) 连续帧 + 最新帧槽位
    rx = _new_test_receiver()
    got = []
    th = threading.Thread(target=_consume, args=(rx, 30, got), daemon=True)
    th.start()
    _send_frames(rx.port, 30, start_id=1, delay=0.02)
    th.join(timeout=6.0)
    st = rx.stats()
    assert st["frames_received"] == 30, st
    assert len(got) >= 28, f"只连续取到 {len(got)}/30 帧"
    assert got == sorted(got) and got[-1] == 30, got
    assert st["protocol_errors"] == 0 and st["decode_errors"] == 0, st
    assert st["frame_gaps"] == 0, st
    print(f"  PASS 1: 30 帧连续接收，取到 {len(got)} 帧，"
          f"0 协议错 / 0 解码错 / 0 网络缺口", flush=True)
    rx.stop()

    # 2) TCP 分包/粘包：每帧拆成 1~1024B 小块
    rx = _new_test_receiver()
    got = []
    th = threading.Thread(target=_consume, args=(rx, 20, got), daemon=True)
    th.start()
    _send_frames(rx.port, 20, start_id=100, chunked=True, delay=0.02)
    th.join(timeout=6.0)
    st = rx.stats()
    assert st["frames_received"] == 20, st
    assert got and got[-1] == 119, got
    assert st["protocol_errors"] == 0 and st["decode_errors"] == 0, st
    print(f"  PASS 2: 随机分片（1~1024B）20 帧全部还原，未把一次 recv 当整帧",
          flush=True)
    rx.stop()

    # 3) 发送端断开后重连
    rx = _new_test_receiver()
    _send_frames(rx.port, 10, start_id=1, delay=0.01)
    assert _wait_for(lambda: rx.stats()["frames_received"] >= 10), rx.stats()
    time.sleep(0.2)
    _send_frames(rx.port, 10, start_id=11, delay=0.01)
    assert _wait_for(lambda: rx.stats()["frames_received"] >= 20), rx.stats()
    st = rx.stats()
    assert st["connections"] >= 2, st
    print(f"  PASS 3: 发送端断开重连 {st['connections']} 次，接收端继续收帧",
          flush=True)
    rx.stop()

    # 4) 非法长度：断开当前连接，但监听不退出
    rx = _new_test_receiver()
    bad = socket.create_connection(("127.0.0.1", rx.port), timeout=3.0)
    bad.sendall(struct.pack(">I", proto.MAX_FRAME_BYTES + 1))
    bad.close()
    time.sleep(0.2)
    _send_frames(rx.port, 5, start_id=200, delay=0.01)
    assert _wait_for(lambda: rx.stats()["frames_received"] >= 5), rx.stats()
    st = rx.stats()
    assert st["protocol_errors"] >= 1, st
    print("  PASS 4: 超长 body_len 被拒绝并断开，随后新连接仍能收帧",
          flush=True)
    rx.stop()

    # 5) 消费者不取帧时只保留最新帧
    rx = _new_test_receiver()
    _send_frames(rx.port, 60, start_id=1, delay=0.0)
    assert _wait_for(lambda: rx.stats()["frames_received"] >= 60), rx.stats()
    st = rx.stats()
    assert st["frames_dropped"] > 0, st
    item = rx.read(timeout=1.0)
    assert item is not None
    assert rx.stats()["frames_delivered"] == 1, rx.stats()
    print(f"  PASS 5: 无消费者时丢弃 {st['frames_dropped']} 个旧帧，"
          f"只保留最新帧，不无界排队", flush=True)
    rx.stop()

    print("SELF-TEST ALL PASS", flush=True)
    return 0

# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def _write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)

def cmd_serve(args):
    rx = NetworkFrameReceiver(
        listen_host=args.host, listen_port=args.port,
        timeout=args.timeout, max_frame_bytes=args.max_frame_bytes,
        width=args.width, height=args.height)
    if not rx.start():
        print(f"[rx] 启动失败: {rx._open_error}", flush=True)
        return 1

    print(f"[rx] 监听 {args.host}:{rx.port} "
          f"（电脑1 用 capture_sender.py --host <本机IP> --port {rx.port}）",
          flush=True)
    if args.save_dir:
        Path(args.save_dir).mkdir(parents=True, exist_ok=True)

    show_ok = args.show
    saved = False
    last_print = time.monotonic()
    last_json = 0.0
    try:
        while True:
            item = rx.read(timeout=0.5)
            if item is not None:
                gray, color, frame_id, _, _ = item
                if args.save_first and not saved and args.save_dir:
                    out = Path(args.save_dir)
                    cv2.imwrite(str(out / "receiver_first.png"), color)
                    cv2.imwrite(str(out / "receiver_first_gray.png"), gray)
                    saved = True
                    print(f"[rx] 首帧已保存: {out / 'receiver_first.png'}",
                          flush=True)
                if show_ok:
                    try:
                        cv2.imshow("network_frame_receiver - computer 2",
                                   color)
                        key = cv2.waitKey(1) & 0xFF
                        if key in (27, ord("q")):
                            break
                    except cv2.error as exc:
                        print(f"[rx] 预览窗口不可用，改为无界面: {exc}",
                              flush=True)
                        show_ok = False

            now = time.monotonic()
            st = rx.stats()
            if args.max_frames and st["frames_received"] >= args.max_frames:
                print(f"[rx] 已达到 --max-frames {args.max_frames}", flush=True)
                break
            if args.print_every > 0 and now - last_print >= args.print_every:
                print(f"[rx] recv={st['frames_received']} "
                      f"delivered={st['frames_delivered']} "
                      f"drop={st['frames_dropped']} "
                      f"gaps={st['frame_gaps']} "
                      f"fps={st['fps_network']} "
                      f"conn={st['connections']} "
                      f"last={st['last_frame_id']} "
                      f"age={st['latency']['capture_to_recv_ms_last']}ms",
                      flush=True)
                last_print = now
            if args.stats_json and now - last_json >= 1.0:
                _write_json_atomic(args.stats_json, st)
                last_json = now
    except KeyboardInterrupt:
        print("[rx] Ctrl+C，停止", flush=True)
    finally:
        rx.stop()
        if args.show:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
        st = rx.stats()
        if args.stats_json:
            _write_json_atomic(args.stats_json, st)
        print(json.dumps(st, ensure_ascii=False), flush=True)
    return 0

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="network 电脑2：TCP 接收 + JPEG 解码 + 最新帧")
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址；0.0.0.0=所有网卡")
    ap.add_argument("--port", type=int, default=proto.DEFAULT_PORT)
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="单帧接收超时秒数")
    ap.add_argument("--max-frame-bytes", type=int,
                    default=proto.MAX_FRAME_BYTES)
    ap.add_argument("--width", type=int, default=0,
                    help="可选：把收到的帧缩放到该宽度（0=保持原样）")
    ap.add_argument("--height", type=int, default=0,
                    help="可选：把收到的帧缩放到该高度（0=保持原样）")
    ap.add_argument("--show", dest="show", action="store_true", default=True)
    ap.add_argument("--no-show", dest="show", action="store_false")
    ap.add_argument("--save-dir", default="",
                    help="保存首帧的目录，例如 samples/network_network")
    ap.add_argument("--save-first", action="store_true",
                    help="保存第一帧 receiver_first.png / _gray.png")
    ap.add_argument("--stats-json", default="",
                    help="可选：每秒写一份接收统计 JSON")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="收到 N 帧后退出；0=一直运行")
    ap.add_argument("--print-every", type=float, default=2.0)
    ap.add_argument("--self-test", action="store_true",
                    help="本机 loopback 自测，不需要摄像头/第二台电脑")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()
    return cmd_serve(args)

if __name__ == "__main__":
    sys.exit(main())
