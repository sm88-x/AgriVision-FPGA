#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detection C 侧后端适配器：在 live_stream 适配器之上挂一层「AI 检测」。

沿用 live_stream 已验证的契约，不另发明一套：
    入：<runDir>/request.json      （mode / input / width / height / threshold / morph /
                                    detect / conf / detector / source / frames / output_dir …）
    出：<runDir>/live_status.json  （running / state / camera_fps / live_fps / latency /
                                    preview / …,本层再补 ai 字段）
        <runDir>/preview/*.png     （cam_latest / board_latest / annot_latest）
        stdout 末行 = 最终 JSON（Image/Batch 模式）

本层新增字段（live_status.json 里的 "ai" 节点）：
    {"enabled": bool, "model_state": "READY"/ "LOADING/MOCK/OFFLINE",
     "seed_count": int, "infer_ms": float, "input": "bgr"/"gray", "source": "model"/"mock",
     "detections": [{"id":0,"bbox_xyxy":[x1,y1,x2,y2],"confidence":0.93,"class_name":"seed"}],
     "annot": "<annotated png path>"}

检测器两种：
    detector=mock  → 用连通域在原图上找候选块（确定性、无需模型，C1 阶段用）
    detector=auto  → 加载 B 的 src/software/ai/inference/infer_seed.py:SeedDetector（C2 阶段用）
                     找不到就自动回落 mock,并在 model_state 里标注 "MOCK"

数据源：
    source=board   → 走 live_stream 那条真实链路（摄像头 → 板端 V4 → 回传）
    source=mock    → 不占板子/不开相机，按目录里的图片循环喂帧（今晚没板子也能演示）
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
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

REPO = PROJECT_ROOT
live_stream_DIR = REPO / "src/software/adapters/live_chain"
APP_PY = REPO / "src/software/app"
# 链路文件按优先级排：显式指定的 AGRI_CHAIN_DIR > board/overlay/qt_live_stream（SMX 适配器写死的口径）
# > app/python > 仓库根。**顺序不能反**：app/python 下可能有旧草稿，会遮住正确版本。
_CHAIN = os.environ.get("AGRI_CHAIN_DIR", "").strip()
_ORDER = [d for d in (_CHAIN, str(live_stream_DIR), str(APP_PY), str(REPO)) if d and Path(d).is_dir()]
for _d in reversed(_ORDER):
    sys.path.insert(0, _d)

import live_backend as backend          # noqa: E402  B 的后端
import camera_capture                   # noqa: E402

SEED_COLOR = (60, 160, 70)             # BGR：农业绿
_LAST_BGR = None                      # 相机最近一帧原始彩色（YOLO 的输入）

# ---------------------------------------------------------------- 检测器
class MockDetector:
    """无模型：阈值 + 连通域找块。确定性，用于 C1 阶段把界面跑起来。"""

    name = "mock"
    state = "MOCK"

    def __init__(self, conf=0.25, min_area=120):
        self.conf = float(conf)
        self.min_area = int(min_area)

    def detect_seeds(self, image_bgr, frame_id=None):
        if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
            return []
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY) \
            if image_bgr.ndim == 3 else image_bgr
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if float((binary > 0).mean()) > 0.5:      # Otsu 极性可能把白底当目标,按少数类纠正
            binary = cv2.bitwise_not(binary)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                  np.ones((3, 3), np.uint8), iterations=1)
        count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        limit = 0.2 * gray.shape[0] * gray.shape[1]   # 超过整图 20% 的一律当背景
        h_img, w_img = gray.shape[:2]
        items = []
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if area < self.min_area or area > limit:
                continue
            # 细长边框（bbox 很大但填充率极低）不是种子，丢掉
            if w > 0.75 * w_img or h > 0.75 * h_img:
                continue
            if area / float(max(1, w * h)) < 0.15:
                continue
            value = min(0.99, 0.55 + area / 4000.0)
            if value < self.conf:            # 置信度阈值对 mock 同样生效（界面控件要能看出差别）
                continue
            items.append(((round(float(y)), float(x)),
                          [float(x), float(y), float(x + w), float(y + h)],
                          value))
        items.sort(key=lambda t: t[0])
        return [{"id": idx,
                 "bbox_xyxy": [round(v, 1) for v in bbox],
                 "confidence": round(float(conf), 4),
                 "class_name": "seed"}
                for idx, (_key, bbox, conf) in enumerate(items)]

class ModelDetector:
    """B 的真模型：src/software/ai/inference/infer_seed.py 的 SeedDetector。"""

    name = "model"
    state = "READY"

    def __init__(self, infer_path, weights=None, conf=0.25, imgsz=640):
        import importlib.util
        spec = importlib.util.spec_from_file_location("infer_seed", str(infer_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._det = module.SeedDetector(weights=weights or module.DEFAULT_WEIGHTS,
                                        conf=conf, imgsz=imgsz)
        self.conf = float(conf)
        # 模块可以自报状态（真模型 READY；接口 stub 会报 STUB，界面照实显示）
        self.state = getattr(module, "MODEL_STATE", "READY")

    def detect_seeds(self, image_bgr, frame_id=None):
        return self._det.detect_seeds(image_bgr, frame_id=frame_id)

def build_detector(request):
    """按 request 决定用哪个检测器；真模型不可用就回落 mock 并如实标注。"""
    conf = float(request.get("conf", 0.25))
    which = str(request.get("detector", "mock")).strip()
    if which in ("", "mock", "none"):
        return MockDetector(conf=conf), "mock requested"
    infer_path = Path(request.get("detector_path")
                      or os.environ.get("AGRI_INFER_SEED")
                      or (REPO / "src/software/ai/inference/infer_seed.py"))
    try:
        if not infer_path.exists():
            raise FileNotFoundError(str(infer_path))
        detector = ModelDetector(infer_path, weights=request.get("weights"),
                                 conf=conf, imgsz=int(request.get("imgsz", 640)))
        return detector, "model loaded: %s" % infer_path
    except Exception as exc:                       # noqa: BLE001 任何失败都回落 mock
        return MockDetector(conf=conf), "fallback to mock: %s" % exc

# ---------------------------------------------------------------- 画框
def annotate(image_bgr, detections, scale=1.0):
    """把框/序号/置信度画在图上（左侧列表用同一套 id）。"""
    canvas = image_bgr.copy() if image_bgr.ndim == 3 else cv2.cvtColor(image_bgr,
                                                                       cv2.COLOR_GRAY2BGR)
    thickness = max(1, int(round(2 * scale)))
    for det in detections:
        x1, y1, x2, y2 = [int(round(v)) for v in det["bbox_xyxy"]]
        cv2.rectangle(canvas, (x1, y1), (x2, y2), SEED_COLOR, thickness)
        label = "#%d %.2f" % (det["id"] + 1, float(det["confidence"]))
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5 * scale, 1)
        top = max(0, y1 - th - 6)
        cv2.rectangle(canvas, (x1, top), (x1 + tw + 6, top + th + 6), SEED_COLOR, -1)
        cv2.putText(canvas, label, (x1 + 3, top + th + 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5 * scale, (255, 255, 255), 1, cv2.LINE_AA)
    return canvas

def normalize_gray(gray, width, height):
    """等比例缩放 + 白底填充（与 live_stream 同一口径）。"""
    h, w = gray.shape[:2]
    scale = min(width / w, height / h)
    nw, nh = min(width, max(1, round(w * scale))), min(height, max(1, round(h * scale)))
    resized = cv2.resize(gray, (nw, nh),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    canvas = np.full((height, width), 255, dtype=np.uint8)
    x, y = (width - nw) // 2, (height - nh) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas

def normalize_bgr(image, width, height):
    """彩色图同样按 letterbox 口径缩放到目标尺寸（YOLO 输入用原图彩色）。"""
    h, w = image.shape[:2]
    scale = min(width / w, height / h)
    nw, nh = min(width, max(1, round(w * scale))), min(height, max(1, round(h * scale)))
    resized = cv2.resize(image, (nw, nh),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    x, y = (width - nw) // 2, (height - nh) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas

def atomic_write_json(path, obj):
    """原子写 + 重试（Qt 可能在读，Windows 会短暂占用）。"""
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    for _ in range(25):
        try:
            temporary.replace(target)
            return True
        except PermissionError:
            time.sleep(0.01)
    return False

# ---------------------------------------------------------------- mock 数据源
def run_mock_source(request, detector, out_dir):
    """不开相机、不占板子：按目录/单图循环喂帧，写出与真链路同格式的状态文件。"""
    width = int(request.get("width", 640))
    height = int(request.get("height", 480))
    frames = int(request.get("frames", 0)) or 10 ** 9
    fps = float(request.get("mock_fps", 8.0))
    enabled = bool(request.get("detect", True))
    stop_file = out_dir / "stop.request"
    preview = out_dir / "preview"
    preview.mkdir(parents=True, exist_ok=True)

    src = request.get("input_dir") or request.get("image_path")
    paths = []
    if src:
        p = Path(src)
        paths = sorted(p.glob("*")) if p.is_dir() else [p]
        paths = [q for q in paths if q.suffix.lower() in
                 (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")]
    if not paths:                      # 没有任何输入就合成一张
        synth = np.full((height, width, 3), 245, np.uint8)
        rng = np.random.RandomState(20261013)
        for i in range(9):
            cx, cy = rng.randint(80, width - 80), rng.randint(60, height - 60)
            cv2.ellipse(synth, (int(cx), int(cy)), (int(rng.randint(16, 30)),
                                                    int(rng.randint(12, 22))),
                        float(rng.randint(0, 180)), 0, 360, (60, 120, 70), -1)
        cv2.imwrite(str(preview / "source_synth.png"), synth)
        paths = [preview / "source_synth.png"]

    started = time.time()
    index = 0
    dets = []
    while index < frames and not stop_file.exists():
        image = cv2.imread(str(paths[index % len(paths)]))
        if image is None:
            index += 1
            continue
        # Original 视图给彩色原图（YOLO 的输入）；Board 视图给灰度（PL 的输入）
        bgr = normalize_bgr(image, width, height)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        t0 = time.perf_counter()
        dets = detector.detect_seeds(bgr, frame_id=index) if enabled else []
        infer_ms = (time.perf_counter() - t0) * 1e3
        annot = annotate(bgr, dets)
        cv2.imwrite(str(preview / "cam_latest.png"), bgr)
        cv2.imwrite(str(preview / "board_latest.png"), gray)
        cv2.imwrite(str(preview / "annot_latest.png"), annot)
        elapsed = max(1e-6, time.time() - started)
        atomic_write_json(out_dir / "live_status.json", {
            "running": True, "state": "LIVE", "mode": "Live",
            "source": "mock", "frame": index,
            "camera_fps": round((index + 1) / elapsed, 1),
            "live_fps": round((index + 1) / elapsed, 1),
            "latency": {"rtt_ms": "", "pl_ms": "", "full_ms": round(infer_ms, 3)},
            "preview": {"cam": str(preview / "cam_latest.png"),
                        "board": str(preview / "board_latest.png"),
                        "annot": str(preview / "annot_latest.png")},
            "ai": {"enabled": enabled, "model_state": detector.state,
                   "seed_count": len(dets), "infer_ms": round(infer_ms, 3),
                   "input": "bgr", "source": "mock-source",
                   "detections": dets,
                   "annot": str(preview / "annot_latest.png")},
        })
        index += 1
        time.sleep(max(0.0, 1.0 / fps))

    preview_dir = preview
    return {"ok": True, "mode": "Live", "source": "mock",
            "frames": index, "seed_count": len(dets),
            "ai": {"model_state": detector.state},
            "output_dir": str(out_dir)}

# ---------------------------------------------------------------- board 数据源
def patch_board_source(request, detector, out_dir):
    """走 live_stream 真链路：在它的每帧钩子里做检测，并把结果并进 live_status。"""
    global _LAST_BGR
    import live_adapter as d3          # noqa: F401  导入即完成 live_stream 补丁

    state = {"detections": [], "infer_ms": 0.0, "frame": 0, "annot": ""}
    enabled = bool(request.get("detect", True))
    preview = out_dir / "preview"
    preview.mkdir(parents=True, exist_ok=True)

    # 相机每帧的原始 BGR：live_stream 只留灰度，这里顺手把原图留住（YOLO 输入是原图）
    original_read = camera_capture.CameraCapture._to_gray

    def keeping_read(self, frame):
        global _LAST_BGR
        _LAST_BGR = frame.copy()
        self._last_bgr = frame.copy()
        return original_read(self, frame)

    camera_capture.CameraCapture._to_gray = keeping_read

    original_send = backend.send_one

    def detecting_send(*args, **kwargs):
        row, board, response = original_send(*args, **kwargs)
        if enabled:
            bgr = _LAST_BGR
            if bgr is not None:
                t0 = time.perf_counter()
                dets = detector.detect_seeds(bgr, frame_id=state["frame"])
                state["infer_ms"] = (time.perf_counter() - t0) * 1e3
                state["detections"] = dets
                annot = annotate(normalize_bgr(bgr, int(request.get("width", 640)),
                                               int(request.get("height", 480))), dets)
                path = preview / "annot_latest.png"
                cv2.imwrite(str(path), annot)
                state["annot"] = str(path)
        state["frame"] += 1
        return row, board, response

    backend.send_one = detecting_send

    original_write = backend.write_live_status

    def writing(directory, info):
        info = dict(info)
        info["ai"] = {"enabled": enabled, "model_state": detector.state,
                      "seed_count": len(state["detections"]),
                      "infer_ms": round(state["infer_ms"], 3),
                      "input": "bgr", "source": "board",
                      "detections": state["detections"], "annot": state["annot"]}
        if state["annot"]:
            info.setdefault("preview", {})["annot"] = state["annot"]
        return original_write(directory, info)

    backend.write_live_status = writing

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    args = ap.parse_args()

    # 容忍 BOM：PowerShell 等工具写的 JSON 可能带 BOM，Qt 写的没有
    request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
    # 界面里显示的是 Live/Image/Batch，B 的后端只接受小写 live/image/batch
    request["mode"] = str(request.get("mode", "live")).strip().lower()
    # 界面里 frames=0 表示"持续运行"，B 的后端按帧数退出，这里换成一个大数（靠 stop.request 停）
    if int(request.get("frames") or 0) <= 0:
        request["frames"] = 10 ** 9
    out_dir = Path(request["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    # B 的 live_backend 会自己再从磁盘读 --request，所以给它一份归一化后的副本
    normalized = out_dir / "request_normalized.json"
    normalized.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    sys.argv[sys.argv.index("--request") + 1] = str(normalized)
    detector, note = build_detector(request)
    print("[detection adapter] %s | %s" % (detector.state, note), flush=True)

    source = str(request.get("source", "board"))
    if source == "mock":
        result = run_mock_source(request, detector, out_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    request["timeout"] = max(0.1, min(float(request.get("timeout", 5.0)), 10.0))
    normalized.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    try:
        patch_board_source(request, detector, out_dir)
        result = backend.run(request)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        atomic_write_json(out_dir / "live_status.json", {
            "running": False, "state": "ERROR", "source": "board",
            "fpga": "OFFLINE", "dma": "UNKNOWN", "error": str(exc),
            "preview": {}, "ai": {"enabled": False, "model_state": "OFFLINE",
                                    "detections": [], "seed_count": 0}})
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

if __name__ == "__main__":
    sys.exit(main())

