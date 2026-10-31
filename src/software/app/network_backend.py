#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""network C：网络摄像头后端（PC2 收 PC1 的 JPEG 帧 → 检测 → live_status.json）。

协议（与 B 的 capture_sender.py 共用 timestamped_frame_protocol.py）：
    4 字节 body_len + 20 字节 AGF1 头（帧号/采集时间/宽高）+ JPEG。
    接收端循环 recv 收满长度和 body，处理分包/粘包。

**最新帧优先**：接收线程只把"最新一帧"放进槽位，处理线程取最新帧；
PC1 发得比检测快时旧帧直接丢弃并计入 `network.dropped`，
不会在 TCP 缓冲里无限排队（任务书硬性要求）。

与现有 Qt 的契约沿用 detection 适配器：
    入：<runDir>/request.json（mode/source=net/width/height/frames/detect/conf/detector/net_port…）
    出：<runDir>/live_status.json + <runDir>/preview/{cam,board,annot}_latest.png
    停：Qt 写 <runDir>/stop.request 后结束进程

source=net 以外的请求原样转交给 `src/software/adapters/detection_adapter.py`。
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
import copy
import csv
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import timestamped_frame_protocol as netproto

REPO = PROJECT_ROOT
ADAPTER_DIR = REPO / "src/software/adapters"
sys.path.insert(0, str(ADAPTER_DIR))
sys.path.insert(0, str(REPO / "algorithm"))
sys.path.insert(0, str(REPO / "src/software/algorithm/adaptive"))
import detection_adapter as adapter          # noqa: E402

MAX_FRAME_BYTES = netproto.MAX_FRAME_BYTES

def recv_exact(sock, need):
    """收满 need 字节；对端关闭返回 None。"""
    chunks, got = [], 0
    while got < need:
        part = sock.recv(min(65536, need - got))
        if not part:
            return None
        chunks.append(part)
        got += len(part)
    return b"".join(chunks)

def make_server(port):
    """先开监听端口再加载模型：PC1 可以立刻连上排队，不会被拒。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(4)
    server.settimeout(0.5)
    return server

def write_png_atomic(path, image):
    """先写临时文件再替换，避免 Qt 轮询时读到半张 PNG。"""
    path = Path(path)
    tmp = path.with_name(path.stem + ".tmp" + path.suffix)
    if not cv2.imwrite(str(tmp), image):
        raise OSError("无法写入预览图 %s" % tmp)
    # Windows 上 Qt 可能正打开旧 PNG 读数秒；此时 os.replace 会短暂报
    # PermissionError/WinError 5。重试一小段时间，避免后端因预览图竞争退出。
    for attempt in range(20):
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)

class LiveComparison:
    """复用网络后端已完成的 YOLO 检测，周期生成三路粘连对比。"""

    SPECIES_ALIAS = {"other": "soybean", "peanut": "soybean", "pea": "mungbean"}

    def __init__(self, request, out_dir):
        import adaptive_split_v2 as avatar
        import comparison_runtime as runtime

        self.avatar = avatar
        self.runtime = runtime
        self.out_dir = Path(out_dir)
        self.analysis_dir = self.out_dir / "analysis"
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self.comparison_path = self.analysis_dir / "comparison.json"
        self.phenotyping_path = self.analysis_dir / "phenotyping.json"
        self.species = str(request.get("species", "other"))
        self.prior_species = self.SPECIES_ALIAS.get(self.species, self.species)
        if self.prior_species not in ("soybean", "mungbean", "redbean"):
            self.prior_species = "soybean"
        self.interval_s = max(0.25, float(request.get("comparison_interval", 1.0)))
        frozen = json.loads((REPO / "data/evidence/segmentation_comparison/frozen.json")
                            .read_text(encoding="utf-8"))
        self.engine = object.__new__(avatar.SeedSeparatorV2)
        self.engine.frozen = copy.deepcopy(frozen)
        self.detector_info = {
            "name": "YOLO11n best.pt",
            "conf": float(request.get("conf", 0.25)),
            "iou": float(frozen["detector"]["iou"]),
            "imgsz": int(frozen["detector"]["imgsz"]),
            "device": str(frozen["detector"]["device"]),
        }
        self.last_run = 0.0
        self.last_frame = -1
        self.last_error = ""
        self.counts = None

    def maybe_run(self, bgr, detections, infer_ms, frame_id):
        now = time.monotonic()
        if self.last_frame == frame_id or now - self.last_run < self.interval_s:
            return None
        self.last_run = now
        self.last_frame = frame_id
        try:
            dets = []
            for i, d in enumerate(detections):
                item = dict(d)
                item.setdefault("id", i)
                item.setdefault("class_name", "seed")
                dets.append(item)
            prediction = {
                "detections": dets,
                "raw_detections": dets,
                "effective_parameters": {
                    "conf": self.detector_info["conf"],
                    "iou": self.detector_info["iou"],
                    "imgsz": self.detector_info["imgsz"],
                    "device": self.detector_info["device"],
                },
                "yolo_infer_ms": float(infer_ms),
                "detector_wall_ms": float(infer_ms),
                "species": self.prior_species,
            }
            kwargs = {
                "session_id": "live_network",
                "frame_id": frame_id,
                "image_id": "live_%06d" % frame_id,
                "gt_count": None,
                "prediction": prediction,
            }
            yolo = self.engine.detect_and_separate(
                bgr, "baseline_0", self.prior_species, **kwargs)
            fixed = self.engine.detect_and_separate(
                bgr, "baseline_1", self.prior_species, **kwargs)
            adaptive = self.engine.detect_and_separate(
                bgr, "proposed_v2", self.prior_species, **kwargs)
            modes = {
                "yolo": self.runtime.mode_for_ui(yolo),
                "fixed": self.runtime.mode_for_ui(fixed),
                "adaptive": self.runtime.mode_for_ui(adaptive),
            }
            timing = {
                "yolo": yolo["timings_ms"].get("yolo_infer_ms"),
                "fixed": fixed["timings_ms"].get("separation_ms"),
                "adaptive": adaptive["timings_ms"].get("separation_ms"),
                "total": adaptive["timings_ms"].get("total_ms"),
                "total_yolo": yolo["timings_ms"].get("total_ms"),
                "total_fixed": fixed["timings_ms"].get("total_ms"),
                "total_adaptive": adaptive["timings_ms"].get("total_ms"),
            }
            image_path = self.out_dir / "preview/cam_latest.png"
            comparison = {
                "schema": "agrivision.segmentation.comparison.v1",
                "source": "live_network_runtime",
                "source_note": "同一网络帧运行 YOLO、固定分水岭和自适应分离",
                "mode_labels": {
                    "yolo": "YOLO 检测框",
                    "fixed": "固定分水岭",
                    "adaptive": "自适应分离",
                },
                "detector": self.detector_info,
                "images": [{
                    "image_id": "live_%06d" % frame_id,
                    "file": str(image_path),
                    "species": self.species,
                    "contact_type": "",
                    "split": "live",
                    "notes": "network frame %d" % frame_id,
                    "fail_note": "",
                    "gt_source": "",
                    "status": "pass",
                    "gt_count": None,
                    "timings_ms": timing,
                    "modes": modes,
                }],
            }
            adapter.atomic_write_json(self.comparison_path, comparison)
            pheno = self.runtime.phenotyping_document(
                frame_id, image_path, self.species, adaptive["final_count"],
                adaptive.get("objects", []), self.detector_info)
            adapter.atomic_write_json(self.phenotyping_path, pheno)
            self.counts = {k: modes[k]["count"] for k in ("yolo", "fixed", "adaptive")}
            self.last_error = ""
            return self.counts
        except Exception as exc:                  # noqa: BLE001
            self.last_error = str(exc)
            print("[live comparison] %s" % exc, flush=True)
            return None

class LatestFrameReader(threading.Thread):
    """后台收帧线程：只保留最新一帧，旧帧覆盖丢弃。"""

    def __init__(self, server):
        super().__init__(daemon=True)
        self.server = server
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.latest = None
        self.seq = 0
        self.received = 0
        self.bytes_total = 0
        self.connected = False
        self.peer = ""
        self.reconnects = 0
        self.last_error = ""
        self.last_recv_ts = 0.0
        self.fps = 0.0

    def run(self):
        conn = None
        while not self.stop_evt.is_set():
            if conn is None:
                try:
                    conn, address = self.server.accept()
                    conn.settimeout(5.0)
                    with self.lock:
                        if self.received:
                            self.reconnects += 1
                        self.connected = True
                        self.peer = "%s:%d" % (address[0], address[1])
                        self.last_error = ""
                except socket.timeout:
                    with self.lock:
                        self.connected = False
                    continue
            try:
                prefix = recv_exact(conn, netproto.LENGTH_SIZE)
                if prefix is None:
                    raise ConnectionError("对端关闭连接")
                body_len = netproto.validate_body_len(
                    netproto.LENGTH.unpack(prefix)[0])
                body = recv_exact(conn, body_len)
                if body is None:
                    raise ConnectionError("帧未收完对端就断开")
                head = netproto.parse_header(body)
                payload = body[netproto.HEADER_SIZE:]
            except (socket.timeout, ConnectionError, ValueError,
                    netproto.FrameProtocolError, OSError) as exc:
                with self.lock:
                    self.connected = False
                    self.last_error = str(exc)
                try:
                    conn.close()
                except OSError:
                    pass
                conn = None
                continue

            now = time.time()
            with self.lock:
                self.latest = (payload, head)  # ← 覆盖旧帧：最新帧优先
                self.seq += 1
                self.received += 1
                self.bytes_total += len(payload)
                if self.last_recv_ts:
                    gap = max(1e-6, now - self.last_recv_ts)
                    self.fps = 0.8 * self.fps + 0.2 * (1.0 / gap) if self.fps else 1.0 / gap
                self.last_recv_ts = now
        try:
            if conn:
                conn.close()
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return {"seq": self.seq, "latest": self.latest, "received": self.received,
                    "connected": self.connected, "peer": self.peer,
                    "reconnects": self.reconnects, "last_error": self.last_error,
                    "fps": self.fps, "bytes_total": self.bytes_total,
                    "sender_frame_id": (self.latest[1]["frame_id"]
                                       if self.latest else None),
                    "capture_ts_ns": (self.latest[1]["capture_ts_ns"]
                                     if self.latest else None)}

def run_net_source(request, detector, out_dir, server):
    width = int(request.get("width", 640))
    height = int(request.get("height", 480))
    frames_limit = int(request.get("frames", 0)) or 10 ** 9
    enabled = bool(request.get("detect", True))
    port = int(request.get("net_port", 9100))
    live_cmp = None
    if enabled and bool(request.get("live_compare", False)):
        try:
            live_cmp = LiveComparison(request, out_dir)
            print("[network net backend] 实时粘连对比已启用", flush=True)
        except Exception as exc:              # noqa: BLE001
            print("[network net backend] 实时粘连对比启动失败：%s" % exc,
                  flush=True)
    stop_file = out_dir / "stop.request"
    preview = out_dir / "preview"
    preview.mkdir(parents=True, exist_ok=True)
    frames_csv = out_dir / "network_frames.csv"

    reader = LatestFrameReader(server)
    reader.start()
    started = time.time()
    processed = 0
    last_seq = 0
    last_status_ts = 0.0
    fields = ["frame_id", "sender_frame_id", "recv_ts", "bytes",
              "capture_ts_ns", "capture_age_ms", "decode_ms", "detect_ms",
              "seed_count", "fps", "dropped"]

    with frames_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()

        while not stop_file.exists():
            snap = reader.snapshot()
            # 收够请求帧数、且最新一帧已经处理完 → 正常结束。
            # 注意：判定用"收到"，不是"处理"——丢旧帧后 processed 会小于 frames。
            if snap["received"] >= frames_limit and snap["seq"] == last_seq:
                break
            if snap["seq"] == last_seq:
                # 还没有新帧：低频刷新监听/断开状态，不刷屏
                if time.time() - last_status_ts > 0.5:
                    adapter.atomic_write_json(out_dir / "live_status.json", {
                        "running": True,
                        "state": "LIVE" if snap["received"] else
                                 ("DISCONNECTED" if snap["last_error"] else "LISTENING"),
                        "mode": "Live", "source": "net", "frame": processed,
                        "network": {"listen_port": port, "connected": snap["connected"],
                                    "peer": snap["peer"], "frames_received": snap["received"],
                                    "processed": processed,
                                    "dropped": max(0, snap["received"] - processed),
                                    "reconnects": snap["reconnects"],
                                    "last_error": snap["last_error"]},
                    })
                    last_status_ts = time.time()
                time.sleep(0.005)
                continue

            payload, sender_head = snap["latest"]
            sender_frame_id = int(sender_head["frame_id"])
            capture_ts_ns = int(sender_head["capture_ts_ns"])
            last_seq = snap["seq"]
            dropped = max(0, snap["received"] - processed - 1)
            now = time.time()
            capture_age_ms = max(
                0.0, time.time_ns() * 1e-6 - capture_ts_ns * 1e-6)
            t_decode = time.perf_counter()
            image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            decode_ms = (time.perf_counter() - t_decode) * 1e3
            if image is None:
                continue

            bgr = adapter.normalize_bgr(image, width, height)
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            t0 = time.perf_counter()
            dets = detector.detect_seeds(bgr, frame_id=processed) if enabled else []
            detect_ms = (time.perf_counter() - t0) * 1e3
            annot = adapter.annotate(bgr, dets)
            write_png_atomic(preview / "cam_latest.png", bgr)
            write_png_atomic(preview / "board_latest.png", gray)
            write_png_atomic(preview / "annot_latest.png", annot)
            total_ms = decode_ms + detect_ms
            cmp_counts = (live_cmp.maybe_run(bgr, dets, detect_ms, processed)
                          if live_cmp is not None else None)

            adapter.atomic_write_json(out_dir / "live_status.json", {
                "running": True, "state": "LIVE", "mode": "Live",
                "source": "net", "frame": processed,
                "camera_fps": round(snap["fps"], 1),
                "live_fps": round(snap["fps"], 1),
                "latency": {"rtt_ms": "", "pl_ms": "",
                            "capture_age_ms": (round(capture_age_ms, 3)
                                               if capture_age_ms is not None else None),
                            "net_ms": round(total_ms, 3),
                            "full_ms": round(
                                (capture_age_ms or 0.0) + total_ms, 3)},
                "preview": {"cam": str(preview / "cam_latest.png"),
                            "board": str(preview / "board_latest.png"),
                            "annot": str(preview / "annot_latest.png")},
                "ai": {"enabled": enabled, "model_state": detector.state,
                       "seed_count": len(dets), "infer_ms": round(detect_ms, 3),
                       "input": "bgr", "source": "network-frame",
                       "detections": dets,
                       "annot": str(preview / "annot_latest.png")},
                "live_comparison": ({
                    "enabled": True,
                    "path": str(live_cmp.comparison_path),
                    "phenotyping_path": str(live_cmp.phenotyping_path),
                    "frame_id": processed,
                    "counts": cmp_counts,
                    "species": live_cmp.species,
                    "interval_s": live_cmp.interval_s,
                    "last_error": live_cmp.last_error,
                } if live_cmp is not None else {"enabled": False}),
                "network": {"listen_port": port, "connected": snap["connected"],
                            "peer": snap["peer"],
                            "protocol": "JPEG AGF1 v1",
                            "frame_bytes": len(payload),
                            "sender_frame_id": sender_frame_id,
                            "capture_ts_ns": capture_ts_ns,
                            "capture_age_ms": (round(capture_age_ms, 3)
                                               if capture_age_ms is not None else None),
                            "frames_received": snap["received"],
                            "processed": processed + 1,
                            "dropped": dropped,
                            "fps": round(snap["fps"], 1),
                            "reconnects": snap["reconnects"],
                            "last_error": snap["last_error"],
                            "elapsed_s": round(now - started, 1)},
            })
            writer.writerow({"frame_id": processed,
                             "sender_frame_id": sender_frame_id,
                             "recv_ts": "%.3f" % now,
                             "bytes": len(payload),
                             "capture_ts_ns": capture_ts_ns,
                             "capture_age_ms": (round(capture_age_ms, 3)
                                                if capture_age_ms is not None else ""),
                             "decode_ms": round(decode_ms, 3),
                             "detect_ms": round(detect_ms, 3), "seed_count": len(dets),
                             "fps": round(snap["fps"], 1), "dropped": dropped})
            fh.flush()
            processed += 1
            last_status_ts = time.time()

    reader.stop_evt.set()
    reader.join(timeout=2.0)
    try:
        server.close()
    except OSError:
        pass

    snap = reader.snapshot()
    elapsed = max(1e-6, time.time() - started)
    adapter.atomic_write_json(out_dir / "live_status.json", {
        "running": False, "state": "STOPPED" if stop_file.exists() else "FINISHED",
        "mode": "Live", "source": "net", "frame": processed,
        "network": {"listen_port": port, "connected": False, "peer": snap["peer"],
                    "frames_received": snap["received"], "processed": processed,
                    "dropped": max(0, snap["received"] - processed),
                    "reconnects": snap["reconnects"], "last_error": snap["last_error"],
                    "elapsed_s": round(elapsed, 1)},
    })
    return {"ok": True, "mode": "Live", "source": "net", "frames": processed,
            "received": snap["received"], "dropped": max(0, snap["received"] - processed),
            "reconnects": snap["reconnects"], "avg_fps": round(processed / elapsed, 2),
            "live_comparison": ({"enabled": True,
                                  "counts": live_cmp.counts,
                                  "last_error": live_cmp.last_error}
                                 if live_cmp is not None else {"enabled": False}),
            "output_dir": str(out_dir)}

def main():
    ap = argparse.ArgumentParser(description="network 网络摄像头后端")
    ap.add_argument("--request", required=True)
    args = ap.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
    request["mode"] = str(request.get("mode", "live")).strip().lower()
    source = str(request.get("source", "board"))
    out_dir = Path(request["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    if source != "net":
        # 非网络源：原样转交 detection 适配器，保持既有行为
        adapter_script = ADAPTER_DIR / "detection_adapter.py"
        return subprocess.call([sys.executable, str(adapter_script), "--request", args.request])

    port = int(request.get("net_port", 9100))
    server = make_server(port)
    adapter.atomic_write_json(out_dir / "live_status.json", {
        "running": True, "state": "LISTENING", "mode": "Live", "source": "net",
        "network": {"listen_port": port, "connected": False, "peer": "",
                    "frames_received": 0, "processed": 0, "dropped": 0,
                    "reconnects": 0, "last_error": ""},
    })
    detector, note = adapter.build_detector(request)
    # 预热：真模型第一次推理要 1~3 秒（torch 初始化 + 首帧图优化），
    # 不预热的话演示一开始就会因为"处理慢"把前几十帧全丢掉。
    t_warm = time.perf_counter()
    try:
        width = int(request.get("width", 640))
        height = int(request.get("height", 480))
        detector.detect_seeds(np.zeros((height, width, 3), np.uint8), frame_id=-1)
        print("[network net backend] 检测器预热 %.0f ms"
              % ((time.perf_counter() - t_warm) * 1e3), flush=True)
    except Exception as exc:                      # noqa: BLE001 预热失败不阻塞
        print("[network net backend] 预热失败（忽略）：%s" % exc, flush=True)
    print("[network net backend] 端口 %s | %s | %s" % (port, detector.state, note), flush=True)
    result = run_net_source(request, detector, out_dir, server)
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
