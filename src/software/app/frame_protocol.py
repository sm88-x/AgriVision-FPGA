# -*- coding: utf-8 -*-
"""live_stream 帧传输协议定义(PC ↔ PYNQ-Z2,v1)。

权威文档:hardware/board/protocol.md;本文件与文档共用同一份定义,板端 frame_server.py
与 PC 侧客户端都 import 本模块,不另抄常量。修改字段必须先改本文件
并同步 hardware/board/protocol.md。

传输层:TCP 长连接,端口 9000,全小端。
一帧 = 固定 40 字节头 + payload(裸像素,无压缩、无额外长度前缀,
payload 长度在头内 payload_len 字段)。
收发纪律:先循环收满 40 字节头,校验 magic/version/payload_len,
再按 payload_len 循环收满 payload;绝不假设一次 recv 拿到整帧。
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

# ---------------------------------------------------------------- 常量
MAGIC = b"AGV1"
VERSION = 1
HEADER_SIZE = 40
HEADER_STRUCT = struct.Struct("<4sHHIIHHBBBBBBBBIII")   # calcsize == 40

# 帧类型(msg_type)
MSG_REQUEST = 1        # PC -> 板:处理一帧
MSG_RESPONSE = 2       # 板 -> PC:输出帧
MSG_ERROR = 3          # 板 -> PC:错误帧(40B 头 + UTF-8 文本 body)

# 通道数(channels):live_stream 实时链路固定 1(gray8);4=RGBX888 为 pipeline_validation 遗留路径
CH_GRAY = 1
CH_GRAY8 = 1
CH_RGB = 3
CH_RGBX = 4
ALLOWED_CHANNELS = (CH_GRAY, CH_RGB, CH_RGBX)

# flags 位
FLAG_ENABLE_GRAY = 0x01    # 仅作提示,板端以 channels 为准
FLAG_ENABLE_GAUSS = 0x02   # bit1=enable_gauss

# pipeline_mode
PIPELINE_EXPLICIT = 0      # 用头里各字段显式组合
PIPELINE_SEED_PRESET = 1   # Seed Preset(忽略其余字段):
                           # enable_gauss=1, Sobel, threshold=40, Dilate

# 尺寸域
MIN_W, MIN_H = 3, 3
MAX_W, MAX_H = 1920, 1080
MAX_PAYLOAD = MAX_W * MAX_H * CH_RGBX     # 1920*1080*4

# 错误码(status;详见 protocol.md 第四节)
ERR_OK = 0
ERR_BAD_MAGIC = 1
ERR_BAD_VERSION = 2
ERR_BAD_LENGTH = 3        # payload 长度不符 / 超上限 / reserved 非 0 / 类型不支持
ERR_BAD_SIZE = 4          # 宽高越界
ERR_BAD_CHANNELS = 5      # 通道数不在 1/3/4 内
ERR_TIMEOUT = 6           # DMA 或接收超时(不许当成像素错误)
ERR_DMA = 7               # DMA 内部错误位被置起(死锁嫌疑,单独计数,验收=0)
ERR_INTERNAL = 8          # 板端内部异常

ERROR_NAMES = {
    ERR_OK: "OK", ERR_BAD_MAGIC: "BAD_MAGIC", ERR_BAD_VERSION: "BAD_VERSION",
    ERR_BAD_LENGTH: "BAD_LENGTH", ERR_BAD_SIZE: "BAD_SIZE",
    ERR_BAD_CHANNELS: "BAD_CHANNELS", ERR_TIMEOUT: "TIMEOUT",
    ERR_DMA: "DMA", ERR_INTERNAL: "INTERNAL",
}

class ProtocolError(Exception):
    """协议层错误:带状态码,服务端据此回 ERROR 帧并分类统计。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)
        self.message = message

# ---------------------------------------------------------------- 打包/解析
def pack_message(msg_type, frame_id, timestamp_ms, width, height, channels,
                 mode=0, morph_mode=0, flags=0,
                 pipeline_mode=PIPELINE_EXPLICIT, threshold=0,
                 payload_len=0, status=ERR_OK):
    """打包 40 字节头。**不做校验**(错误帧可能带 width=0 等值,也必须能打包)。

    40B 布局:magic4s versionH msg_typeH frame_idI timestamp_msI
              widthH heightH channelsB modeB morph_modeB flagsB
              pipeline_modeB reserved×3B thresholdI payload_lenI statusI
    """
    return HEADER_STRUCT.pack(MAGIC, VERSION, msg_type, frame_id, timestamp_ms,
                              width, height, channels, mode, morph_mode,
                              flags, pipeline_mode, 0, 0, 0, threshold,
                              payload_len, status)

def unpack_message(header):
    """解析 40 字节头为 dict;头不足 40 字节按协议错误处理。"""
    if len(header) != HEADER_SIZE:
        raise ProtocolError(ERR_BAD_LENGTH,
                            "头长度 %d != %d" % (len(header), HEADER_SIZE))
    (magic, version, msg_type, frame_id, timestamp_ms, width, height,
     channels, mode, morph_mode, flags, pipeline_mode, r0, r1, r2,
     threshold, payload_len, status) = HEADER_STRUCT.unpack(header)
    return {"magic": magic, "version": version, "msg_type": msg_type,
            "frame_id": frame_id, "timestamp_ms": timestamp_ms,
            "width": width, "height": height, "channels": channels,
            "mode": mode, "morph_mode": morph_mode, "flags": flags,
            "pipeline_mode": pipeline_mode, "reserved": (r0, r1, r2),
            "threshold": threshold, "payload_len": payload_len,
            "status": status}

def validate(head, payload_len, expected_msg_type):
    """校验已解析的头;通过则返回 payload_len,否则抛 ProtocolError。

    head 为 unpack_message 返回的 dict;expected_msg_type 为期望的 msg_type
    (板端只认 MSG_REQUEST,PC 只认 MSG_RESPONSE)。
    错误帧(MSG_ERROR)不经过本函数——其 width/height 可能为 0。
    """
    if head["magic"] != MAGIC:
        raise ProtocolError(ERR_BAD_MAGIC, "魔数 %r != %r"
                            % (head["magic"], MAGIC))
    if head["version"] != VERSION:
        raise ProtocolError(ERR_BAD_VERSION,
                            "版本 %d 不支持(当前 %d)" % (head["version"], VERSION))
    if head["msg_type"] != expected_msg_type:
        raise ProtocolError(ERR_BAD_LENGTH,
                            "msg_type=%d 不支持(期望 %d)"
                            % (head["msg_type"], expected_msg_type))
    if head["reserved"] != (0, 0, 0):
        raise ProtocolError(ERR_BAD_LENGTH,
                            "reserved 非 0:%r" % (head["reserved"],))
    if not (MIN_W <= head["width"] <= MAX_W
            and MIN_H <= head["height"] <= MAX_H):
        raise ProtocolError(ERR_BAD_SIZE, "尺寸 %dx%d 越界(允许 %d~%d / %d~%d)"
                            % (head["width"], head["height"],
                               MIN_W, MAX_W, MIN_H, MAX_H))
    if head["channels"] not in ALLOWED_CHANNELS:
        raise ProtocolError(ERR_BAD_CHANNELS,
                            "channels=%d 不在 %s 内"
                            % (head["channels"], ALLOWED_CHANNELS))
    if payload_len > MAX_PAYLOAD:
        raise ProtocolError(ERR_BAD_LENGTH, "payload_len=%d 超上限 %d"
                            % (payload_len, MAX_PAYLOAD))
    expected = head["width"] * head["height"] * head["channels"]
    if payload_len != expected:
        raise ProtocolError(ERR_BAD_LENGTH,
                            "payload_len=%d != %dx%dx%d=%d"
                            % (payload_len, head["width"], head["height"],
                               head["channels"], expected))
    return payload_len
