
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import json
from pathlib import Path
import cv2

root = Path(__file__).resolve().parents[1]
image_dir = root / "data/fixtures/agricultural_images"
output = root / "data/evidence/hardware/roi_definitions.json"

if output.exists():
    raise RuntimeError("ROI file exists; preserve it before selecting again")

records = []
for path in sorted(image_dir.glob("seed_*.png")):
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("Cannot read " + str(path))
    image = cv2.resize(image, (640, 480), interpolation=cv2.INTER_AREA)

    print("Select seeds in", path.name)
    print("SPACE/ENTER confirms a box; ESC finishes this image")
    boxes = cv2.selectROIs(
        "phenotyping ROI selection", image,
        showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()

    for x, y, w, h in boxes:
        if w <= 0 or h <= 0:
            continue
        records.append({
            "id": "ROI_{:03d}".format(len(records) + 1),
            "image": path.name,
            "width": 640,
            "height": 480,
            "bbox_xyxy": [int(x), int(y), int(x+w), int(y+h)],
            "source": "manual_hardware_validation",
        })

    if len(records) >= 10:
        break

if len(records) < 10:
    raise RuntimeError("Need at least 10 ROIs; selected " + str(len(records)))

output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(records, indent=2), encoding="utf-8")
print("Saved", output)
