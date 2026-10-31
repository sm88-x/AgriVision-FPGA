#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""network 电脑1 → 电脑2 网络帧协议（v1）。

注意：本文件不是板端那套 `frame_protocol.py`。
  - 本协议：电脑1 摄像头 → 电脑2 Python，TCP 默认 9100，magic "AGF1"
  - 板端协议：电脑2 → PYNQ-Z2，TCP 9000，magic "AGV1"
两条链路可以同时存在，端口和魔数都不同，日志里不要混写。

线上格式（全部大端）：
    [4B body_len][20B header][JPEG bytes]
    header = >4sIQHH
      magic         4s   b"AGF1"
      frame_id      I    发送端逐帧递增（1,2,3...）
      capture_ts_ns Q    发送端墙钟 time.time_ns()
      width         H    JPEG 实际宽度
      height        H    JPEG 实际高度

body_len = 20 + len(jpeg)。接收端必须先循环收满 4B 长度，再循环收满
body_len；绝不假设一次 recv 就能拿到整帧（TCP 会分包/粘包）。

协议说明文档：docs/frame_protocol.md
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import struct

MAGIC = b"AGF1"
VERSION = 1
HEADER = struct.Struct(">4sIQHH")
HEADER_SIZE = HEADER.size          # 20
LENGTH = struct.Struct(">I")
LENGTH_SIZE = LENGTH.size          # 4
DEFAULT_PORT = 9100

# 8 MiB 足够 1080p JPEG（通常 200~800 KB）；超过视为流损坏，直接断开重连。
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_JPEG_BYTES = MAX_FRAME_BYTES - HEADER_SIZE

class FrameProtocolError(ValueError):
    """帧长度/魔数/尺寸不自洽；流已不可信，调用方应断开连接。"""

def pack_body(frame_id, capture_ts_ns, width, height, jpeg):
    """打包 20B 头 + JPEG，返回不含 4B 长度前缀的 body。"""
    frame_id = int(frame_id)
    capture_ts_ns = int(capture_ts_ns)
    width = int(width)
    height = int(height)
    if not (0 <= frame_id <= 0xFFFFFFFF):
        raise ValueError(f"frame_id 越界: {frame_id}")
    if not (1 <= width <= 0xFFFF and 1 <= height <= 0xFFFF):
        raise ValueError(f"尺寸越界: {width}x{height}")
    if not (0 < len(jpeg) <= MAX_JPEG_BYTES):
        raise ValueError(f"JPEG 长度 {len(jpeg)} 不在 1..{MAX_JPEG_BYTES}")
    body = HEADER.pack(MAGIC, frame_id, capture_ts_ns, width, height) + jpeg
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"body 长度 {len(body)} 超过 {MAX_FRAME_BYTES}")
    return body

def pack_frame(frame_id, capture_ts_ns, width, height, jpeg):
    """打包完整线上包：4B 大端长度 + body。"""
    body = pack_body(frame_id, capture_ts_ns, width, height, jpeg)
    return LENGTH.pack(len(body)) + body

def parse_header(body):
    """解析并校验 body 头，返回 dict；不合法抛 FrameProtocolError。"""
    if len(body) < HEADER_SIZE:
        raise FrameProtocolError(
            f"body 只有 {len(body)}B，不足 {HEADER_SIZE}B 头")
    magic, frame_id, capture_ts_ns, width, height = HEADER.unpack_from(body, 0)
    if magic != MAGIC:
        raise FrameProtocolError(f"魔数 {magic!r} != {MAGIC!r}")
    if width == 0 or height == 0:
        raise FrameProtocolError(f"尺寸非法: {width}x{height}")
    jpeg_len = len(body) - HEADER_SIZE
    if jpeg_len <= 0:
        raise FrameProtocolError("JPEG 负载为空")
    return {
        "magic": magic,
        "version": VERSION,
        "frame_id": frame_id,
        "capture_ts_ns": capture_ts_ns,
        "width": width,
        "height": height,
        "jpeg_len": jpeg_len,
    }

def validate_body_len(body_len):
    """校验 4B 长度前缀；不合法抛 FrameProtocolError。"""
    body_len = int(body_len)
    if body_len < HEADER_SIZE:
        raise FrameProtocolError(
            f"body_len={body_len} 小于头长度 {HEADER_SIZE}")
    if body_len > MAX_FRAME_BYTES:
        raise FrameProtocolError(
            f"body_len={body_len} 超过上限 {MAX_FRAME_BYTES}")
    return body_len
