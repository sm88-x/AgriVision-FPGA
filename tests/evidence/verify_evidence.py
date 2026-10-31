"""只读核验：python verify_evidence.py --project ."""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import argparse
import ast
import csv
import hashlib
import re
import statistics
from pathlib import Path

import numpy as np

def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    root = parser.parse_args().project.resolve()
    here = Path(__file__).resolve().parent
    manifest = rows(here / "source_manifest.csv")
    for item in manifest:
        source = root / item["source"]
        assert source.is_file(), f"缺少来源文件：{source}"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == item["sha256"], f"来源已更新：{source}；请重新核验材料"
    groups = rows(here / "performance_summary.csv")
    count = 0
    for group in groups:
        sample = [r for r in rows(root / group["source"]) if r["backend"] == group["backend"]]
        values = [float(r["ms"]) for r in sample]
        assert len(values) == int(group["runs"]) == 20
        assert all(r["correct"] == "PASS" and int(r["different_pixels"]) == 0 for r in sample)
        assert abs(statistics.median(values) - float(group["median"])) < 1e-8
        count += len(sample)
    assert len(groups) == 14 and count == 280
    regression = rows(root / "data/evidence/board/application_regression/application_regression.csv")
    hardware = [r for r in regression if r["backend"] == "FPGA"]
    assert len(regression) == 12 and len(hardware) == 11
    assert all(r["golden"] == "PASS" and int(r["different_pixels"]) == 0 for r in hardware)
    # 仅提取纯 NumPy 参考函数，不导入或运行板端初始化代码。
    tree = ast.parse((root / "hardware/drivers/board_benchmark.py").read_text(encoding="utf-8-sig"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "edge_reference")
    namespace = {"np": np}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "reference", "exec"), namespace)
    cases = 0
    for source in sorted((root / "data/fixtures/images").glob("plant_*.bin")):
        match = re.search(r"(\d+)x(\d+)", source.stem)
        if not match:
            continue
        width, height = map(int, match.groups())
        pixels = np.frombuffer(source.read_bytes(), dtype=np.uint8).reshape(height, width)
        for algorithm, suffix in [("sobel", "edge_hw"), ("prewitt", "edge_prewitt")]:
            expected = np.frombuffer((root / f"data/fixtures/golden/{source.stem}_{suffix}.bin").read_bytes(), dtype=np.uint8)
            actual = namespace["edge_reference"](pixels, algorithm, 0)
            assert not any(np.any(v) for v in [actual[0], actual[-1], actual[:, 0], actual[:, -1]])
            if len(expected) == (width - 2) * (height - 2):
                actual = actual[1:-1, 1:-1]
            assert np.array_equal(expected, actual.ravel()), f"Golden 不一致：{source.name}/{algorithm}"
            cases += 1
    assert cases == 12
    print(f"PASS：{len(manifest)} 个来源哈希；14 组/280 行性能记录；12 行应用回归（11 行 FPGA PASS）；12 个离线 Golden 用例。")
    print("本脚本只读文件，不加载 bitstream，不代表本次重新上板测试。")

if __name__ == "__main__":
    main()
