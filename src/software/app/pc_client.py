#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_stream PC 侧 TCP 客户端 + Mock 服务端(协议 v1,端口 9000)。

协议单一定义源:同目录 frame_protocol.py;权威文档 hardware/board/protocol.md。
与板端 hardware/board/frame_server.py 共用同一协议模块;MockServer(golden)直接
复用板端 hardware/board/frame_service.py 的 FrameService(mock=True),客户端测过的
协议与板端同一条代码路径。

CLI:
    python pc_client.py mock-server [--port 9000] [--transform identity|golden|corrupt]
    python pc_client.py self-test [--n 100] [--images 5.1.1*.tiff ...]
    python pc_client.py probe --host 192.168.2.99 [--port 9000]   # 上板探测:发 1 小帧
    python pc_client.py golden-check --host 192.168.2.99 IMAGE... # 逐像素对拍(板端或 mock)

统计口径:round_trip_ms = send_ms + recv_ms(PC 侧计时);板端 pl_ms 在板端
CSV(协议响应不含板端耗时,两侧分开记,不混写)。
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
import sys
import threading
import time
from pathlib import Path

import numpy as np

REPO_ROOT = PROJECT_ROOT
for _d in (str(REPO_ROOT / "board"), str(REPO_ROOT / "hardware/drivers"),
           str(REPO_ROOT / "src/software/app")):
    sys.path.insert(0, _d)

import frame_protocol as fp          # noqa: E402

DEFAULT_HOST = "192.168.2.99"
DEFAULT_PORT = 9000

def read_gray_image(path):
    """读灰度图(tiff/png/bmp)返回 uint8 h×w。"""
    import cv2
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise RuntimeError(f"读图失败 {path}")
    return img

def local_golden(gray, mode, morph_mode, threshold, enable_gauss,
                 pipeline_mode=fp.PIPELINE_EXPLICIT):
    """PC 侧软件 Golden,与板端同口径(run_pipeline 参考实现)。

    gray:uint8 h×w;返回 uint8 h×w(板端 out_sel=0 的最终 morph 输出)。
    """
    from run_pipeline import (EDGE_PREWITT, EDGE_SOBEL, SEED_PRESET,
                              pipeline_stages)
    if pipeline_mode == fp.PIPELINE_SEED_PRESET:
        cfg = dict(SEED_PRESET)
    else:
        cfg = {"enable_gray": True,
               "enable_gauss": bool(enable_gauss),
               "edge_mode": EDGE_SOBEL if mode == 0 else EDGE_PREWITT,
               "threshold": int(threshold),
               "morph_mode": int(morph_mode)}
    rgb = np.stack([gray, gray, gray], axis=-1)   # 灰度复刻 3 通道,rgb2gray 恒等
    stages, _ = pipeline_stages(rgb, cfg)
    return stages["output"]

# ------------------------------------------------------------------
# 客户端
# ------------------------------------------------------------------
class FrameClient:
    """单连接锁步帧客户端:发一帧等一帧,响应回来才发下一帧(天然背压)。"""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=5.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock = None
        self.next_frame_id = 0

    def connect(self):
        sock = socket.create_connection((self.host, self.port),
                                        timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        self._sock = sock
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ---- 收发 ----
    def send(self, frame):
        self._sock.sendall(frame)

    def _recv_exact(self, n):
        """循环收满 n 字节;EOF 返回 None(绝不假设一次 recv 收到整帧)。"""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def recv_response(self):
        """收完整响应帧(40B 头 + payload),返回 dict;协议错抛 ProtocolError,
        连接被截断抛 ConnectionError。"""
        header = self._recv_exact(fp.HEADER_SIZE)
        if header is None:
            raise ConnectionError("对端关闭连接(EOF)")
        head = fp.unpack_message(header)
        if head["msg_type"] == fp.MSG_ERROR:
            # 错误帧:仅轻校验(其 width/height 可能为 0),body 为 UTF-8 文本
            if head["magic"] != fp.MAGIC:
                raise fp.ProtocolError(fp.ERR_BAD_MAGIC, "错误帧魔数损坏")
            if head["payload_len"] > fp.MAX_PAYLOAD:
                raise fp.ProtocolError(fp.ERR_BAD_LENGTH, "错误帧 body 超上限")
            body = self._recv_exact(head["payload_len"])
            if body is None:
                raise ConnectionError("错误帧 body 被截断")
            return {**head, "payload": body,
                    "error_text": body.decode("utf-8", errors="replace")}
        want = fp.validate(head, head["payload_len"], fp.MSG_RESPONSE)
        body = self._recv_exact(want)
        if body is None:
            raise ConnectionError(f"响应 payload 被截断(期望 {want}B)")
        return {**head, "payload": body, "error_text": None}

    # ---- 事务 ----
    def transact(self, header, payload, expect_frame_id=None, retries=1):
        """发送 40B 头 + payload 并收响应;网络层超时/断连重试 retries 次;
        ERR_DMA 响应不重试。frame_id 回显不符视为陈旧响应(协议错)。"""
        last = None
        for _ in range(retries + 1):
            try:
                self.send(header + payload)
                resp = self.recv_response()
            except (socket.timeout, ConnectionError, OSError) as exc:
                last = exc
                continue
            if expect_frame_id is not None \
                    and resp["frame_id"] != expect_frame_id:
                raise fp.ProtocolError(
                    fp.ERR_BAD_LENGTH,
                    f"frame_id 回显不符:期望 {expect_frame_id},"
                    f"得到 {resp['frame_id']}(疑似陈旧响应)")
            return resp
        raise TimeoutError(f"transact 超时(重试 {retries} 次后):{last}")

    # ---- 高层接口 ----
    def send_gray(self, gray, frame_id=None, mode=0, morph_mode=0, threshold=0,
                  enable_gauss=False, pipeline_mode=fp.PIPELINE_EXPLICIT):
        """发一帧灰度图(channels=1),返回 (resp_dict, round_trip_ms,
        send_ms, recv_ms)。"""
        h, w = gray.shape
        if frame_id is None:
            frame_id = self.next_frame_id
            self.next_frame_id += 1
        ts_ms = time.monotonic_ns() // 1_000_000   # 发送端单调时钟毫秒
        flags = fp.FLAG_ENABLE_GRAY | (fp.FLAG_ENABLE_GAUSS if enable_gauss
                                       else 0)
        payload = np.ascontiguousarray(gray, dtype=np.uint8).tobytes()
        header = fp.pack_message(fp.MSG_REQUEST, frame_id, ts_ms, w, h,
                                 fp.CH_GRAY8, mode, morph_mode, flags,
                                 pipeline_mode, threshold,
                                 payload_len=len(payload), status=fp.ERR_OK)
        frame = header + payload
        last = None
        for _ in range(2):              # 网络层超时重试 1 次(同一帧)
            t0 = time.perf_counter()
            try:
                self.send(frame)
                t1 = time.perf_counter()
                resp = self.recv_response()
                t2 = time.perf_counter()
            except (socket.timeout, ConnectionError, OSError) as exc:
                last = exc
                continue
            if resp["frame_id"] != frame_id:
                raise fp.ProtocolError(
                    fp.ERR_BAD_LENGTH,
                    f"frame_id 回显不符:期望 {frame_id},得到 {resp['frame_id']}")
            send_ms = round((t1 - t0) * 1e3, 3)
            recv_ms = round((t2 - t1) * 1e3, 3)
            return resp, round(send_ms + recv_ms, 3), send_ms, recv_ms
        raise TimeoutError(f"send_gray 超时(重试 1 次后):{last}")

    def probe(self):
        """上板探测:发一帧 16×16 灰图,返回 (ok, resp)。"""
        gray = np.zeros((16, 16), dtype=np.uint8)
        resp, rtt, _, _ = self.send_gray(gray, frame_id=0)
        return resp["status"] == fp.ERR_OK, resp, rtt

# ------------------------------------------------------------------
# Mock 服务端
# ------------------------------------------------------------------
class MockServer:
    """线程化 TCP mock。transform:
    identity=逐字节回传;golden=复用板端 FrameService(mock=True) 软件处理;
    corrupt=按帧轮转 4 种注入(截断/payload_len 虚增/坏魔数/坏版本)。
    """

    def __init__(self, host="127.0.0.1", port=0, transform="identity"):
        self.transform = transform
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._thread = None
        self._stop = threading.Event()
        self.frames_served = 0
        self.corrupt_seq = 0
        self._service = None

    @property
    def port(self):
        return self._sock.getsockname()[1]

    def start(self):
        self._sock.listen(4)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    @staticmethod
    def _recv_exact(conn, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _handle(self, conn):
        conn.settimeout(30)
        try:
            while True:
                header = self._recv_exact(conn, fp.HEADER_SIZE)
                if header is None:
                    return
                try:
                    head = fp.unpack_message(header)
                    want = fp.validate(head, head["payload_len"],
                                       fp.MSG_REQUEST)
                except fp.ProtocolError as exc:
                    self._send_error(conn, exc.status, str(exc),
                                     head.get("frame_id", 0) if head else 0)
                    return                      # 流已不可信,断开
                payload = self._recv_exact(conn, want)
                if payload is None:
                    return
                self._respond(conn, head, payload)
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send_error(conn, status, message, frame_id):
        body = str(message).encode("utf-8")
        header = fp.pack_message(fp.MSG_ERROR, frame_id, 0, 0, 0, fp.CH_GRAY8,
                                 payload_len=len(body), status=status)
        conn.sendall(header + body)

    def _respond(self, conn, head, payload):
        self.frames_served += 1
        if self.transform == "corrupt":
            self._respond_corrupt(conn, head, payload)
            return
        gray = np.frombuffer(payload, dtype=np.uint8) \
            .reshape(head["height"], head["width"])
        if self.transform == "golden":
            if self._service is None:
                from frame_service import FrameService
                self._service = FrameService(mock=True)
            enable_gauss = bool(head["flags"] & fp.FLAG_ENABLE_GAUSS)
            result = self._service.process(
                payload, head["width"], head["height"], fp.CH_GRAY8,
                mode=head["mode"], morph_mode=head["morph_mode"],
                threshold=head["threshold"], enable_gauss=enable_gauss,
                pipeline_mode=head["pipeline_mode"], frame_id=head["frame_id"])
            body = result["output"]
            cfg = result["config"]
        else:                                   # identity:逐字节回传
            body = gray.tobytes()
            cfg = {"mode": head["mode"], "morph_mode": head["morph_mode"],
                   "threshold": head["threshold"]}
        header = fp.pack_message(
            fp.MSG_RESPONSE, head["frame_id"], head["timestamp_ms"],
            head["width"], head["height"], fp.CH_GRAY8, cfg["mode"],
            cfg["morph_mode"], head["flags"], head["pipeline_mode"],
            cfg["threshold"], len(body), fp.ERR_OK)
        conn.sendall(header + body)

    def _respond_corrupt(self, conn, head, payload):
        pattern = self.corrupt_seq % 4
        self.corrupt_seq += 1
        header = fp.pack_message(
            fp.MSG_RESPONSE, head["frame_id"], head["timestamp_ms"],
            head["width"], head["height"], fp.CH_GRAY8, head["mode"],
            head["morph_mode"], head["flags"], head["pipeline_mode"],
            head["threshold"], len(payload), fp.ERR_OK)
        if pattern == 0:        # 截断:只发 20 字节头后断连
            conn.sendall(header[:20])
        elif pattern == 1:      # payload_len 虚增:声明多 100B,发完正常 body 断连
            bad = bytearray(header)
            struct.pack_into("<I", bad, 32, len(payload) + 100)
            conn.sendall(bytes(bad) + payload)
        elif pattern == 2:      # 坏魔数
            conn.sendall(b"XXXX" + header[4:] + payload)
        else:                   # 坏版本(version=2)
            bad = bytearray(header)
            struct.pack_into("<H", bad, 4, 2)
            conn.sendall(bytes(bad) + payload)

# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def cmd_mock_server(args):
    server = MockServer(host=args.host, port=args.port,
                        transform=args.transform)
    port = server.start()
    print(f"mock-server 监听 {args.host}:{port} transform={args.transform}",
          flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()

def _fresh_client(server, timeout=1.0):
    return FrameClient(host="127.0.0.1", port=server.port,
                       timeout=timeout).connect()

def cmd_self_test(args):
    rng = np.random.RandomState(0)

    # ---- 1) identity:100 帧逐字节回传一致 + frame_id 回显 + 0 超时 ----
    server = MockServer(port=0, transform="identity")
    server.start()
    client = _fresh_client(server)
    try:
        for i in range(args.n):
            w, h = 64, 48
            gray = rng.randint(0, 256, size=(h, w), dtype=np.uint8)
            resp, rtt, send_ms, recv_ms = client.send_gray(
                gray, frame_id=i, mode=i % 2, morph_mode=i % 3,
                threshold=80 if i % 2 else 0, enable_gauss=bool(i % 2),
                pipeline_mode=i % 2)
            got = np.frombuffer(resp["payload"], dtype=np.uint8).reshape(h, w)
            assert resp["frame_id"] == i, f"frame_id 回显错 {resp['frame_id']}"
            assert resp["status"] == fp.ERR_OK, f"status={resp['status']}"
            assert np.array_equal(got, gray), f"第 {i} 帧回传不一致"
            assert rtt >= 0 and send_ms >= 0 and recv_ms >= 0
        # 坏请求 → 服务端回 ERROR 帧并断开,不崩
        client2 = _fresh_client(server)
        try:
            resp = client2.transact(b"XXXX" + b"\x00" * 36, b"", retries=0)
            assert resp["msg_type"] == fp.MSG_ERROR, "坏魔数未回 ERROR 帧"
            assert resp["status"] == fp.ERR_BAD_MAGIC, \
                f"status={resp['status']} 非 BAD_MAGIC"
        finally:
            client2.close()
        # 服务端仍活着
        client3 = _fresh_client(server)
        ok, _, _ = client3.probe()
        assert ok, "坏请求后服务端挂了"
        client3.close()
        print(f"identity {args.n} 帧 PASS:0 失配 0 超时")
    finally:
        client.close()
        server.stop()

    # ---- 2) corrupt:4 种注入全部被拒绝,客户端不崩 ----
    server = MockServer(port=0, transform="corrupt")
    server.start()
    refused = [0, 0, 0, 0]
    for pattern in range(4):
        client = _fresh_client(server)
        try:
            try:
                client.send_gray(np.zeros((48, 64), dtype=np.uint8),
                                 frame_id=pattern)
                raise AssertionError(f"注入 {pattern} 未被拒绝")
            except (fp.ProtocolError, TimeoutError, ConnectionError, OSError):
                refused[pattern] = 1
        finally:
            client.close()
    assert all(refused), f"corrupt 拒绝统计异常:{refused}"
    server.stop()
    print("corrupt 4 模式 PASS:截断/payload_len 虚增/坏魔数/坏版本 全部拒绝,无崩溃")

    # ---- 3) golden:板端 FrameService(mock) 服务端 vs 本地 golden,0 像素差 ----
    images = args.images or sorted(
        str(p) for p in REPO_ROOT.glob("test/images/5.1.1*.tiff"))
    if images:
        server = MockServer(port=0, transform="golden")
        server.start()
        client = _fresh_client(server)
        try:
            for path in images:
                gray = read_gray_image(path)
                h, w = gray.shape
                resp, _, _, _ = client.send_gray(
                    gray, mode=0, morph_mode=1, threshold=80,
                    enable_gauss=True, pipeline_mode=fp.PIPELINE_EXPLICIT)
                expect = local_golden(gray, 0, 1, 80, True,
                                      fp.PIPELINE_EXPLICIT)
                got = np.frombuffer(resp["payload"], dtype=np.uint8) \
                    .reshape(h, w)
                diff = int(np.count_nonzero(got != expect))
                assert diff == 0, f"{path}:{diff} 像素差"
                print(f"golden {Path(path).name}:0 像素差")
        finally:
            client.close()
            server.stop()
        print(f"golden 对拍 PASS:{len(images)} 张图 0 失配")
    print("self-test ALL PASS")
    return 0

def cmd_probe(args):
    client = FrameClient(args.host, args.port, timeout=args.timeout)
    try:
        client.connect()
        ok, resp, rtt = client.probe()
        print(f"PROBE {args.host}:{args.port} → "
              f"{'OK' if ok else 'status=' + str(resp['status'])} "
              f"rtt={rtt:.1f}ms")
        return 0 if ok else 1
    finally:
        client.close()

def cmd_golden_check(args):
    client = FrameClient(args.host, args.port, timeout=args.timeout)
    try:
        client.connect()
        for path in args.images:
            gray = read_gray_image(path)
            h, w = gray.shape
            resp, _, _, _ = client.send_gray(
                gray, mode=args.mode, morph_mode=args.morph,
                threshold=args.threshold, enable_gauss=args.enable_gauss,
                pipeline_mode=args.pipeline_mode)
            if resp["status"] != fp.ERR_OK:
                print(f"{path}: status={fp.ERROR_NAMES.get(resp['status'])}"
                      f" ({resp.get('error_text', '')})")
                return 1
            got = np.frombuffer(resp["payload"], dtype=np.uint8).reshape(h, w)
            expect = local_golden(gray, args.mode, args.morph, args.threshold,
                                  args.enable_gauss, args.pipeline_mode)
            diff = int(np.count_nonzero(got != expect))
            print(f"{path}:{'0 像素差' if diff == 0 else f'{diff} 像素差'}")
            if diff:
                return 1
        return 0
    finally:
        client.close()

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="live_stream TCP 客户端/mock")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_srv = sub.add_parser("mock-server")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_srv.add_argument("--transform", default="identity",
                       choices=("identity", "golden", "corrupt"))
    p_srv.set_defaults(func=cmd_mock_server)

    p_test = sub.add_parser("self-test")
    p_test.add_argument("--n", type=int, default=100)
    p_test.add_argument("--images", nargs="*")
    p_test.set_defaults(func=cmd_self_test)

    p_probe = sub.add_parser("probe", help="上板探测:发一帧 16×16 灰图")
    p_probe.add_argument("--host", default=DEFAULT_HOST)
    p_probe.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_probe.add_argument("--timeout", type=float, default=5.0)
    p_probe.set_defaults(func=cmd_probe)

    p_g = sub.add_parser("golden-check", help="逐像素对拍(板端或 mock)")
    p_g.add_argument("--host", default="127.0.0.1")
    p_g.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_g.add_argument("--timeout", type=float, default=5.0)
    p_g.add_argument("--mode", type=int, default=0)
    p_g.add_argument("--morph", type=int, default=1)
    p_g.add_argument("--threshold", type=int, default=80)
    p_g.add_argument("--enable-gauss", type=int, default=1)
    p_g.add_argument("--pipeline-mode", type=int, default=0)
    p_g.add_argument("images", nargs="+")
    p_g.set_defaults(func=cmd_golden_check)

    args = parser.parse_args()
    return args.func(args)

if __name__ == "__main__":
    sys.exit(main())
