
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

BOARD_URL = os.environ.get(
    "AGRIVISION_BOARD_URL",
    "http://192.168.2.99:8765",
)

def save_image(output_dir, name, image_base64):
    output_path = output_dir / name
    output_path.write_bytes(
        base64.b64decode(image_base64)
    )
    return str(output_path)

def run_on_board(request):
    image_path = Path(request["image_path"])
    output_dir = Path(request["output_dir"])

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "width": request["width"],
        "height": request["height"],
        "threshold": request["threshold"],
        "edge_mode": request["edge_mode"],
        "morph_mode": request["morph_mode"],
        "image_base64": base64.b64encode(
            image_path.read_bytes()
        ).decode("ascii"),
    }

    data = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    http_request = urllib.request.Request(
        BOARD_URL + "/run",
        data=data,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            http_request,
            timeout=55,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except Exception as error:
        raise RuntimeError(
            "无法连接 PYNQ 板卡 "
            + BOARD_URL
            + "："
            + str(error)
        )

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "error",
                "板端返回失败",
            )
        )

    images = result.pop("images_base64")

    result["images"] = {
        "edge": save_image(
            output_dir,
            "edge.png",
            images["edge"],
        ),
        "morphology": save_image(
            output_dir,
            "morphology.png",
            images["morphology"],
        ),
        "annotated": save_image(
            output_dir,
            "annotated.png",
            images["annotated"],
        ),
    }

    return result

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--request",
        required=True,
    )

    args = parser.parse_args()

    try:
        request = json.loads(
            Path(args.request).read_text(
                encoding="utf-8"
            )
        )

        response = run_on_board(request)

        print(
            json.dumps(
                response,
                ensure_ascii=False,
                allow_nan=False,
            )
        )

        return 0

    except Exception as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": False,
                    "error": str(error),
                },
                ensure_ascii=False,
            )
        )

        return 1

if __name__ == "__main__":
    sys.exit(main())
