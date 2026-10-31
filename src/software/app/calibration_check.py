#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""network B4：px→mm 标定复核（手工量像素 + 读取 scene 标定契约）。

不依赖圆检测/棋盘格，避免现场光照导致自动识别失败：

  1. 在同一拍摄平面放已知长度的参照物（如 1 元硬币直径 25.0 mm）；

  2. 在图上量出它的像素长度（Qt 看图/画图工具均可）；

  3. 输入像素长度和已知毫米数，本脚本算 px/mm，并与 scene 标定文件里的基线比较。

标定文件选择（与 Qt scene 页一致）：

  优先 data/calibration/scale_calibration/camera_calibration.json（当前有效标定，Qt scene 页生成），

  不存在时回退 data/calibration/scale_calibration/calibration.json（B 的契约/模板）；

  也可用 --calibration-json 显式指定。

用法（默认自动读取 scene 标定文件）：

  python src/software/app/calibration_check.py ^

      --px-length 174.01149387324966 --known-mm 50.0 ^

      --image-size 640x480 --setup-id camera_fixed --require-calibration

  python src/software/app/calibration_check.py ^

      --px-length 178.0 --known-mm 50.0 --expected-px-per-mm 3.4802

判定：

  · 标定文件 valid=true 且 px_per_mm 有数字时，自动作为基线；

  · 误差绝对值 <= --tolerance（默认 5%）→ PASS，固定高度/角度下旧参数可用；

  · 超过 5% → FAIL，重新标定；

  · 提供 --image-size 且与标定文件 scope.image_size 不一致 → 直接 FAIL

    （归一化工作图换了尺寸，旧比例不再成立）；

  · 提供 --setup-id 且与标定文件 setup_id 不一致 → 直接 FAIL

    （机位换了，旧比例不能跨姿态复用）；

  · 加 --angle-changed 或 --height-changed → 无论误差多少都判 FAIL；

  · 加 --require-calibration → 标定文件缺失或 valid=false 时直接 FAIL，

    用于“没有有效标定就禁止毫米显示”的启用门禁。

当前 data/calibration/scale_calibration/camera_calibration.json 是 valid=true / px_per_mm≈3.4802 的

有效标定（50 mm 钢尺、174.0115 px、640×480、中心 ROI、setup_id=camera_fixed）；

data/calibration/scale_calibration/calibration.json 仍是 valid=false 的契约模板，只在

camera_calibration.json 不存在时兜底。有效标定也只在该 ROI 与 setup_id 内允许 mm 显示。

"""

from __future__ import annotations
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

from datetime import datetime

from pathlib import Path

ROOT = PROJECT_ROOT

CALIBRATION_DIR = ROOT / "data/calibration/scale_calibration"

CALIBRATION_OWN = CALIBRATION_DIR / "camera_calibration.json"

CALIBRATION_CONTRACT = CALIBRATION_DIR / "calibration.json"

def parse_image_size(text: str) -> tuple[int, int]:

    """把 640x480 / 640X480 / 640*480 解析成 (w, h)。"""

    parts = str(text).lower().replace("*", "x").replace(" ", "").split("x")

    if len(parts) != 2:

        raise ValueError("--image-size 格式应为 640x480，收到 %r" % text)

    try:

        width, height = int(parts[0]), int(parts[1])

    except ValueError:

        raise ValueError("--image-size 里有非整数：%r" % text)

    if width <= 0 or height <= 0:

        raise ValueError("--image-size 必须为正数：%r" % text)

    return width, height

def load_calibration(path: Path) -> tuple[dict | None, str]:

    """读取 scene 标定文件；失败返回 (None, 原因)。"""

    try:

        data = json.loads(path.read_text(encoding="utf-8-sig"))

    except FileNotFoundError:

        return None, "标定文件不存在"

    except OSError as exc:

        return None, "标定文件读取失败: %s" % exc

    except json.JSONDecodeError as exc:

        return None, "标定文件不是合法 JSON: %s" % exc

    if not isinstance(data, dict):

        return None, "标定文件根节点不是 JSON 对象"

    return data, ""

def resolve_calibration_path(explicit: str | None) -> tuple[Path, str]:

    """与 Qt scene 页一致：C 的 camera_calibration.json 优先，B 的契约兜底。"""

    if explicit:

        return Path(explicit), "explicit"

    if CALIBRATION_OWN.is_file():

        return CALIBRATION_OWN, "auto_own"

    return CALIBRATION_CONTRACT, "auto_contract"

def is_number(value) -> bool:

    return isinstance(value, (int, float)) and not isinstance(value, bool)

def valid_roi(value) -> bool:

    """归一化 ROI 必须是 [x1,y1,x2,y2]，0<=x1<x2<=1、0<=y1<y2<=1。"""

    if not isinstance(value, (list, tuple)) or len(value) != 4:

        return False

    if not all(is_number(item) for item in value):

        return False

    x1, y1, x2, y2 = (float(item) for item in value)

    return 0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0

def run(args) -> int:

    if args.known_mm <= 0:

        print("[FAIL] --known-mm 必须大于 0", file=sys.stderr)

        return 2

    if args.px_length <= 0:

        print("[FAIL] --px-length 必须大于 0", file=sys.stderr)

        return 2

    try:

        image_size = (parse_image_size(args.image_size)

                      if args.image_size else None)

    except ValueError as exc:

        print("[FAIL] %s" % exc, file=sys.stderr)

        return 2

    measured = float(args.px_length) / float(args.known_mm)

    # ---- 读取 scene 标定契约（camera_calibration.json 优先） ----------------

    calibration = None

    calibration_note = ""

    calibration_path = None

    calibration_selection = "skipped"

    calibration_candidates = []

    if not args.no_calibration_json:

        calibration_path, calibration_selection = resolve_calibration_path(

            args.calibration_json)

        calibration, calibration_note = load_calibration(calibration_path)

        if calibration_selection.startswith("auto"):

            for candidate in (CALIBRATION_OWN, CALIBRATION_CONTRACT):

                calibration_candidates.append({

                    "path": str(candidate),

                    "exists": candidate.is_file(),

                })

    calib_valid = (calibration.get("valid") is True) if calibration else False

    calib_px = calibration.get("px_per_mm") if calibration else None

    calib_usable = (calib_valid

                    and is_number(calib_px)

                    and float(calib_px) > 0)

    calib_reason = ""

    if calibration is not None:

        calib_reason = str(calibration.get("reason") or "")

    else:

        calib_reason = calibration_note

    scope = (calibration or {}).get("scope")

    if not isinstance(scope, dict):

        scope = {}

    reference = (calibration or {}).get("reference")

    if not isinstance(reference, dict):

        reference = {}

    scope_size = scope.get("image_size")

    calib_id = str((calibration or {}).get("calibration_id") or "")

    calib_method = str((calibration or {}).get("method") or "")

    calib_setup = str((calibration or {}).get("setup_id") or "")

    roi = scope.get("working_roi_normalized")

    roi_px = None

    if (valid_roi(roi) and isinstance(scope_size, (list, tuple))

            and len(scope_size) == 2):

        width, height = int(scope_size[0]), int(scope_size[1])

        roi_px = [round(float(roi[0]) * width, 1),

                  round(float(roi[1]) * height, 1),

                  round(float(roi[2]) * width, 1),

                  round(float(roi[3]) * height, 1)]

    # 标定文件自身的数值一致性（reference 与 px_per_mm 是否对得上）

    ref_known = reference.get("known_length_mm")

    ref_measured = reference.get("measured_px")

    ref_scale = None

    ref_points_px = None

    if is_number(ref_known) and float(ref_known) > 0 and is_number(ref_measured):

        ref_scale = float(ref_measured) / float(ref_known)

    p1, p2 = reference.get("p1"), reference.get("p2")

    if (isinstance(p1, (list, tuple)) and isinstance(p2, (list, tuple))

            and len(p1) == 2 and len(p2) == 2

            and all(is_number(item) for item in (*p1, *p2))):

        ref_points_px = ((float(p2[0]) - float(p1[0])) ** 2

                         + (float(p2[1]) - float(p1[1])) ** 2) ** 0.5

    # ---- 基线来源：命令行优先，其次标定文件 -------------------------

    warnings = []

    expected = None

    baseline_source = "none"

    if args.expected_px_per_mm is not None:

        expected = float(args.expected_px_per_mm)

        baseline_source = "cli"

    elif calib_usable:

        expected = float(calib_px)

        baseline_source = "calibration_json"

    if calibration is None and not args.no_calibration_json:

        warnings.append("未读取到标定文件 %s（%s）"

                        % (calibration_path, calibration_note))

    if calibration is not None and not calib_valid:

        warnings.append("scene 标定文件 valid=false：%s"

                        % (calib_reason or "未给出原因"))

    if calibration is not None and calib_valid and not calib_usable:

        warnings.append("标定文件 valid=true，但 px_per_mm 不是正数，忽略该基线")

    if (args.expected_px_per_mm is not None and calib_usable

            and abs(float(args.expected_px_per_mm) - float(calib_px)) > 1e-6):

        warnings.append("命令行基线 %.4f 与标定文件 %.4f 不一致"

                        % (float(args.expected_px_per_mm), float(calib_px)))

    if (is_number(ref_known)

            and abs(float(ref_known) - float(args.known_mm)) > 0.5):

        warnings.append("本次参照长度 %.2f mm 与标定文件记录 %.2f mm 不同；"

                        "作为独立复核可以，但不要用同一张标定图自证"

                        % (float(args.known_mm), float(ref_known)))

    if image_size is None and isinstance(scope_size, (list, tuple)):

        warnings.append("未提供 --image-size，无法核对标定文件 scope.image_size；"

                        "请确认测量的是 640×480 归一化工作图")

    if (image_size is not None and calibration is not None

            and not isinstance(scope_size, (list, tuple))):

        warnings.append("标定文件未记录 scope.image_size，无法自动核对工作图尺寸")

    if calibration is not None and calib_usable:

        if ref_scale is not None:

            delta_pct = (ref_scale - float(calib_px)) / float(calib_px) * 100.0

            if abs(delta_pct) > 0.5:

                warnings.append("标定文件自身不一致：reference 算得 %.4f px/mm，"

                                "px_per_mm 写的是 %.4f（差 %+.2f%%）"

                                % (ref_scale, float(calib_px), delta_pct))

        if (ref_points_px is not None and is_number(ref_measured)

                and abs(ref_points_px - float(ref_measured)) > 0.5):

            warnings.append("标定文件自身不一致：p1/p2 距离 %.2f px，"

                            "reference.measured_px 写的是 %.2f px"

                            % (ref_points_px, float(ref_measured)))

    if calibration is not None and not valid_roi(roi) and roi is not None:

        warnings.append("标定文件 working_roi_normalized 格式不合法，"

                        "无法确认毫米比例的适用范围")

    if calib_setup and not args.setup_id:

        warnings.append("未提供 --setup-id，无法核对当前机位是否仍为 %s"

                        % calib_setup)

    if calibration_selection == "auto_own" and CALIBRATION_CONTRACT.is_file():

        other, _ = load_calibration(CALIBRATION_CONTRACT)

        other_px = (other or {}).get("px_per_mm")

        if (other and other.get("valid") and is_number(other_px)

                and float(other_px) > 0 and calib_usable

                and abs(float(other_px) - float(calib_px)) / float(calib_px)

                > 0.005):

            warnings.append("calibration.json 也是 valid=true（%.4f px/mm），"

                            "但当前按 Qt 规则使用 camera_calibration.json（%.4f px/mm）；"

                            "B 侧复核请用 --calibration-json 显式指定"

                            % (float(other_px), float(calib_px)))

    # ---- 失效判定 ---------------------------------------------------

    problems = []

    size_mismatch = False

    if (image_size is not None

            and isinstance(scope_size, (list, tuple))

            and len(scope_size) == 2):

        calibrated_size = [int(scope_size[0]), int(scope_size[1])]

        if calibrated_size != list(image_size):

            size_mismatch = True

            problems.append(

                "工作图尺寸 %dx%d 与标定 scope.image_size %dx%d 不一致："

                "旧 px/mm 作废，必须重新标定"

                % (image_size[0], image_size[1],

                   calibrated_size[0], calibrated_size[1]))

    setup_mismatch = False

    if args.setup_id and calibration is not None:

        if calib_setup and args.setup_id != calib_setup:

            setup_mismatch = True

            problems.append(

                "当前机位 setup_id=%s 与标定记录 %s 不一致：旧 px/mm 不可复用"

                % (args.setup_id, calib_setup))

        elif not calib_setup:

            warnings.append("标定文件未记录 setup_id，无法自动核对当前机位 %s"

                            % args.setup_id)

    if args.angle_changed or args.height_changed:

        changed = []

        if args.angle_changed:

            changed.append("相机角度")

        if args.height_changed:

            changed.append("相机高度")

        problems.append("%s变化：旧 px/mm 不可复用，必须重新标定"

                        % "、".join(changed))

    error_pct = None

    if expected is not None:

        error_pct = (measured - expected) / expected * 100.0

    notes = []

    if expected is None:

        if calibration is not None and not calib_valid:

            notes.append("标定文件未生效（valid=false），本次只记录测得的 px/mm；"

                         "展示层保持 px，不显示 mm")

        else:

            notes.append("没有可用基线，本次只记录测得的 px/mm")

    elif not problems:

        if abs(error_pct) <= float(args.tolerance):

            notes.append("同一高度/角度、同一机位下旧参数仍可用；"

                         "mm 只在标定 ROI 内有效")

        else:

            problems.append("误差超过 %.1f%%，重新标定"

                            % float(args.tolerance))

    if args.require_calibration and not calib_usable:

        problems.append("--require-calibration：标定文件缺失或 valid=false，"

                        "禁止毫米显示")

    passed = None

    if problems:

        passed = False

    elif expected is not None:

        passed = True

    reason = "；".join(problems or notes)

    # 只有标定契约本身有效时，才允许下游展示层把 px 换算成 mm。

    mm_display_allowed = bool(passed is True and calib_usable)

    print("已知长度        : %.3f mm" % float(args.known_mm))

    print("像素长度        : %.3f px" % float(args.px_length))

    print("测得 px/mm      : %.4f" % measured)

    if calibration_path is not None:

        print("标定文件        : %s（%s）"

              % (calibration_path,

                 "valid=true" if calib_valid else "valid=false"))

        if calibration_selection == "auto_own":

            print("标定选择        : 自动优先 camera_calibration.json")

        elif calibration_selection == "auto_contract":

            print("标定选择        : 自动回退 calibration.json（未见 camera_calibration.json）")

        elif calibration_selection == "explicit":

            print("标定选择        : 命令行 --calibration-json 显式指定")

        if calib_id:

            print("标定 ID         : %s" % calib_id)

        if calib_method:

            print("标定方法        : %s" % calib_method)

        if calib_setup:

            print("标定机位        : %s" % calib_setup)

        if valid_roi(roi):

            roi_text = ", ".join("%.2f" % float(value) for value in roi)

            if roi_px is not None and isinstance(scope_size, (list, tuple)):

                print("标定 ROI        : [%s]；%dx%d 下像素 [%.1f, %.1f]–[%.1f, %.1f]"

                      % (roi_text, int(scope_size[0]), int(scope_size[1]),

                         roi_px[0], roi_px[1], roi_px[2], roi_px[3]))

            else:

                print("标定 ROI        : [%s]" % roi_text)

            print("                  mm 只在上述 ROI 内有效；ROI 外或换机位必须重标")

        if not calib_valid and calib_reason:

            print("标定说明        : %s" % calib_reason)

    else:

        print("标定文件        : 已跳过（--no-calibration-json）")

    print("基线来源        : %s" % {

        "calibration_json": "标定文件",

        "cli": "命令行 --expected-px-per-mm",

        "none": "无",

    }[baseline_source])

    if expected is not None:

        print("基线 px/mm      : %.4f" % expected)

        print("误差            : %+.2f%%（容差 ±%.1f%%）"

              % (error_pct, float(args.tolerance)))

    if image_size is not None:

        print("工作图尺寸      : %dx%d" % image_size)

    print("判定            : %s" % ("PASS" if passed is True else

                                   ("FAIL" if passed is False else "仅记录")))

    print("说明            : %s" % reason)

    print("毫米显示        : %s"

          % ("允许" if mm_display_allowed else "禁止（保持 px / px²）"))

    for warning in warnings:

        print("警告            : %s" % warning)

    doc = {

        "schema": "agrivision.network.calibration.v1",

        "generated_at": datetime.now().isoformat(timespec="seconds"),

        "method": "manual_pixel_length",

        "image": args.image or "",

        "known_mm": float(args.known_mm),

        "px_length": float(args.px_length),

        "measured_px_per_mm": round(measured, 6),

        "expected_px_per_mm": (round(expected, 6)

                               if expected is not None else None),

        "baseline_source": baseline_source,

        "error_pct": (round(error_pct, 4)

                      if error_pct is not None else None),

        "tolerance_pct": float(args.tolerance),

        "pass": passed,

        "mm_display_allowed": mm_display_allowed,

        "angle_changed": bool(args.angle_changed),

        "height_changed": bool(args.height_changed),

        "height_mm": args.height_mm,

        "angle_deg": args.angle_deg,

        "image_size": list(image_size) if image_size else None,

        "image_size_matches": (None if image_size is None

                               or not isinstance(scope_size, (list, tuple))

                               else not size_mismatch),

        "calibration_selection": calibration_selection,

        "calibration_candidates": calibration_candidates,

        "setup_id": args.setup_id or None,

        "setup_id_matches": (None if not args.setup_id or calibration is None

                             else (not setup_mismatch if calib_setup else None)),

        "calibration": {

            "path": str(calibration_path) if calibration_path else "",

            "found": calibration is not None,

            "valid": calib_valid,

            "px_per_mm": (float(calib_px) if is_number(calib_px)

                          and float(calib_px) > 0 else None),

            "calibration_id": calib_id or None,

            "method": calib_method or None,

            "setup_id": calib_setup or None,

            "working_roi_normalized": (list(roi) if valid_roi(roi) else None),

            "working_roi_px": roi_px,

            "reason": calib_reason,

            "scope": scope or None,

            "reference": reference or None,

            "consistency": {

                "reference_px_per_mm": (round(ref_scale, 6)

                                        if ref_scale is not None else None),

                "reference_points_px": (round(ref_points_px, 6)

                                        if ref_points_px is not None else None),

                "px_per_mm_delta_pct": (

                    round((ref_scale - float(calib_px))

                          / float(calib_px) * 100.0, 4)

                    if ref_scale is not None and calib_usable else None),

            },

        },

        "warnings": warnings,

        "problems": problems,

        "note": args.note or "",

        "recommendation": reason,

    }

    if args.out:

        out = Path(args.out)

        out.parent.mkdir(parents=True, exist_ok=True)

        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1),

                       encoding="utf-8")

        print("写出            : %s" % out)

    if passed is False:

        return 3

    return 0

def main(argv: list[str] | None = None) -> int:

    ap = argparse.ArgumentParser(

        description=__doc__,

        formatter_class=argparse.RawDescriptionHelpFormatter)

    ap.add_argument("--px-length", type=float, required=True,

                    help="参照物在图像上的像素长度")

    ap.add_argument("--known-mm", type=float, required=True,

                    help="参照物真实长度（mm）")

    ap.add_argument("--expected-px-per-mm", type=float, default=None,

                    help="手工指定基线；优先级高于标定文件")

    ap.add_argument("--calibration-json", default=None,

                    help="指定 scene 标定文件；默认自动选择 "

                         "camera_calibration.json（优先）→ calibration.json")

    ap.add_argument("--no-calibration-json", action="store_true",

                    help="不读取标定文件，只用命令行参数")

    ap.add_argument("--require-calibration", action="store_true",

                    help="标定文件缺失或 valid=false 时直接判 FAIL（毫米显示门禁）")

    ap.add_argument("--tolerance", type=float, default=5.0,

                    help="允许误差百分比，默认 5%%")

    ap.add_argument("--image", default="", help="参照物图片路径（记录用）")

    ap.add_argument("--image-size", default="",

                    help="测量所用的归一化工作图尺寸，如 640x480")

    ap.add_argument("--setup-id", default="",

                    help="当前机位标识（如 camera_fixed）；与标定文件 setup_id "

                         "不一致时直接 FAIL")

    ap.add_argument("--height-mm", type=float, default=None,

                    help="相机镜头到拍摄平面的高度（mm，记录用）")

    ap.add_argument("--angle-deg", type=float, default=None,

                    help="相机俯仰角（度，记录用）")

    ap.add_argument("--angle-changed", action="store_true",

                    help="相机角度已变化：直接判 FAIL")

    ap.add_argument("--height-changed", action="store_true",

                    help="相机高度已变化：直接判 FAIL")

    ap.add_argument("--note", default="")

    ap.add_argument("--out", default=None,

                    help="把复核结果写成 JSON，如 results/network/calibration.json")

    args = ap.parse_args(argv)

    return run(args)

if __name__ == "__main__":

    try:

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    except AttributeError:

        pass

    raise SystemExit(main())

