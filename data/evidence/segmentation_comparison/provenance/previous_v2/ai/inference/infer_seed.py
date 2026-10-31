#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detection 卡B3:YOLO 种子检测推理接口(Image / Live 共用同一入口)。

接口约定:
  detect_seeds(image_bgr, frame_id=None)
    -> [{"id": int, "bbox_xyxy": [x1,y1,x2,y2], "confidence": float,
         "class_name": "seed"}, ...]
  - 坐标系: 输入原图像素坐标(OpenCV BGR 口径),检测框按先上后下、再左到右排序,
    id 稳定便于 Qt 逐框交互
  - YOLO 输入是**原图(彩色)**,FPGA V4 只负责像素流预处理(灰度/边缘),
    不声称 FPGA 提高 YOLO 精度

实时链路(SeedLiveDetector):
  后台推理线程 + 待处理槽位长度 1 —— 新帧顶掉旧帧(排队上限 1,不堆积),
  结果带 frame_id,消费端可据此丢弃过期帧,UI 延迟不会持续增长。

用法:
  python infer_seed.py --images <目录或图片...>   # 自检: 逐图推理打印 JSON
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
import sys
import threading
import time
from pathlib import Path

import numpy as np
import cv2

REPO = PROJECT_ROOT
sys.path.insert(0, str(REPO))
DEFAULT_WEIGHTS = Path(__file__).resolve().parents[1] / "model/best.pt"

class SeedDetector:
    """同步推理入口。"""

    def __init__(self, weights=DEFAULT_WEIGHTS, conf=0.25, imgsz=640,
                 device=None,iou=0.45):
        from ultralytics import YOLO
        if not Path(weights).exists():
            raise FileNotFoundError(f"权重不存在: {weights}")
        self.conf = conf
        self.imgsz = imgsz
        self.iou = float(iou)
        if not 0.0 <= self.iou <= 1.0:
            raise ValueError("iou 必须在 0 到 1 之间")
        self.model = YOLO(str(weights))
        if device is None:
            import torch
            device = 0 if torch.cuda.is_available() else "cpu"
        self.device = device
        # ultralytics predict 内部非线程安全,同步入口串行化
        self._lock = threading.Lock()

    def detect_seeds(self, image_bgr, frame_id=None):
        """image_bgr: np.ndarray (H, W, 3) uint8 BGR。返回检测列表。"""
        if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
            return []
        if image_bgr.ndim == 2:      # 灰度源兜底:转 BGR(YOLO 输入是原图)
            image_bgr = cv2.cvtColor(image_bgr, cv2.COLOR_GRAY2BGR)
        with self._lock:
            res = self.model.predict(image_bgr, imgsz=self.imgsz,
                                     conf=self.conf, iou=self.iou,
                                     device=self.device, verbose=False)[0]
        dets = []
        boxes = getattr(res, "boxes", None)
        if boxes is not None and len(boxes):
            items = sorted(zip(boxes.xyxy.tolist(), boxes.conf.tolist()),
                           key=lambda t: (round(t[0][1]), t[0][0]))
            for i, (xyxy, conf) in enumerate(items):
                dets.append({
                    "id": i,
                    "bbox_xyxy": [round(float(v), 1) for v in xyxy],
                    "confidence": round(float(conf), 4),
                    "class_name": "seed",
                })
        return dets

class SeedLiveDetector:
    """实时链路: 后台推理线程 + 排队上限 1(新帧覆盖旧帧)。

    submit(frame_bgr, frame_id) -> dropped(旧帧是否被顶掉)
    latest() -> (frame_id, dets, infer_ms, ver) 或 None
    """

    def __init__(self, weights=DEFAULT_WEIGHTS, conf=0.25, imgsz=640,
                 device=None, iou=0.45):
        self._det = SeedDetector(weights=weights, conf=conf, imgsz=imgsz, device=device, iou=iou)
        self._slot = [None, None]      # [frame_bgr, frame_id] 待处理
        self._result = None            # {"frame_id","dets","infer_ms","done_ts"}
        self._ver = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                if self._slot[0] is None:
                    frame, fid = None, None
                else:
                    frame, fid = self._slot
                    self._slot = [None, None]
            if frame is None:
                time.sleep(0.002)
                continue
            t0 = time.perf_counter()
            dets = self._det.detect_seeds(frame, fid)
            infer_ms = (time.perf_counter() - t0) * 1000
            with self._lock:
                self._result = {"frame_id": fid, "dets": dets,
                                "infer_ms": round(infer_ms, 1),
                                "done_ts": time.monotonic()}
                self._ver += 1

    def submit(self, frame_bgr, frame_id):
        with self._lock:
            dropped = self._slot[0] is not None
            self._slot = [frame_bgr, frame_id]
        return dropped

    def latest(self):
        with self._lock:
            if self._result is None:
                return None
            return (self._result["frame_id"], self._result["dets"],
                    self._result["infer_ms"], self._ver)

    def stop(self):
        self._stop.set()

def main():
    ap = argparse.ArgumentParser(description="infer_seed 自检")
    ap.add_argument("--images", nargs="+", required=True,
                    help="图片文件或目录")
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.45)
    args = ap.parse_args()

    import cv2
    det = SeedDetector(args.weights, conf=args.conf, iou=args.iou)
    paths = []
    for item in args.images:
        p = Path(item)
        if p.is_dir():
            paths += sorted(p.glob("*.jpg")) + sorted(p.glob("*.png"))
        else:
            paths.append(p)
    out = {}
    for p in paths:
        img = cv2.imread(str(p))
        if img is None:
            out[str(p)] = {"error": "读图失败"}
            continue
        t0 = time.perf_counter()
        dets = det.detect_seeds(img, frame_id=p.name)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        out[str(p)] = {"n": len(dets), "infer_ms": ms, "dets": dets}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0

if __name__ == "__main__":
    sys.exit(main())
