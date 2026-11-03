# scene_FINISH_V1
# -*- coding: utf-8 -*-
from __future__ import annotations
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src").is_dir())
for _layout_rel in ("src", "src/app", "src/adapters",
                    "src/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import argparse
import copy
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src/app"))

import cv2
import numpy as np

from algorithm.adaptive.adaptive_split_v2 import SeedSeparatorV2, DETECTOR_CONF_V2
from fpga.scene_policy import scene_policy as policy

CONTROL = ROOT / "outputs/config/scene_control.json"
DEFAULT_CONTROL = ROOT / "data/evidence/demo/scene_control.json"
FROZEN = ROOT / "data/evidence/segmentation_comparison/frozen.json"
WEIGHTS = ROOT / "data/models/best.pt"

EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

def existing_dir(path):
    path = Path(path).resolve()
    if not path.is_dir():
        raise ValueError(f"输出目录必须已经存在：{path}")
    if not path.is_relative_to(ROOT):
        raise ValueError(f"输出目录必须位于工程内部：{path}")
    return path

def atomic_json(path, value):
    path = Path(path)
    existing_dir(path.parent)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()

def image_write(path, image):
    path = Path(path)
    existing_dir(path.parent)
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"图像编码失败：{path}")
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_bytes(data.tobytes())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()

def image_read(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图片：{path}")
    return image

def control():
    path = CONTROL if CONTROL.exists() else DEFAULT_CONTROL
    if not path.exists():
        return {
            "mode": "manual",
            "setup_id": "",
            "revision": "",
        }
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("mode") not in ("auto", "manual"):
        raise ValueError("scene_control.json 的 mode 必须为 auto/manual")
    return value

def cmp_object(obj):
    result = dict(obj)
    result["bbox"] = obj["bbox_xyxy_src"]
    result["contour"] = obj.get("contour_xy_src", [])
    return result

class Engine:
    def __init__(self):
        frozen = json.loads(FROZEN.read_text(encoding="utf-8-sig"))
        self.hw = tuple(frozen["image_hw"])
        self.frozen = frozen
        self.v2_by_species = {}

        # 与 B 的既有基线共用冻结检测器参数。
        # 三路使用同一份 prediction，保证本次比较检测输入一致。
        self.separator = SeedSeparatorV2(
            copy.deepcopy(frozen), weights=WEIGHTS
        )

    def process(self, bgr, frame_id, species, out_dir, session,
                board=None, gt=None):
        if species not in ("soybean", "mungbean", "redbean"):
            raise ValueError("请选择黄豆、绿豆或红豆")

        if tuple(bgr.shape[:2]) != self.hw:
            raise ValueError(
                f"B 当前冻结模型要求 {self.hw[1]}x{self.hw[0]}，"
                f"实际输入为 {bgr.shape[1]}x{bgr.shape[0]}。"
                "请使用原始分辨率，禁止静默缩放后套用旧标定。"
            )

        cfg_control = control()
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        t0 = time.perf_counter()
        if cfg_control["mode"] == "auto":
            cfg = policy.auto_config(policy.compute_frame_stats(rgb))
        else:
            cfg = policy.manual_config()
        decision_ms = (time.perf_counter() - t0) * 1000

        height, width = bgr.shape[:2]
        register_plan = [
            {"offset": f"0x{offset:02X}", "value": int(value)}
            for offset, value in policy.apply_config(
                cfg, width=width, height=height
            )
        ]

        receipt = {
            "frame_id": frame_id,
            "mode": cfg_control["mode"],
            "setup_id": cfg_control.get("setup_id", ""),
            "control_revision": cfg_control.get("revision", ""),
            "scene": cfg["scene"],
            "config_id": cfg["config_id"],
            "requested_config": cfg,
            "requested_registers": register_plan,
            "actual_registers": None,
            "applied_frame": None,
            "register_verified": False,
            "ps_decision_ms": decision_ms,
            "fpga_dma_ms": None,
            "transport_rtt_ms": None,
            "hardware": False,
        }

        pl = None
        if board:
            from scene_board_client import process_board
            pl, board_receipt = process_board(
                bgr, frame_id, board, cfg_control["mode"]
            )
            receipt["local_candidate_config"] = receipt.pop(
                "requested_config"
            )
            receipt["local_candidate_registers"] = receipt.pop(
                "requested_registers"
            )
            receipt.update(board_receipt)
        else:
            receipt["receipt_note"] = "本机算法路径，FPGA 未参与"

        started = time.perf_counter()
        prediction = self.separator.predict(
            bgr, frame_id=frame_id, species=species
        )

        v2_conf = float(DETECTOR_CONF_V2.get(species,self.frozen["detector"]["conf"]))
        if abs(v2_conf-float(self.frozen["detector"]["conf"])) < 1e-9:
            adaptive_separator = self.separator
            adaptive_prediction = prediction
        else:
            if species not in self.v2_by_species:
                frozen_v2 = copy.deepcopy(self.frozen)
                frozen_v2["detector"]["conf"] = v2_conf
                self.v2_by_species[species] = SeedSeparatorV2(frozen_v2, weights=WEIGHTS)
            adaptive_separator = self.v2_by_species[species]
            adaptive_prediction = adaptive_separator.predict(bgr,frame_id=frame_id,species=species)
        results = {}
        for label, mode in (
            ("yolo", "baseline_0"),
            ("fixed", "baseline_1"),
            ("adaptive", "proposed_v2"),
        ):
            separator = adaptive_separator if label == "adaptive" else self.separator
            selected_prediction = adaptive_prediction if label == "adaptive" else prediction
            results[label] = separator.detect_and_separate(
                bgr,
                mode,
                species,
                session_id=session,
                frame_id=frame_id,
                image_id=f"{session}_{frame_id}",
                gt_count=gt,
                prediction=selected_prediction,
            )

        analysis_wall_ms = (time.perf_counter() - started) * 1000

        stem = f"scene_{session}_{frame_id:08d}"
        raw_path = out_dir / f"{stem}_raw.png"
        image_write(raw_path, bgr)

        pl_path = None
        if pl is not None:
            pl_path = out_dir / f"{stem}_pl.png"
            image_write(pl_path, pl)

        adaptive = results["adaptive"]
        frame = {
            "frame_id": frame_id,
            "session_id": session,
            "width": width,
            "height": height,
            "image_format": "BGR8",
            "species": species,
            "setup_id": cfg_control.get("setup_id", ""),
            "image_file": str(raw_path),
            "fpga_file": str(pl_path) if pl_path else "",
            "seed_count": adaptive["final_count"],
            "objects": adaptive["objects"],
            "scene": receipt,
        }

        modes = {}
        for label, result in results.items():
            modes[label] = {
                "count": result["final_count"],
                "abs_error": result["absolute_error"],
                "objects": [cmp_object(o) for o in result["objects"]],
            }

        timings = {
            "yolo": prediction["detector_wall_ms"],
            "fixed": results["fixed"]["timings_ms"]["separation_ms"],
            "adaptive": adaptive["timings_ms"]["separation_ms"],
            "analysis_wall": analysis_wall_ms,
        }
        for label, result in results.items():
            timings["total_" + label] = result["timings_ms"]["total_ms"]

        comparison = {
            "image_id": f"{session}_{frame_id}",
            "frame_id": frame_id,
            "file": str(raw_path),
            "species": species,
            "gt_count": gt,
            "gt_source": "manual" if gt is not None else "",
            "status": (
                "fail" if adaptive["status"] == "UNRESOLVED"
                or (gt is not None and adaptive["final_count"] != gt)
                else "ok"
            ),
            "notes": (
                "分离算法输入为原始 BGR 图；PL 输出仅单独展示，"
                "未声称分离算法使用了 PL 输出"
            ),
            "modes": modes,
            "timings_ms": timings,
            "scene": receipt,
            "detector_parameters": {
                "baseline":prediction.get("effective_parameters",{}),
                "adaptive":adaptive_prediction.get("effective_parameters",{}),
            },
        }
        return frame, comparison

def comparison_document(images):
    return {
        "schema": "agrivision.segmentation.comparison.v1",
        "source": "real",
        "source_note": "真实输入和 B 的算法输出；未使用占位检测结果",
        "mode_labels": {
            "yolo": "原 YOLO",
            "fixed": "固定分水岭",
            "adaptive": "自适应 V2",
        },
        "images": images,
    }

def preprocess_frame(bgr, frame_id, species, out_dir, session, board):
    """No detector or separator: return only the current image and optional verified PL output."""
    cfg = control()
    receipt = {"hardware":False,"simulated":False,"frame_id":frame_id,
               "receipt_note":"本机图像查看，未启用检测或板端处理"}
    pl = None
    if board:
        from scene_board_client import process_board
        pl, receipt = process_board(bgr, frame_id, board, cfg["mode"])
    raw_path = out_dir / f"preprocess_{session}_{frame_id:08d}_raw.png"
    image_write(raw_path, bgr)
    pl_path = out_dir / f"preprocess_{session}_{frame_id:08d}_pl.png"
    if pl is not None:image_write(pl_path, pl)
    return {"frame_id":frame_id,"session_id":session,"width":bgr.shape[1],"height":bgr.shape[0],
            "image_format":"BGR8","species":species,"setup_id":cfg.get("setup_id",""),
            "image_file":str(raw_path),"fpga_file":str(pl_path) if pl is not None else "",
            "seed_count":0,"objects":[],"scene":receipt,"analysis_enabled":False}


def offline(args):
    out = Path(args.out).resolve() if args.out else None
    pheno = Path(args.pheno_out).resolve() if args.pheno_out else None
    if not out and not pheno:
        raise ValueError("至少指定 --out 或 --pheno-out")
    for path in (out, pheno):
        if path:
            existing_dir(path.parent)

    image_dir = (pheno or out).parent
    if args.directory:
        paths = sorted(
            p for p in Path(args.directory).iterdir()
            if p.is_file() and p.suffix.lower() in EXTENSIONS
        )
        if args.gt is not None:
            raise ValueError("Batch 不能使用一个统一 GT")

        # 工程现有 segmentation 数据集混合三品种。
        # 有标准品种前缀时，只分析当前选择的品种。
        prefixes = ("soybean_", "mungbean_", "redbean_")
        if not args.preprocess_only and any(p.name.startswith(prefixes) for p in paths):
            paths = [
                p for p in paths
                if p.name.startswith(args.species + "_")
            ]
    elif args.image:
        paths = [Path(args.image)]
    else:
        raise ValueError("需要 --image 或 --dir")

    if not paths:
        raise ValueError("没有可处理的图片")
    if args.frame_id is not None and len(paths) != 1:
        raise ValueError("--frame-id 仅能用于单张图片")

    if args.preprocess_only and out:
        raise ValueError("仅预处理时请使用 --pheno-out 保存图像与回执，不生成分离对比")
    engine = None if args.preprocess_only else Engine()
    session = uuid.uuid4().hex[:12]
    frames, images = [], []

    for index, path in enumerate(paths):
        frame_id = args.frame_id if args.frame_id is not None else index
        if engine is None:
            frame = preprocess_frame(image_read(path), frame_id, args.species, image_dir, session, args.board)
        else:
            frame, comparison = engine.process(
                image_read(path), frame_id, args.species,
                image_dir, session, board=args.board, gt=args.gt,
            )
            images.append(comparison)
        frames.append(frame)
        print(json.dumps({
            "progress": index + 1,
            "total": len(paths),
            "file": str(path),
            "frame_id": frame_id,
        }, ensure_ascii=False), flush=True)

    if pheno:
        atomic_json(pheno, {
            "schema": "agrivision.scene.phenotyping.v1",
            "frames": frames,
        })
    if out:
        atomic_json(out, comparison_document(images))

def live(request_path):
    req = json.loads(
        Path(request_path).read_text(encoding="utf-8-sig")
    )
    out_dir = existing_dir(req["output_dir"])
    stop = out_dir / "stop.request"
    status_path = out_dir / "live_status.json"

    engine = Engine()
    session = uuid.uuid4().hex[:12]
    species = req.get("species", "soybean")
    source = req.get("source", "mock")
    frames_limit = int(req.get("frames") or 0)

    width = int(req.get("width", 640))
    height = int(req.get("height", 480))
    if (height, width) != engine.hw:
        raise ValueError("Live 分辨率必须与 B 当前冻结分辨率一致")

    board = None
    camera = None
    paths = []

    if source == "board":
        board = f'{req["host"]}:{int(req.get("port", 9000))}'
        camera = cv2.VideoCapture(int(req.get("camera_index", 0)))
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if not camera.isOpened():
            camera.release()
            raise RuntimeError("无法打开电脑摄像头")
    elif source == "mock":
        path = Path(req.get("input_dir") or req.get("image_path") or "")
        if path.is_file():
            paths = [path]
        elif path.is_dir() and str(path) != ".":
            paths = sorted(
                p for p in path.iterdir()
                if p.is_file() and p.suffix.lower() in EXTENSIONS
            )
        if not paths:
            raise ValueError("本地回放需要先选择真实图片或图片目录")
    else:
        raise ValueError("本适配层仅支持 board/mock")

    status = {}
    started = time.perf_counter()
    index = 0
    try:
        while not stop.exists() and (
            frames_limit <= 0 or index < frames_limit
        ):
            if camera:
                ok, bgr = camera.read()
                if not ok:
                    raise RuntimeError("摄像头读取失败")
            else:
                bgr = image_read(paths[index % len(paths)])

            frame, comparison = engine.process(
                bgr, index, species, out_dir, session, board=board
            )
            base = f"scene_{session}_{index:08d}"
            pheno_path = out_dir / f"{base}_phenotyping.json"
            cmp_path = out_dir / f"{base}_comparison.json"

            # 先完成本帧不可变资产，最后发布状态文件。
            atomic_json(pheno_path, {"frames": [frame]})
            atomic_json(cmp_path, comparison_document([comparison]))

            receipt = frame["scene"]
            elapsed = max(time.perf_counter() - started, 1e-6)
            status = {
                "frame_id": index,
                "session_id": session,
                "running": True,
                "state": "LIVE",
                "camera": "ONLINE" if camera else "REPLAY",
                "fpga": (
                    "ONLINE" if receipt.get("hardware")
                    else "SIMULATED" if board else "NOT_USED"
                ),
                "dma": "RESPONSE_OK" if board else "NOT_USED",
                "preview": {
                    "cam": frame["image_file"],
                    "board": frame["fpga_file"],
                },
                "ai": {
                    "enabled": True,
                    "model_state": "REAL",
                    "seed_count": frame["seed_count"],
                    "infer_ms": comparison["timings_ms"]["yolo"],
                    "detections": [
                        {
                            "id": o["id"],
                            "confidence": o.get("confidence", 0.0),
                            "bbox_xyxy": o["bbox_xyxy_src"],
                        }
                        for o in frame["objects"]
                    ],
                },
                "latency": {
                    "rtt_ms": receipt["transport_rtt_ms"],
                },
                "fps": {"live": (index + 1) / elapsed},
                "scene": receipt,
                "scene_pheno_file": str(pheno_path),
                "scene_cmp_file": str(cmp_path),
            }
            atomic_json(status_path, status)
            index += 1

        status.update(
            running=False,
            state="STOPPED" if stop.exists() else "DONE",
        )
        atomic_json(status_path, status)
    except Exception as exc:
        status.update(running=False, state="ERROR", error=str(exc), preview={}, scene={}, latency={})
        atomic_json(status_path, status)
        raise
    finally:
        if camera:
            camera.release()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--image")
    inputs.add_argument("--dir", dest="directory")
    parser.add_argument("--out")
    parser.add_argument("--pheno-out")
    parser.add_argument("--species", default="soybean")
    parser.add_argument("--gt", type=int)
    parser.add_argument("--board")
    parser.add_argument("--frame-id", type=int)
    parser.add_argument("--preprocess-only", action="store_true", help="仅保存当前原图、可选 FPGA 图和回执，不加载检测模型")
    args = parser.parse_args()

    if args.request:
        live(args.request)
    else:
        offline(args)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({
            "ok": False, "error": str(exc)
        }, ensure_ascii=False), file=sys.stderr, flush=True)
        raise SystemExit(1)
