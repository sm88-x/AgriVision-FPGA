
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

# -*- coding: utf-8 -*-
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

import live_backend as backend
import camera_capture

OriginalCamera = camera_capture.CameraCapture
request_path = Path(sys.argv[sys.argv.index("--request") + 1])
request = json.loads(request_path.read_text(encoding="utf-8"))
out_dir = Path(request["output_dir"])
stop_file = out_dir / "stop.request"

# CSV 加入现有后端已计算、但原 CSV 未保存的 full_ms。
if "full_ms" not in backend.METRIC_FIELDS:
    backend.METRIC_FIELDS.append("full_ms")

def normalize_gray(gray, width, height):
    """等比例缩放、白底填充，保证网络尺寸严格一致。"""
    h, w = gray.shape
    scale = min(width / w, height / h)
    nw = min(width, max(1, round(w * scale)))
    nh = min(height, max(1, round(h * scale)))
    resized = cv2.resize(
        gray, (nw, nh),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
    )
    canvas = np.full((height, width), 255, dtype=np.uint8)
    x, y = (width - nw) // 2, (height - nh) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas

class QtCamera(OriginalCamera):
    def _open(self):
        if self.is_file:
            raise RuntimeError("真实摄像头模式不允许视频回放")

        # DSHOW 不可用时尝试 MSMF。
        for api in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
            cap = cv2.VideoCapture(int(self.src), api)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                return cap
            cap.release()
        raise RuntimeError("无法打开内置摄像头 index={}".format(self.src))

    def _to_gray(self, frame):
        gray = OriginalCamera._to_gray(frame)
        return normalize_gray(gray, self.width, self.height)

    def read(self, timeout=1.0):
        # 在两帧之间正常停止，使原后端执行 finally 和 Golden 汇总。
        if stop_file.exists():
            return None

        end = time.monotonic() + timeout
        with self._cond:
            while not self._slot["fresh"]:
                if stop_file.exists() or self._eof:
                    return None
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.1))

            # 在锁内复制槽位元数据，防止生产线程覆盖。
            slot = dict(self._slot)
            self._slot["fresh"] = False

        self._frames_delivered += 1
        return (
            slot["frame"], slot["seq"],
            slot["ts_ns"], slot["ts_mono_ns"],
        )

camera_capture.CameraCapture = QtCamera

# 摄像头打不开就明确失败，禁止自动降级视频。
backend._fallback_camera = lambda req: (None, "OFFLINE")

# Image / Batch 同样按界面选择的尺寸发送。
original_read = backend.pc_client.read_gray_image

def read_normalized(path):
    return normalize_gray(
        original_read(path),
        int(request.get("width", 640)),
        int(request.get("height", 480)),
    )

backend.pc_client.read_gray_image = read_normalized

def safe_write_live_status(directory, info):
    directory = Path(directory)
    target = directory / "live_status.json"
    temporary = directory / "live_status.tmp"

    temporary.write_text(
        json.dumps(info, ensure_ascii=False),
        encoding="utf-8",
    )

    # Windows 中 Qt/杀毒软件短暂持有文件句柄时重试。
    for _ in range(25):
        try:
            temporary.replace(target)
            return
        except PermissionError:
            time.sleep(0.01)

    # 本次状态刷新失败不应中断摄像头和 FPGA 数据链。
    print(
        "状态文件暂时被占用，跳过本次刷新",
        file=sys.stderr,
        flush=True,
    )

backend.write_live_status = safe_write_live_status

original_send_one = backend.send_one

def timed_send_one(*args, **kwargs):
    row, board, response = original_send_one(*args, **kwargs)

    # live_backend 用 recv_ms 减去相机的 monotonic 时间戳。
    # 必须使用同一时钟的绝对毫秒值，不能混用 perf_counter。
    row["recv_ms"] = time.monotonic_ns() / 1_000_000.0
    return row, board, response

backend.send_one = timed_send_one

# 由 B 的原入口处理请求、日志和 stdout JSON。
if __name__ == "__main__":
    sys.exit(backend.main())
