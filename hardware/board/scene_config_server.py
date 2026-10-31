# -*- coding: utf-8 -*-
"""scene 配置与图像联调服务。

复用工程已有 FrameService、V4 驱动及 A 的 scene_policy。
单线程串行处理：一帧处理、寄存器读回完成后才接下一帧。
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
import hashlib
import json
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 同时支持工程目录结构及板端已有的平铺目录。
for folder in (
    HERE,
    HERE / "overlay",
    HERE.parent / "src/software/app",
    HERE.parent / "src/software/fpga/scene_policy",
):
    if folder.is_dir():
        sys.path.insert(0, str(folder))

import numpy as np
import scene_policy as policy
from frame_service import FrameService

def receive_exact(stream, count):
    parts = []
    while count:
        data = stream.read(min(count, 65536))
        if not data:
            raise ValueError("请求图像数据不完整")
        parts.append(data)
        count -= len(data)
    return b"".join(parts)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            if self.path != "/frame":
                raise ValueError("接口路径必须为 /frame")

            width = int(self.headers["X-Width"])
            height = int(self.headers["X-Height"])
            frame_id = int(self.headers["X-Frame-Id"])
            request_id = self.headers["X-Request-Id"]
            mode = self.headers["X-Control-Mode"]

            if mode not in ("auto", "manual"):
                raise ValueError("模式必须为 auto/manual")
            if not request_id or len(request_id) > 128:
                raise ValueError("request_id 无效")
            if not 0 <= frame_id < 2**31:
                raise ValueError("frame_id 无效")
            if not (3 <= width <= 1920 and 3 <= height <= 1080):
                raise ValueError("输入尺寸超出允许范围")

            count = int(self.headers["Content-Length"])
            if count != width * height * 4:
                raise ValueError("RGBX 图像长度与尺寸不一致")

            payload = receive_exact(self.rfile, count)

            # 小端 uint32：R 位于 23:16，G 位于 15:8，B 位于 7:0。
            words = np.frombuffer(payload, dtype="<u4")
            rgb = np.stack(
                [
                    (words >> 16) & 255,
                    (words >> 8) & 255,
                    words & 255,
                ],
                axis=-1,
            ).reshape(height, width, 3).astype(np.uint8)

            t0 = time.perf_counter()
            if mode == "auto":
                config = policy.auto_config(
                    policy.compute_frame_stats(rgb)
                )
            else:
                config = policy.manual_config()
            decision_ms = (time.perf_counter() - t0) * 1000

            service = self.server.service

            # FrameService 对 RGBX 输入设置 enable_gray=1，
            # 且固定 out_sel=0；当前 A 的各场景配置均符合此约定。
            if int(config["enable_gray"]) != 1:
                raise ValueError("当前接口要求 enable_gray=1")
            if int(config["out_sel"]) != 0:
                raise ValueError("当前接口要求 out_sel=0")

            wanted = {
                "width": width,
                "height": height,
                "mode": int(config["edge_mode"]),
                "threshold": int(config["threshold"]),
                "morph_mode": int(config["morph_mode"]),
                "enable_gray": 1,
                "enable_gauss": int(config["enable_gauss"]),
                "out_sel": 0,
            }

            # 真板必须能从 HWH 找到全部参数寄存器。
            if not service.simulated:
                missing = [
                    name for name in wanted
                    if name not in service.driver.regs
                ]
                if missing:
                    raise RuntimeError(
                        "HWH 缺少寄存器：" + ", ".join(missing)
                    )

            result = service.process(
                payload,
                width,
                height,
                4,
                mode=wanted["mode"],
                morph_mode=wanted["morph_mode"],
                threshold=wanted["threshold"],
                enable_gauss=bool(wanted["enable_gauss"]),
                pipeline_mode=0,
                frame_id=frame_id,
            )

            actual = None
            verified = False

            if not service.simulated:
                actual = []
                mismatches = []

                # HTTPServer 串行运行，读回时不会插入其他帧。
                for name, expected in wanted.items():
                    offset = service.driver.regs[name]
                    value = int(service.driver.ctrl.read(offset))
                    actual.append({
                        "name": name,
                        "offset": "0x%02X" % offset,
                        "value": value,
                    })
                    if value != expected:
                        mismatches.append(
                            "%s: expected=%s actual=%s"
                            % (name, expected, value)
                        )

                if mismatches:
                    raise RuntimeError(
                        "寄存器读回不一致：" + "; ".join(mismatches)
                    )
                verified = True

            # 仅保留可直接序列化的配置主字段。
            applied = {
                key: int(config[key])
                for key in (
                    "config_id", "enable_gray", "enable_gauss",
                    "edge_mode", "threshold", "morph_mode", "out_sel",
                )
            }
            applied["scene"] = str(config["scene"])

            output = result["output"]
            meta = {
                "request_id": request_id,
                "frame_id": frame_id,
                "width": width,
                "height": height,
                "mode": mode,
                "scene": applied["scene"],
                "config_id": applied["config_id"],
                "applied_config": applied,
                "actual_registers": actual,
                "register_verified": verified,
                "applied_frame": frame_id if verified else None,
                "hardware": not service.simulated,
                "simulated": bool(service.simulated),
                "policy_version": getattr(
                    policy, "POLICY_VERSION", "unknown"
                ),
                "ps_decision_ms": decision_ms,
                # 既有驱动的 run_frame 耗时包含 DMA，
                # 因而这里不把它当作纯 PL 核心耗时。
                "fpga_dma_ms": result["pl_ms"],
                "board_service_ms": result["service_ms"],
                "overlay": result["overlay"],
                "input_sha256": hashlib.sha256(payload).hexdigest(),
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "receipt_note": (
                    "真板处理完成，8 个参数寄存器读回一致"
                    if verified else
                    "软件模拟：不能作为真板配置或性能验收"
                ),
            }

            encoded = json.dumps(
                meta, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            body = struct.pack("!I", len(encoded)) + encoded + output

            # 日志目录由启动参数指定，要求已经存在。
            with self.server.log_path.open(
                "a", encoding="utf-8"
            ) as log:
                log.write(json.dumps(meta, ensure_ascii=False) + "\n")

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except Exception as exc:
            body = json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--bitfile", required=True)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--log", default="scene_config_receipts.jsonl")
    args = parser.parse_args()

    log_path = Path(args.log).resolve()
    if not log_path.parent.is_dir():
        raise SystemExit("日志目录不存在；本程序不创建目录")

    if not args.mock:
        bitfile = Path(args.bitfile)
        if not bitfile.is_file():
            raise SystemExit("bit 文件不存在")
        if not bitfile.with_suffix(".hwh").is_file():
            raise SystemExit("缺少同名 HWH 文件")

    service = FrameService(
        bitfile=args.bitfile,
        mock=args.mock,
        timeout=10.0,
        verbose=True,
    )

    if service.simulated and not args.mock:
        service.close()
        raise SystemExit(
            "未连接真实 PYNQ，拒绝静默回退模拟。"
            "软件测试需明确添加 --mock。"
        )

    server = None
    try:
        server = HTTPServer((args.host, args.port), Handler)
        server.service = service
        server.log_path = log_path
        print(
            "scene 服务端口 %s；simulated=%s"
            % (args.port, service.simulated),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        service.close()

if __name__ == "__main__":
    main()
