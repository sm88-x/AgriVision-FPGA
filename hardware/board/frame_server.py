#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_stream A 侧：板端网络帧服务（frame_server）

链路：Camera(PC) → Ethernet → PYNQ PS → AXI DMA → PL V4 → AXI DMA → PS → Ethernet → Qt(PC)

职责（对应 live_stream 任务书 A2）：
    1. 收帧 + 参数 → V4 DMA → 回传输出帧与**板端耗时**；
    2. 协议里**绝不假设一次 recv 拿到整帧**：先循环收满 40 字节头，再按 payload_len
       循环收满；非法长度/尺寸/通道一律回 ERROR 帧；
    3. 单板单主控：DMA 是独占资源，用锁串行化，几路客户端也只有一个在跑；
    4. 逐帧落 `frame_metrics.csv`，汇总与异常落 `board_300frames.log`
       （成功帧/丢帧/超时/DMA 错误、均值中位数最大值），供 300 帧验收直接引用。

用法（板端，与 bit/hwh 同目录）：
    sudo python3 frame_server.py --bitfile configurable_pipeline.bit --port 9000 \
        --out . --log board_300frames.log

不占板子时先在 PC 上跑同一套代码：
    python frame_server.py --mock --port 9000

只跑固定帧数后自动退出（做 300 帧验收最省事）：
    python frame_server.py --mock --frames 300 --out /tmp/live_stream
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
import csv
import json
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

# 板端是"一个平铺目录"，仓库里是 board/ + board/overlay/ + app/python/，两种都要能找到依赖
_HERE = Path(__file__).resolve().parent
for _d in (str(_HERE), str(_HERE / "overlay"), str(_HERE.parent / "src/software/app")):
    if Path(_d).is_dir():
        sys.path.insert(0, _d)

import frame_protocol as fp
from frame_service import FrameError, FrameService

MAX_CONSECUTIVE_ERRORS = 3          # 连续错这么多帧就断开，避免"一直报错还不断"
IDLE_TIMEOUT = 30.0                 # 一条连接空闲这么久就收掉

def recv_exact(sock, nbytes, timeout_hint=None):
    """循环收满 nbytes 字节。**绝不假设一次 recv 就是整帧**（任务书第七节）。"""
    chunks, got = [], 0
    while got < nbytes:
        chunk = sock.recv(min(65536, nbytes - got))
        if not chunk:
            raise fp.ProtocolError(fp.ERR_TIMEOUT,
                                   "连接关闭，只收到 %d/%d 字节" % (got, nbytes))
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)

def send_error(sock, status, message, frame_id=0, width=0, height=0, channels=fp.CH_GRAY8):
    """回一帧 ERROR：同样的 40 字节头 + UTF-8 文本 body，frame_id 回填请求值。"""
    body = str(message).encode("utf-8")
    header = fp.pack_message(fp.MSG_ERROR, frame_id, 0, width, height, channels,
                             payload_len=len(body), status=status)
    sock.sendall(header + body)

class FrameLogger:
    """逐帧 CSV + 汇总日志；300 帧验收直接引用这里的数字。"""

    CSV_FIELDS = ["frame_id", "width", "height", "channels", "mode", "morph_mode",
                  "threshold", "enable_gauss", "preset", "pl_ms", "service_ms",
                  "status", "overlay", "simulated", "note"]

    def __init__(self, out_dir=".", log_name="board_300frames.log",
                 csv_name="frame_metrics.csv", service=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.csv_path = out / csv_name
        self.log_path = out / log_name
        self.service = service
        self.lock = threading.Lock()
        self.counters = {"frames": 0, "ok": 0, "error": 0, "timeout": 0,
                         "dma": 0, "protocol": 0, "bytes_in": 0, "bytes_out": 0}
        self.samples = []
        new_file = not self.csv_path.exists()
        self._csv = open(self.csv_path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._csv, fieldnames=self.CSV_FIELDS)
        if new_file:
            self._writer.writeheader()
        self.note("服务启动：%s" % json.dumps(
            (service.info() if service else {}), ensure_ascii=False, default=str))

    # -------------------------------------------------- 写
    def note(self, text):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n" % (stamp, text))

    def frame(self, head, result=None, status=fp.ERR_OK, note=""):
        row = {"frame_id": head.get("frame_id", 0), "width": head.get("width", 0),
               "height": head.get("height", 0), "channels": head.get("channels", 0),
               "mode": head.get("mode", 0), "morph_mode": head.get("morph_mode", 0),
               "threshold": head.get("threshold", 0),
               "enable_gauss": int(bool(head.get("flags", 0) & fp.FLAG_ENABLE_GAUSS)),
               "preset": int(head.get("pipeline_mode", 0) == fp.PIPELINE_SEED_PRESET),
               "pl_ms": "", "service_ms": "", "status": status,
               "overlay": (result or {}).get("overlay", ""),
               "simulated": (result or {}).get("simulated", ""),
               "note": note}
        if result:
            row["pl_ms"] = result["pl_ms"]
            row["service_ms"] = result["service_ms"]
            row["preset"] = int(bool(result["config"]["preset"]))
        with self.lock:
            self._writer.writerow(row)
            self._csv.flush()
            self.counters["frames"] += 1
            self.counters["bytes_in"] += int(head.get("payload_len", 0))
            if status == fp.ERR_OK and result:
                self.counters["ok"] += 1
                self.counters["bytes_out"] += int(result["width"] * result["height"])
                self.samples.append(result["pl_ms"])
            else:
                self.counters["error"] += 1
                if status == fp.ERR_TIMEOUT:
                    self.counters["timeout"] += 1
                elif status == fp.ERR_DMA:
                    self.counters["dma"] += 1
                elif status in (fp.ERR_BAD_MAGIC, fp.ERR_BAD_VERSION, fp.ERR_BAD_LENGTH,
                                fp.ERR_BAD_SIZE, fp.ERR_BAD_CHANNELS):
                    self.counters["protocol"] += 1

    # -------------------------------------------------- 汇总
    def summary(self):
        data = dict(self.counters)
        if self.samples:
            arr = np.asarray(self.samples)
            data.update({"pl_mean_ms": round(float(arr.mean()), 4),
                         "pl_median_ms": round(float(np.median(arr)), 4),
                         "pl_min_ms": round(float(arr.min()), 4),
                         "pl_max_ms": round(float(arr.max()), 4)})
            if arr.size > 1:
                data["fps_from_pl"] = round(1000.0 / float(arr.mean()), 2)
        return data

    def close(self):
        data = self.summary()
        self.note("服务结束：%s" % json.dumps(data, ensure_ascii=False))
        with self.lock:
            self._csv.close()
        return data

def handle_connection(conn, addr, service, service_lock, logger, verbose=True,
                      stop_event=None, max_frames=0):
    """一条连接上的所有帧；出错回 ERROR 帧，连续出错则断开。"""
    logger.note("客户端接入 %s:%d" % addr)
    frames_here = 0
    consecutive = 0
    conn.settimeout(IDLE_TIMEOUT)
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                header = recv_exact(conn, fp.HEADER_SIZE)
            except fp.ProtocolError:
                break                                     # 对端正常关闭
            except (socket.timeout, OSError):
                logger.note("空闲超时，断开 %s:%d" % addr)
                break
            try:
                head = fp.unpack_message(header)
                want = fp.validate(head, head["payload_len"], fp.MSG_REQUEST)
            except fp.ProtocolError as exc:
                logger.frame({}, status=exc.status, note="协议错误")
                try:
                    send_error(conn, exc.status, str(exc))
                except OSError:
                    pass
                break                                     # 流已经不可信，断开

            try:
                payload = recv_exact(conn, want)
            except fp.ProtocolError as exc:
                logger.frame(head, status=exc.status, note="payload 未收满")
                try:
                    send_error(conn, exc.status, str(exc), head["frame_id"],
                               head["width"], head["height"], head["channels"])
                except OSError:
                    pass
                break

            status, result, message = fp.ERR_OK, None, ""
            with service_lock:                            # DMA 独占，串行化
                try:
                    result = service.process(
                        payload, head["width"], head["height"], head["channels"],
                        mode=head["mode"], morph_mode=head["morph_mode"],
                        threshold=head["threshold"],
                        enable_gauss=bool(head["flags"] & fp.FLAG_ENABLE_GAUSS),
                        pipeline_mode=head["pipeline_mode"], frame_id=head["frame_id"])
                except FrameError as exc:
                    status, message = exc.status, str(exc)
                except Exception as exc:                  # noqa: BLE001 兜底成结构化错误
                    status, message = fp.ERR_INTERNAL, "%s: %s" % (type(exc).__name__, exc)

            try:
                if status == fp.ERR_OK:
                    body = result["output"]
                    conn.sendall(fp.pack_message(
                        fp.MSG_RESPONSE, head["frame_id"], head["timestamp_ms"],
                        head["width"], head["height"], fp.CH_GRAY8,
                        result["config"]["mode"], result["config"]["morph_mode"],
                        head["flags"], head["pipeline_mode"],
                        result["config"]["threshold"], len(body), fp.ERR_OK) + body)
                else:
                    send_error(conn, status, message, head["frame_id"],
                               head["width"], head["height"], head["channels"])
            except OSError:
                logger.frame(head, result, status, "回传失败")
                break

            logger.frame(head, result, status, "" if status == fp.ERR_OK else message)
            frames_here += 1
            consecutive = consecutive + 1 if status != fp.ERR_OK else 0

            if verbose:
                if status == fp.ERR_OK:
                    print("[板端] %s 帧 %-5s %dx%d  %s/%s  gauss=%d  pl=%7.4f ms"
                          % (addr[0], head["frame_id"], head["width"], head["height"],
                             {0: "Sobel", 1: "Prewitt"}[result["config"]["mode"]],
                             {0: "bypass", 1: "erode", 2: "dilate"}[result["config"]["morph_mode"]],
                             result["config"]["enable_gauss"], result["pl_ms"]), flush=True)
                else:
                    print("[板端] %s 帧 %s 失败：%s（%s）"
                          % (addr[0], head["frame_id"],
                             fp.ERROR_NAMES.get(status, status), message), flush=True)
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                logger.note("连续 %d 帧出错，断开 %s:%d" % (consecutive, addr[0], addr[1]))
                break
            if max_frames and logger.counters["frames"] >= max_frames:
                logger.note("已达到目标帧数 %d（含错误帧），停止接收" % max_frames)
                if stop_event:
                    stop_event.set()
                break
    finally:
        try:
            conn.close()
        except OSError:
            pass
        logger.note("客户端断开 %s:%d（本次 %d 帧）" % (addr[0], addr[1], frames_here))
    return frames_here

def serve(host="0.0.0.0", port=9000, bitfile=None, mock=False, out_dir=".",
          log_name="board_300frames.log", csv_name="frame_metrics.csv",
          timeout=5.0, max_frames=0, fail_every=0, verbose=True):
    service = FrameService(bitfile=bitfile, mock=mock, timeout=timeout,
                           fail_every=fail_every, verbose=verbose)
    logger = FrameLogger(out_dir, log_name, csv_name, service)
    info = service.info()
    print("=" * 78)
    print("live_stream 板端帧服务就绪 %s:%d" % (host, port))
    print("Overlay : %s | 核: %s | %s"
          % (info["overlay"], info["kernel"],
             "**软件 Golden 模拟（不是实测性能）**" if info["simulated"] else "真板"))
    print("寄存器 :", ", ".join("%s%s" % (k, "" if v else "(缺)")
                                 for k, v in info["supported"].items()))
    print("日志   : %s / %s" % (logger.csv_path, logger.log_path))
    print("=" * 78, flush=True)

    service_lock = threading.Lock()
    stop_event = threading.Event()
    workers = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(4)
    server.settimeout(0.5)
    started = time.perf_counter()
    try:
        while not stop_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=handle_connection,
                                      args=(conn, addr, service, service_lock, logger,
                                            verbose, stop_event, max_frames),
                                      daemon=True)
            worker.start()
            workers.append(worker)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，收尾中…", flush=True)
    finally:
        stop_event.set()
        try:
            server.close()
        except OSError:
            pass
        # 先把正在处理的那一帧回完再收日志，别把最后一帧的响应截断
        for worker in workers:
            worker.join(timeout=2.0)
        data = logger.close()
        service.close()
    data["wall_seconds"] = round(time.perf_counter() - started, 3)
    print("-" * 78)
    print("帧数 %(frames)d（成功 %(ok)d / 失败 %(error)d；超时 %(timeout)d / "
          "DMA %(dma)d / 协议 %(protocol)d）" % data)
    if "pl_mean_ms" in data:
        print("板端耗时 mean=%(pl_mean_ms).4f median=%(pl_median_ms).4f "
              "min=%(pl_min_ms).4f max=%(pl_max_ms).4f ms" % data)
    print("=" * 78, flush=True)
    return data

def main(argv=None):
    ap = argparse.ArgumentParser(description="live_stream 板端网络帧服务（A 侧）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--bitfile", default="configurable_pipeline.bit",
                    help="默认沿用 pipeline_validation 冻结版本（live_stream 未改硬件）")
    ap.add_argument("--mock", action="store_true", help="软件 Golden 顶替 PL（不占板子）")
    ap.add_argument("--out", default=".", help="日志输出目录")
    ap.add_argument("--log", default="board_300frames.log")
    ap.add_argument("--csv", default="frame_metrics.csv")
    ap.add_argument("--timeout", type=float, default=5.0, help="单帧 DMA 超时（秒）")
    ap.add_argument("--frames", type=int, default=0,
                    help="服务满 N 帧（含错误帧）后自动退出，0=一直跑")
    ap.add_argument("--fail-every", type=int, default=0, help="每 N 帧故意注入一次 DMA 错误")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    serve(args.host, args.port, args.bitfile, args.mock, args.out, args.log, args.csv,
          args.timeout, args.frames, args.fail_every, not args.quiet)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
