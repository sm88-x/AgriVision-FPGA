#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_stream B 摄像头采集:USB 内置摄像头(主)/ 视频文件回放(降级)。

灰度转换用任务书定稿的定点式 `(77R+150G+29B+128)>>8`(run_pipeline.rgb2gray,
与板端 golden 同口径);**不用 cv2.cvtColor**——BT.601 舍入不同会让逐像素
Golden 差 ±1(见 hardware/board/protocol.md 第五节)。

- CameraCapture:采集线程 + 最新帧槽位;消费慢时丢旧帧(如实计数),
  主链路永不阻塞。read() 返回 (gray_uint8_hw, frame_id, capture_ts_ns,
  capture_mono_ns);两个时间戳分别基于墙钟/单调钟,全链路延迟用单调钟。
- probe_capabilities:index × 后端 × 尺寸 × fourcc 实测 → camera_capability.md。
- CLI:
    python camera_capture.py probe [--out live_stream_output]
    python camera_capture.py self-test [--n 100] [--index 0] [--w 640] [--h 480]
    python camera_capture.py record-video [--seconds 15] [--out live_stream_output]
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
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from run_pipeline import rgb2gray

BACKENDS = (("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY))
SIZES = ((640, 480), (320, 240), (256, 256))
DEFAULT_OUT = PROJECT_ROOT / "outputs/capture"

class CameraCapture:
    """后台采集线程 + 最新帧槽位(消费者慢则丢旧帧并计数)。"""

    def __init__(self, src=0, width=640, height=480, fourcc="MJPG",
                 out_dir=None, loop_file=True):
        self.src = src
        self.width, self.height = width, height
        self.fourcc = fourcc
        self.out_dir = Path(out_dir) if out_dir else DEFAULT_OUT
        self.loop_file = loop_file
        self.is_file = isinstance(src, str)

        self._cap = None
        self._slot = {"frame": None, "seq": 0, "fresh": False}
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._open_error = None
        self._frames_captured = 0
        self._frames_delivered = 0
        self._frames_dropped = 0
        self._t0 = None
        self._eof = False
        self.file_fps = 0.0      # 视频源标称帧率(回放限速用)
        self._play_t0 = None

    # ---------------- 生命周期 ----------------
    def _open(self):
        if self.is_file:
            cap = cv2.VideoCapture(self.src)
        else:
            cap = cv2.VideoCapture(self.src, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频源 {self.src!r}")
        if self.is_file:
            self.file_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not self.is_file:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return cap

    def start(self):
        try:
            self._cap = self._open()
        except RuntimeError as exc:
            self._open_error = str(exc)
            return False
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _run(self):
        self._play_t0 = time.monotonic()
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                if self.is_file and self.loop_file:
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    self._play_t0 = time.monotonic()   # 回绕后重排播放时钟
                    continue
                self._eof = True
                break
            gray = self._to_gray(frame)
            if gray.shape[1] != self.width or gray.shape[0] != self.height:
                # 相机不按请求尺寸出帧:按左上角裁剪到请求尺寸
                gray = gray[:self.height, :self.width]
            now_ns = time.time_ns()
            self._frames_captured += 1
            with self._cond:
                if self._slot["fresh"]:
                    self._frames_dropped += 1
                self._slot.update(frame=gray, seq=self._frames_captured,
                                  fresh=True, ts_ns=now_ns,
                                  ts_mono_ns=time.monotonic_ns())
                self._cond.notify_all()
            if self.is_file and self.file_fps > 0:
                # 视频回放按标称帧率限速,贴近真实相机节奏
                target = self._play_t0 + self._frames_captured / self.file_fps
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        with self._cond:
            self._cond.notify_all()

    @staticmethod
    def _to_gray(frame):
        """BGR → 灰度(定点式,与板端同口径)。灰度输入原样返回。"""
        if frame.ndim == 3 and frame.shape[2] in (3, 4):
            rgb = frame[:, :, 2::-1]      # BGR(A) → RGB
            return rgb2gray(rgb)
        return frame

    # ---------------- 取帧 ----------------
    def read(self, timeout=1.0):
        """返回 (gray, frame_id, capture_ts_ns, capture_mono_ns);
        超时/EOF 返回 None。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._slot["fresh"]:
                if self._eof:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            slot = self._slot
            slot["fresh"] = False
        self._frames_delivered += 1
        return (slot["frame"], slot["seq"], slot["ts_ns"],
                slot["ts_mono_ns"])

    # ---------------- 统计 ----------------
    def stats(self):
        elapsed = max(time.perf_counter() - self._t0, 1e-6) if self._t0 else 0
        return {
            "source": self.src,
            "is_file": self.is_file,
            "width": self.width,
            "height": self.height,
            "frames_captured": self._frames_captured,
            "frames_delivered": self._frames_delivered,
            "frames_dropped": self._frames_dropped,
            "fps_capture": round(self._frames_captured / elapsed, 1),
            "fps_delivery": round(self._frames_delivered / elapsed, 1),
            "eof": self._eof,
            "open_error": self._open_error,
        }

    # ---------------- 工具 ----------------
    def save_stills(self, n=10, subdir="golden_sample10"):
        """连续取 n 帧保存 PNG,返回文件路径列表。"""
        out = self.out_dir / subdir
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for _ in range(n):
            item = self.read(timeout=2)
            if item is None:
                break
            gray, frame_id, _, _ = item
            path = out / f"cam_{frame_id:06d}.png"
            cv2.imwrite(str(path), gray)
            paths.append(str(path))
        return paths

    def record_seed_video(self, seconds=15, path=None):
        """录制演示种子视频(相机可用时),返回路径。"""
        path = Path(path) if path else self.out_dir / "demo_seed_video.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"),
                                 15.0, (self.width, self.height), False)
        t_end = time.monotonic() + seconds
        n = 0
        while time.monotonic() < t_end and n < seconds * 15:
            item = self.read(timeout=1)
            if item is None:
                break
            writer.write(item[0])
            n += 1
        writer.release()
        return str(path) if n else None

# ------------------------------------------------------------------
# 探测
# ------------------------------------------------------------------
def probe_one(index, backend_name, backend, width, height, fourcc, n_frames=5):
    """单个组合实测:打开 → 设尺寸/fourcc → 取 n 帧计时。返回 dict。"""
    cap = cv2.VideoCapture(index, backend)
    result = {"index": index, "backend": backend_name,
              "requested": f"{width}x{height}", "fourcc": fourcc, "ok": False,
              "actual": None, "fps": 0.0, "note": ""}
    try:
        if not cap.isOpened():
            result["note"] = "open failed"
            return result
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        for _ in range(2):          # 首帧往往慢,预热
            if not cap.read()[0]:
                result["note"] = "first read failed"
                return result
        t0 = time.perf_counter()
        got = 0
        for _ in range(n_frames):
            ok, _ = cap.read()
            if not ok:
                break
            got += 1
        elapsed = time.perf_counter() - t0
        if got == 0:
            result["note"] = "no frame"
            return result
        result["ok"] = True
        result["fps"] = round(got / elapsed, 1)
        result["actual"] = f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x" \
                           f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
    finally:
        cap.release()
    return result

def probe_capabilities(max_index=5, out_dir=None):
    """index 0..max_index × 后端 × 尺寸 × MJPG/默认 全组合实测。"""
    out = Path(out_dir) if out_dir else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(max_index + 1):
        for backend_name, backend in BACKENDS:
            for width, height in SIZES:
                for fourcc in ("MJPG", ""):
                    rows.append(probe_one(index, backend_name, backend,
                                          width, height, fourcc))
    return rows

def write_capability_md(rows, out_dir=None):
    out = Path(out_dir) if out_dir else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    ok_rows = [r for r in rows if r["ok"]]
    lines = [
        "# 摄像头能力探测(camera_capability)",
        "",
        f"- 探测时间:{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 探测范围:index 0..{max(r['index'] for r in rows)} × DSHOW/MSMF/ANY "
        f"× 640×480/320×240/256×256 × MJPG/默认",
        f"- 可用组合:{len(ok_rows)} / 总组合:{len(rows)}",
        "",
        "## 推荐配置(自测用)",
        "",
        "| index | 后端 | 尺寸 | fourcc | 实测 FPS | 实际尺寸 |",
        "|---|---|---|---|---|---|",
    ]
    seen = set()
    for r in sorted(ok_rows, key=lambda x: (-x["fps"], x["index"])):
        key = (r["index"], r["backend"], r["requested"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"| {r['index']} | {r['backend']} | {r['requested']} "
                     f"| {r['fourcc'] or '默认'} | {r['fps']} | {r['actual']} |")
    lines += [
        "",
        "## 完整探测表",
        "",
        "| index | 后端 | 请求尺寸 | fourcc | 结果 | 实际尺寸 | FPS | 备注 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['index']} | {r['backend']} | {r['requested']} "
                     f"| {r['fourcc'] or '默认'} | "
                     f"{'OK' if r['ok'] else 'FAIL'} | {r['actual'] or '-'} "
                     f"| {r['fps']} | {r['note']} |")
    path = out / "camera_capability.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def cmd_probe(args):
    rows = probe_capabilities(max_index=args.max_index, out_dir=args.out)
    path = write_capability_md(rows, out_dir=args.out)
    ok = [r for r in rows if r["ok"]]
    print(f"探测完成:{len(ok)}/{len(rows)} 组合可用 → {path}")
    for r in sorted(ok, key=lambda x: -x["fps"])[:5]:
        print(f"  index={r['index']} {r['backend']} {r['requested']} "
              f"fourcc={r['fourcc'] or '默认'} fps={r['fps']} "
              f"actual={r['actual']}")

def cmd_self_test(args):
    cap = CameraCapture(src=args.index, width=args.width, height=args.height,
                        fourcc=args.fourcc, out_dir=args.out)
    if not cap.start():
        print(f"FAIL: 相机 index={args.index} 打不开: {cap._open_error}")
        return 1
    t0 = time.perf_counter()
    n = 0
    last_shape = None
    while n < args.n:
        item = cap.read(timeout=2)
        if item is None:
            print(f"FAIL: 第 {n} 帧后取帧超时/EOF(共采 {cap._frames_captured})")
            cap.stop()
            return 1
        gray, _, _, _ = item
        last_shape = gray.shape
        n += 1
    elapsed = time.perf_counter() - t0
    cap.stop()
    stats = cap.stats()
    print(f"自测通过:{n} 帧 / {elapsed:.1f}s,交付 FPS={n / elapsed:.1f},"
          f"尺寸={last_shape},丢帧={stats['frames_dropped']}")
    return 0

def cmd_record_video(args):
    cap = CameraCapture(src=args.index, width=args.width, height=args.height,
                        fourcc=args.fourcc, out_dir=args.out)
    if not cap.start():
        print(f"FAIL: 相机 index={args.index} 打不开: {cap._open_error}")
        return 1
    path = cap.record_seed_video(seconds=args.seconds)
    cap.stop()
    if path:
        print(f"已录制种子视频:{path}")
        return 0
    print("FAIL: 未录到任何帧")
    return 1

def main():
    sys_out = __import__("sys")
    sys_out.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="live_stream 摄像头采集/探测")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="探测摄像头能力并写 camera_capability.md")
    p_probe.add_argument("--max-index", type=int, default=5)
    p_probe.add_argument("--out", default=str(DEFAULT_OUT))
    p_probe.set_defaults(func=cmd_probe)

    p_test = sub.add_parser("self-test", help="连续采集 N 帧自测")
    p_test.add_argument("--n", type=int, default=100)
    p_test.add_argument("--index", type=int, default=0)
    p_test.add_argument("--width", type=int, default=640)
    p_test.add_argument("--height", type=int, default=480)
    p_test.add_argument("--fourcc", default="MJPG")
    p_test.add_argument("--out", default=str(DEFAULT_OUT))
    p_test.set_defaults(func=cmd_self_test)

    p_rec = sub.add_parser("record-video", help="录制演示种子视频")
    p_rec.add_argument("--seconds", type=float, default=15)
    p_rec.add_argument("--index", type=int, default=0)
    p_rec.add_argument("--width", type=int, default=640)
    p_rec.add_argument("--height", type=int, default=480)
    p_rec.add_argument("--fourcc", default="MJPG")
    p_rec.add_argument("--out", default=str(DEFAULT_OUT))
    p_rec.set_defaults(func=cmd_record_video)

    args = parser.parse_args()
    return args.func(args)

if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
