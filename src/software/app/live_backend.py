#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_stream PC 侧后端:QProcess 兼容 CLI,提供 Image/Batch/Live 三入口(协议 v1)。

调用方式:`python live_backend.py --request <json 文件>`
→ stdout 仅一行 JSON;诊断信息走 stderr;异常输出 `{ok:false,error}` 并 exit 1。

协议:hardware/board/protocol.md——TCP 9000、40B 头 + payload、
channels=1 灰度、pipeline_mode 0=显式/1=Seed Preset。本文件只 import
frame_protocol.py / pc_client.py,不另抄常量。

请求 JSON:
{
  "schema_version": 1,
  "mode": "image" | "batch" | "live",
  "image_path": "...",            # image 模式
  "input_dir": "...",             # batch 模式(扫描 tiff/png/bmp/bin)
  "output_dir": "...",            # 可选,默认 app/python/live_stream_output
  "host": "192.168.2.99",         # 板端
  "port": 9000,
  "source": "board",              # live: board=本地摄像头 / network=电脑1 网络帧
  "camera_index": 0,              # live 模式
  "camera_src": "video.mp4",      # live 模式:显式视频源(降级回放)
  "network_listen_host": "0.0.0.0",  # source=network 时监听地址
  "network_listen_port": 9100,       # 电脑1→电脑2 端口(不是板端 9000)
  "network_start_timeout_s": 60,     # 等电脑1 第一帧的秒数
  "network_idle_timeout_s": 10,      # 运行中无帧多少秒后结束
  "width": 640, "height": 480,    # live 模式采集尺寸
  "preset": "sobel_thr80_erode",  # 与 pipeline{} 二选一,preset 优先
  "pipeline": {"mode":0,"threshold":80,"morph":1,"en_gauss":1},
  "frames": 300,                  # live 模式帧数,null=直到被终止
  "golden_audit": true,           # image/batch/live 是否做 golden 对拍
  "ai_detection": false,          # image/live:YOLO 种子检测(原图彩色帧)
  "ai_conf": 0.25                 # 检测置信度阈值(ai_detection=true 时生效)
}

统计口径分离(三数永不混用):
- camera_fps = 相机采集侧实测
- round_trip_ms = PC 网络往返(PC 单调钟计时;板端 pl_ms 在板端日志,不在响应里)
- full_ms = 采集时刻(单调钟)→ 收到响应(全链路)
Live 期间每帧写 live_stream_output/live_status.json 供 Qt 轮询。

AI 检测(detection B3):ai_detection=true 时,Image 模式对原图跑一次
infer_seed.detect_seeds;Live 模式用 SeedLiveDetector(排队上限 1,
新帧顶旧帧)后台推理,结果按 frame_id 对齐写进 live_status.json 的
"ai" 字段:{frame_id, seed_count, dets, infer_ms, lag_frames}。
YOLO 输入是原图彩色帧,与 FPGA 灰度链路互不干扰。
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
import sys
import time
from pathlib import Path

import cv2
import numpy as np

import camera_capture
import frame_protocol as fp
import network_frame_receiver
import pc_client

sys.path.insert(0, str(PROJECT_ROOT
                       / "src/software/ai/inference"))
import infer_seed                      # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "outputs/capture"
DEFAULT_HOST = "192.168.2.99"
DEFAULT_PORT = 9000

# 5 档一键切换;seed_preset 走 pipeline_mode=1(板端忽略其余字段),
# 其余 4 档走显式组合(与 pipeline_validation 已验证用例同参,out_sel 恒 0=最终 morph 输出)
PRESETS = {
    "seed_preset": dict(pipeline_mode=fp.PIPELINE_SEED_PRESET),
    "sobel_thr80_erode": dict(mode=0, threshold=80, morph_mode=1,
                              enable_gauss=True,
                              pipeline_mode=fp.PIPELINE_EXPLICIT),
    "sobel_thr80_dilate": dict(mode=0, threshold=80, morph_mode=2,
                               enable_gauss=True,
                               pipeline_mode=fp.PIPELINE_EXPLICIT),
    "prewitt_thr80_erode": dict(mode=1, threshold=80, morph_mode=1,
                                enable_gauss=True,
                                pipeline_mode=fp.PIPELINE_EXPLICIT),
    "prewitt_thr0_dilate": dict(mode=1, threshold=0, morph_mode=2,
                                enable_gauss=True,
                                pipeline_mode=fp.PIPELINE_EXPLICIT),
}

# 前 9 字段与板端 pc_side 口径同构(验收逐帧 CSV),后 6 字段为 PC 侧扩展
METRIC_FIELDS = ["frame_id", "width", "height", "config", "golden",
                 "different_pixels", "round_trip_ms", "send_ms", "recv_ms",
                 "preset", "camera_fps", "capture_ts_ns", "status",
                 "cam_dropped", "net_dropped"]

class MetricsWriter:
    """逐帧追加 CSV;status 存名字字符串(OK/NET_TIMEOUT/DMA_ERR/...)。"""

    def __init__(self, path):
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=METRIC_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def row(self, **fields):
        self._writer.writerow(fields)
        self._file.flush()

    def close(self):
        self._file.close()

def resolve_pipeline(request):
    """preset 字符串优先,否则取 pipeline dict;返回 (params, preset_name)。"""
    name = request.get("preset")
    if name:
        if name not in PRESETS:
            raise ValueError(f"未知 preset {name!r},可选:{list(PRESETS)}")
        return dict(PRESETS[name]), name
    pipe = request.get("pipeline") or {}
    params = dict(mode=pipe.get("mode", 0),
                  threshold=pipe.get("threshold", 0),
                  morph_mode=pipe.get("morph", 0),
                  enable_gauss=bool(pipe.get("en_gauss", 0)),
                  pipeline_mode=fp.PIPELINE_EXPLICIT)
    return params, "custom"

def params_config(params):
    """CSV 的 config 列:seed=Seed Preset / explicit=显式组合。"""
    return "seed" if params.get("pipeline_mode") == fp.PIPELINE_SEED_PRESET \
        else "explicit"

def status_name(resp_or_exc):
    if isinstance(resp_or_exc, dict):
        return fp.ERROR_NAMES.get(resp_or_exc["status"], "UNKNOWN")
    if isinstance(resp_or_exc, fp.ProtocolError):
        return fp.ERROR_NAMES.get(resp_or_exc.status, "PROTO_ERR")
    return "NET_TIMEOUT"

def write_live_status(out_dir, info):
    path = out_dir / "live_status.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    # Windows 下 Qt/记录器可能正好打开着 live_status.json，原子替换会短暂
    # 报 PermissionError；重试几次即可，不能因此把后端主循环打崩。
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.002)

def _camera_fps(cam_stats):
    """CameraCapture 用 fps_capture；NetworkFrameReceiver 用 fps_network。"""
    return cam_stats.get("fps_capture", cam_stats.get("fps_network", 0.0))

def _network_status(cam_stats):
    """接收端 stats → live_status 的 network 节点（只暴露对外字段）。"""
    lat = cam_stats.get("latency") or {}
    return {
        "listen": cam_stats.get("listen", ""),
        "fps": cam_stats.get("fps_network", 0.0),
        "frames_received": cam_stats.get("frames_received", 0),
        "frames_delivered": cam_stats.get("frames_delivered", 0),
        "frames_dropped": cam_stats.get("frames_dropped", 0),
        "frame_gaps": cam_stats.get("frame_gaps", 0),
        "decode_errors": cam_stats.get("decode_errors", 0),
        "protocol_errors": cam_stats.get("protocol_errors", 0),
        "timeouts": cam_stats.get("timeouts", 0),
        "capture_to_recv_ms_last": lat.get("capture_to_recv_ms_last"),
        "capture_to_recv_ms_avg": lat.get("capture_to_recv_ms_avg"),
        "decode_ms_last": lat.get("decode_ms_last"),
        "client_connected": cam_stats.get("client_connected", False),
        "connections": cam_stats.get("connections", 0),
        "reconnects": cam_stats.get("reconnects", 0),
        "last_error": cam_stats.get("last_error", ""),
        "open_error": cam_stats.get("open_error", ""),
    }

# ------------------------------------------------------------------
# Image / Batch
# ------------------------------------------------------------------
def send_one(client, gray, params, preset_name, frame_id=0, do_golden=False):
    """发一帧,返回 (row_dict, board_gray, resp)。row 已含 golden 对拍。"""
    h, w = gray.shape
    resp, rtt_ms, send_ms, recv_ms = client.send_gray(
        gray, frame_id=frame_id,
        mode=params.get("mode", 0),
        morph_mode=params.get("morph_mode", 0),
        threshold=params.get("threshold", 0),
        enable_gauss=params.get("enable_gauss", False),
        pipeline_mode=params.get("pipeline_mode", fp.PIPELINE_EXPLICIT))
    status = status_name(resp)
    board = None
    diff = ""
    golden = ""
    if status == "OK":
        board = np.frombuffer(resp["payload"], dtype=np.uint8).reshape(h, w)
        if do_golden:
            expect = pc_client.local_golden(
                gray, mode=params.get("mode", 0),
                morph_mode=params.get("morph_mode", 0),
                threshold=params.get("threshold", 0),
                enable_gauss=params.get("enable_gauss", False),
                pipeline_mode=params.get("pipeline_mode",
                                         fp.PIPELINE_EXPLICIT))
            diff = int(np.count_nonzero(board != expect))
            golden = "PASS" if diff == 0 else "FAIL"
    row = {
        "frame_id": frame_id,
        "width": w, "height": h,
        "config": params_config(params),
        "golden": golden,
        "different_pixels": diff,
        "round_trip_ms": round(rtt_ms, 3),
        "send_ms": send_ms, "recv_ms": recv_ms,
        "preset": preset_name,
        "camera_fps": 0,
        "capture_ts_ns": 0,
        "status": status,
        "cam_dropped": 0,
        "net_dropped": 0,
    }
    return row, board, resp

def timeout_row(gray, frame_id, preset, params, capture_ts_ns=0,
                cam_dropped=0, net_dropped=0, camera_fps=0, status=None):
    row = dict.fromkeys(METRIC_FIELDS, "")
    row.update(frame_id=frame_id, width=gray.shape[1], height=gray.shape[0],
               config=params_config(params), preset=preset,
               camera_fps=camera_fps, capture_ts_ns=capture_ts_ns,
               status=status or "NET_TIMEOUT", cam_dropped=cam_dropped,
               net_dropped=net_dropped)
    return row

def handle_image(request):
    out_dir = Path(request.get("output_dir", DEFAULT_OUT))
    out_dir.mkdir(parents=True, exist_ok=True)
    params, preset = resolve_pipeline(request)
    gray = pc_client.read_gray_image(request["image_path"])
    client = pc_client.FrameClient(request.get("host", DEFAULT_HOST),
                                   request.get("port", DEFAULT_PORT),
                                   request.get("timeout", 5.0)).connect()
    try:
        row, board, _ = send_one(client, gray, params, preset,
                                 do_golden=bool(request.get("golden_audit")))
    finally:
        client.close()
    input_path = out_dir / ("input_" + Path(request["image_path"]).name)
    cv2.imwrite(str(input_path), gray)
    result = {"image": {"input": str(input_path)}, "frame_id": row["frame_id"],
              "latency_ms": row["round_trip_ms"], "status": row["status"]}
    if board is not None:
        out_path = out_dir / (f"board_{preset}_"
                              + Path(request["image_path"]).stem + ".png")
        cv2.imwrite(str(out_path), board)
        result["image"]["output"] = str(out_path)
        if row["golden"]:
            result["golden"] = {"different_pixels": row["different_pixels"],
                                "total_pixels": int(gray.size),
                                "match": row["golden"] == "PASS"}
    # AI 检测(YOLO 输入是原图彩色帧,不经过 FPGA 灰度链路)
    if request.get("ai_detection"):
        color = cv2.imread(str(request["image_path"]))
        if color is None:
            color = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        det = infer_seed.SeedDetector(conf=float(request.get("ai_conf", 0.25)))
        t0 = time.perf_counter()
        dets = det.detect_seeds(color, frame_id=row["frame_id"])
        result["ai"] = {"frame_id": row["frame_id"],
                        "seed_count": len(dets), "dets": dets,
                        "infer_ms": round((time.perf_counter() - t0) * 1000, 1)}
    return result

def handle_batch(request):
    out_dir = Path(request.get("output_dir", DEFAULT_OUT))
    out_dir.mkdir(parents=True, exist_ok=True)
    params, preset = resolve_pipeline(request)
    in_dir = Path(request["input_dir"])
    exts = (".tiff", ".tif", ".png", ".bmp", ".bin")
    images = sorted(p for p in in_dir.iterdir()
                    if p.suffix.lower() in exts)
    if not images:
        raise RuntimeError(f"{in_dir} 下没有图像")
    do_golden = bool(request.get("golden_audit"))

    metrics = MetricsWriter(out_dir / "frame_metrics.csv")
    rows, ok, dma_err, timeout = [], 0, 0, 0
    t0 = time.perf_counter()
    client = pc_client.FrameClient(request.get("host", DEFAULT_HOST),
                                   request.get("port", DEFAULT_PORT),
                                   request.get("timeout", 5.0)).connect()
    try:
        for i, path in enumerate(images):
            gray = pc_client.read_gray_image(path)
            try:
                row, board, resp = send_one(client, gray, params, preset,
                                            frame_id=i, do_golden=do_golden)
            except (TimeoutError, ConnectionError, OSError):
                timeout += 1
                row = timeout_row(gray, i, preset, params, net_dropped=1)
            except fp.ProtocolError as exc:
                row = timeout_row(gray, i, preset, params,
                                  status=status_name(exc))
            if row["status"] == "OK":
                ok += 1
                if board is not None:
                    cv2.imwrite(str(out_dir / f"board_{preset}_"
                                    f"{path.stem}.png"), board)
            elif row["status"] == "DMA":
                dma_err += 1
            metrics.row(**row)
            rows.append(row)
    finally:
        metrics.close()
        client.close()
    elapsed = time.perf_counter() - t0
    stats = {"images": len(images), "ok": ok, "timeout": timeout,
             "dma_err": dma_err,
             "fps": round(ok / elapsed, 1) if elapsed > 0 else 0,
             "metrics_csv": str(out_dir / "frame_metrics.csv")}
    checked = sum(1 for r in rows if r["golden"])
    mismatched = sum(1 for r in rows if r["golden"] == "FAIL")
    return {"image": None, "frame_id": len(images) - 1,
            "latency_ms": 0, "status": "OK" if timeout == 0 else "PARTIAL",
            "stats": stats,
            "golden": {"checked": checked, "mismatched": mismatched}}

# ------------------------------------------------------------------
# Live
# ------------------------------------------------------------------
def _fallback_camera(request):
    """相机打不开时降级:demo_seed_video.mp4 回放(同接口)。"""
    out_dir = Path(request.get("output_dir", DEFAULT_OUT))
    seed = out_dir / "demo_seed_video.mp4"
    if seed.exists():
        return str(seed), "VIDEO_SEED"
    return None, "OFFLINE"

def handle_live(request):
    out_dir = Path(request.get("output_dir", DEFAULT_OUT))
    out_dir.mkdir(parents=True, exist_ok=True)
    params, preset = resolve_pipeline(request)
    width = request.get("width", 640)
    height = request.get("height", 480)
    frames = request.get("frames", 300)
    if frames is None:
        frames = 10 ** 9   # 无限:由 C 杀进程终止
    do_golden = bool(request.get("golden_audit"))

    # 帧来源：默认本地摄像头；source=network 时改用电脑1 网络帧。
    use_network = str(request.get("source", "board")).lower() == "network"
    if use_network:
        listen_host = str(request.get("network_listen_host", "0.0.0.0"))
        listen_port = int(request.get("network_listen_port", 9100))
        cam = network_frame_receiver.NetworkFrameReceiver(
            listen_host=listen_host, listen_port=listen_port,
            timeout=float(request.get("network_timeout", 5.0)),
            width=int(width), height=int(height))
        camera_state = "NETWORK"
        if not cam.start():
            net_info = _network_status(cam.stats())
            net_info["open_error"] = cam._open_error
            write_live_status(out_dir, {
                "running": False, "camera": "OFFLINE",
                "fpga": "UNKNOWN", "dma": "UNKNOWN", "ai": None,
                "network": net_info,
                "error": f"网络帧接收端启动失败: {cam._open_error}"})
            raise RuntimeError(
                f"网络帧接收端 {listen_host}:{listen_port} 启动失败:"
                f"{cam._open_error}")
    else:
        src = request.get("camera_src", request.get("camera_index", 0))
        cam = camera_capture.CameraCapture(src=src, width=width, height=height,
                                           out_dir=out_dir)
        camera_state = "ONLINE" if not cam.is_file else "VIDEO_SEED"
        if not cam.start():
            fallback, camera_state = _fallback_camera(request)
            if not fallback:
                raise RuntimeError(f"相机 {src!r} 打不开且无降级视频:"
                                   f"{cam._open_error}")
            cam = camera_capture.CameraCapture(src=fallback, width=width,
                                               height=height, out_dir=out_dir)
            cam.start()

    # FPGA:TCP 连接即视为服务在线;首帧静默超时才判 OFFLINE 并中止。
    # 特意不做 probe:probe 帧会被板端 --frames 计数,吞掉验收帧数
    # (上板探测用 pc_client.py probe,不混入 Live 流程)。
    host = request.get("host", DEFAULT_HOST)
    port = request.get("port", DEFAULT_PORT)
    client = pc_client.FrameClient(host, port, request.get("timeout", 5.0))
    try:
        client.connect()
        fpga_state = "ONLINE"
    except (OSError, TimeoutError, ConnectionError) as exc:
        fail_status = {"running": False, "camera": camera_state,
                       "fpga": "OFFLINE", "dma": "UNKNOWN",
                       "ai": None, "error": str(exc)}
        if use_network:
            fail_status["network"] = _network_status(cam.stats())
        write_live_status(out_dir, fail_status)
        cam.stop()
        raise RuntimeError(f"FPGA {host}:{port} 连不上:{exc}")

    sample_dir = out_dir / "golden_sample10"
    sample_dir.mkdir(parents=True, exist_ok=True)
    for old in sample_dir.glob("*"):
        old.unlink()
    metrics = MetricsWriter(out_dir / "frame_metrics.csv")

    # AI 检测:后台推理线程 + 排队上限 1(新帧顶旧帧),不阻塞主链路
    ai_det = None
    ai_info = None
    if request.get("ai_detection"):
        ai_det = infer_seed.SeedLiveDetector(
            conf=float(request.get("ai_conf", 0.25)))

    counters = {"ok": 0, "timeout": 0, "dma_err": 0, "protocol_err": 0}
    saved_samples = 0
    net_dropped = 0
    t0 = time.perf_counter()
    cam_stats = cam.stats()
    camera_fps = _camera_fps(cam_stats)
    row = None
    try:
        for frame_id in range(frames):
            item = cam.read(timeout=1.0)
            if item is None and use_network:
                # 电脑1 可能还没启动或刚断线：等一会儿，不消耗帧预算。
                wait_t0 = time.monotonic()
                waiting_first = cam.stats().get("frames_received", 0) == 0
                wait_limit = float(request.get(
                    "network_start_timeout_s" if waiting_first
                    else "network_idle_timeout_s",
                    60.0 if waiting_first else 10.0))
                while item is None:
                    if time.monotonic() - wait_t0 > wait_limit:
                        stderr(f"帧 {frame_id}:网络无帧超过 {wait_limit:g}s,"
                               f"提前结束")
                        break
                    item = cam.read(timeout=0.5)
                if item is None:
                    break
            elif item is None:
                stderr(f"帧 {frame_id}:相机无帧(EOF/饥饿),提前结束")
                break
            gray, color, cam_frame_id, capture_ts, capture_mono = item
            cam_stats = cam.stats()
            camera_fps = _camera_fps(cam_stats)
            board = None
            if ai_det is not None:
                ai_det.submit(color, cam_frame_id)
                latest = ai_det.latest()
                if latest is not None:
                    ai_fid, ai_dets, ai_ms, _ = latest
                    ai_info = {"frame_id": ai_fid,
                               "seed_count": len(ai_dets),
                               "dets": ai_dets, "infer_ms": ai_ms,
                               "lag_frames": cam_frame_id - ai_fid}
            try:
                row, board, resp = send_one(client, gray, params, preset,
                                            frame_id=frame_id,
                                            do_golden=do_golden)
                row["capture_ts_ns"] = capture_ts
                row["camera_fps"] = camera_fps
                row["cam_dropped"] = cam_stats["frames_dropped"]
                row["net_dropped"] = net_dropped
                if use_network:
                    # 采集→接收用两机墙钟（需对时）；接收→响应用本机单调钟。
                    c2r_ms = (time.time_ns() - capture_ts) / 1e6
                    local_ms = (time.monotonic_ns() / 1e6
                                - capture_mono / 1e6)
                    row["full_ms"] = round(c2r_ms + local_ms, 3)
                else:
                    row["full_ms"] = round(row["recv_ms"]
                                           - capture_mono // 1_000_000, 3)
                if resp["status"] == fp.ERR_DMA:
                    counters["dma_err"] += 1
            except (TimeoutError, ConnectionError, OSError):
                net_dropped += 1
                counters["timeout"] += 1
                fpga_state = "OFFLINE"
                row = timeout_row(gray, frame_id, preset, params,
                                  capture_ts_ns=capture_ts,
                                  cam_dropped=cam_stats["frames_dropped"],
                                  net_dropped=net_dropped,
                                  camera_fps=camera_fps)
                write_live_status(out_dir, {
                    "running": False, "state": "ERROR", "camera": camera_state,
                    "fpga": "OFFLINE", "dma": "UNKNOWN", "preview": {},
                    "error": "Board connection lost or response timed out; restart after recovery"})
                if frame_id > 0:
                    raise ConnectionError("Board connection lost; stale frame invalidated")
                if frame_id == 0:
                    # 服务在 listen 却对首帧静默 → 判 FPGA OFFLINE,中止
                    fail_status = {
                        "running": False, "camera": camera_state,
                        "fpga": "OFFLINE", "dma": "UNKNOWN",
                        "ai": ai_info,
                        "error": f"首帧 NET_TIMEOUT(host={host},port={port})"}
                    if use_network:
                        fail_status["network"] = _network_status(cam_stats)
                    write_live_status(out_dir, fail_status)
                    raise RuntimeError(f"FPGA {host}:{port} 首帧静默超时,"
                                       f"判 OFFLINE 中止")
            except fp.ProtocolError as exc:
                counters["protocol_err"] += 1
                row = timeout_row(gray, frame_id, preset, params,
                                  capture_ts_ns=capture_ts,
                                  cam_dropped=cam_stats["frames_dropped"],
                                  net_dropped=net_dropped,
                                  camera_fps=camera_fps,
                                  status=status_name(exc))

            if row["status"] == "OK":
                counters["ok"] += 1
                fpga_state = "ONLINE"
                # Qt 显示用最新帧预览(覆盖写,避免文件爆炸)
                preview_dir = out_dir / "preview"
                preview_dir.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(preview_dir / "cam_latest.png"), gray)
                cv2.imwrite(str(preview_dir / "board_latest.png"), board)
            if board is not None and saved_samples < 10:
                cv2.imwrite(str(sample_dir / f"cam_{frame_id:06d}.png"), gray)
                cv2.imwrite(str(sample_dir / f"board_{frame_id:06d}.png"),
                            board)
                saved_samples += 1
            metrics.row(**{k: row.get(k, "") for k in METRIC_FIELDS})

            # 每帧状态文件(供 Qt 轮询显示)
            elapsed = max(time.perf_counter() - t0, 1e-6)
            status_info = {
                "running": True,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "camera": camera_state,
                "fpga": fpga_state,
                "dma": "ERROR" if counters["dma_err"] else "READY",
                "frame_id": frame_id,
                "preset": preset,
                "width": width, "height": height,
                "fps": {"camera": camera_fps,
                        "live": round(counters["ok"] / elapsed, 1)},
                "latency": {"rtt_ms": row.get("round_trip_ms", ""),
                            "round_trip_ms": row.get("round_trip_ms", ""),
                            "full_ms": row.get("full_ms", "")},
                "counters": dict(counters),
                "cam_dropped": cam_stats["frames_dropped"],
                "net_dropped": net_dropped,
                "ai": ai_info,
                "preview": {"cam": str(out_dir / "preview/cam_latest.png"),
                            "board": str(out_dir / "preview"
                                         / "board_latest.png")},
            }
            if use_network:
                status_info["network"] = _network_status(cam_stats)
            write_live_status(out_dir, status_info)
    finally:
        metrics.close()
        cam.stop()
        client.close()
        if ai_det is not None:
            ai_det.stop()

    elapsed = max(time.perf_counter() - t0, 1e-6)
    cam_stats = cam.stats()
    camera_fps = _camera_fps(cam_stats)
    golden_result = None
    if request.get("golden_audit") and saved_samples:
        golden_result = golden_audit(out_dir, params, preset)
    final_status = {
        "running": False,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "camera": camera_state, "fpga": fpga_state,
        "dma": "ERROR" if counters["dma_err"] else "READY",
        "frame_id": max(counters["ok"] + counters["timeout"] - 1, 0),
        "preset": preset, "width": width, "height": height,
        "fps": {"camera": camera_fps,
                "live": round(counters["ok"] / elapsed, 1)},
        "counters": dict(counters),
        "cam_dropped": cam_stats["frames_dropped"],
        "net_dropped": net_dropped,
        "ai": ai_info,
    }
    if use_network:
        final_status["network"] = _network_status(cam_stats)
    write_live_status(out_dir, final_status)
    return {"image": None, "frame_id": counters["ok"] + counters["timeout"],
            "latency_ms": 0, "status": "OK", "ai": ai_info, "stats": {
                "frames_ok": counters["ok"],
                "frames_timeout": counters["timeout"],
                "dma_err": counters["dma_err"],
                "protocol_err": counters["protocol_err"],
                "cam_dropped": cam_stats["frames_dropped"],
                "net_dropped": net_dropped,
                "camera_fps": camera_fps,
                "live_fps": round(counters["ok"] / elapsed, 1),
                "saved_samples": saved_samples,
                "metrics_csv": str(out_dir / "frame_metrics.csv"),
                "live_status": str(out_dir / "live_status.json")},
            "golden": golden_result}

def golden_audit(out_dir, params, preset):
    """对 golden_sample10/ 的保存帧做本地 golden 对拍:diff 热图 + 报告。"""
    sample_dir = out_dir / "golden_sample10"
    rows = []
    for cam_path in sorted(sample_dir.glob("cam_*.png")):
        frame_id = int(cam_path.stem.split("_")[1])
        board_path = sample_dir / f"board_{frame_id:06d}.png"
        if not board_path.exists():
            continue
        gray = cv2.imread(str(cam_path), cv2.IMREAD_GRAYSCALE)
        board = cv2.imread(str(board_path), cv2.IMREAD_GRAYSCALE)
        expect = pc_client.local_golden(
            gray, mode=params.get("mode", 0),
            morph_mode=params.get("morph_mode", 0),
            threshold=params.get("threshold", 0),
            enable_gauss=params.get("enable_gauss", False),
            pipeline_mode=params.get("pipeline_mode", fp.PIPELINE_EXPLICIT))
        diff = board.astype(np.int16) - expect.astype(np.int16)
        n_diff = int(np.count_nonzero(diff))
        heat = cv2.applyColorMap(np.abs(diff).clip(0, 255).astype(np.uint8),
                                 cv2.COLORMAP_JET)
        cv2.imwrite(str(sample_dir / f"diff_{frame_id:06d}.png"), heat)
        rows.append({"frame_id": frame_id, "preset": preset,
                     "different_pixels": n_diff,
                     "total_pixels": int(gray.size),
                     "max_abs_diff": int(np.abs(diff).max()),
                     "match": n_diff == 0})
    report_path = sample_dir / "golden_report.csv"
    if rows:
        with open(report_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        mismatched = sum(1 for r in rows if not r["match"])
    else:
        mismatched = 0
    return {"samples": len(rows), "mismatched": mismatched,
            "report": str(report_path)}

# ------------------------------------------------------------------
# CLI(QProcess 兼容入口:--request <json> → stdout 一行 JSON)
# ------------------------------------------------------------------
def stderr(msg):
    print(msg, file=sys.stderr, flush=True)

def run(request):
    mode = request.get("mode", "image")
    if mode == "image":
        return handle_image(request)
    if mode == "batch":
        return handle_batch(request)
    if mode == "live":
        return handle_live(request)
    raise ValueError(f"mode {mode!r} 非法,可选 image/batch/live")

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()

    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = run(request)
        print(json.dumps({"schema_version": 1, "ok": True, "mode":
                          request.get("mode", "image"), **result},
                         ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({"schema_version": 1, "ok": False,
                          "error": str(error)},
                         ensure_ascii=False, allow_nan=False))
        return 1

if __name__ == "__main__":
    sys.exit(main())
