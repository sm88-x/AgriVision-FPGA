# 性能与证据索引

所有路径以项目根目录为基准。正式性能只使用 `data/evidence/board/board_benchmark/r1~r6_*.csv`，不与早期不同计时口径混合。

## 正式性能

| 尺寸 | 算法 | PS ms | PL E2E ms | PL FPS | 加速比 |
|---|---|---:|---:|---:|---:|
| 256x256 | Sobel | 39.5652 | 1.2087 | 827.37 | 32.73 |
| 256x256 | Prewitt | 33.9318 | 1.2144 | 823.42 | 27.94 |
| 320x240 | Sobel | 47.0606 | 1.4489 | 690.20 | 32.48 |
| 320x240 | Prewitt | 36.9306 | 1.4476 | 690.80 | 25.51 |
| 640x480 | Sobel | 216.3076 | 3.6066 | 277.27 | 59.98 |
| 640x480 | Prewitt | 179.1900 | 3.5903 | 278.53 | 49.91 |

每个配置 20 条记录，预热次数按现有测量脚本为 2。均值、中位数、最小值、最大值及来源见 `performance_summary.csv`。FPS=1000/中位数毫秒，加速比=同组 PS 中位数/PL 中位数。

六个原始文件合计 **280 条计时记录**：PS 120 条、PL 160 条，共 14 个配置。原有说明中的“320 次传输”与 CSV 不符。本材料以逐行统计结果为准。

计时脚本 `hardware/drivers/board_benchmark.py` 的 `measure_pl` 在计时前保存一次结果用于对拍，随后将该 verdict 附到各次耗时记录。不得把 160 条 PL 耗时解释为 160 次独立逐像素验证。新应用回归 CSV 的 11 次 FPGA 运行分别记录 Golden PASS 和 different_pixels=0。

## 核心证据映射

| 结论 | 原始文件与定位 |
|---|---|
| V0/V3 加速 5.83 倍 | `data/evidence/board/board_benchmark/r1_256_sobel.csv`，backend=v0/v3，ms 中位数 |
| 640×480 Sobel 59.98 倍 | `data/evidence/board/board_benchmark/r4_640_sobel.csv`，backend=ps/v3p |
| 三尺寸双算法 | r1~r6 文件的 resolution、algorithm 与 backend 列 |
| Achieved II=1 | `hardware/build/reports/sobel_v3_param/sobel_v3_param_pipeline_stream_pixels_csynth.rpt`，STREAM_PIXELS 的 achieved 列 |
| LUT 3345、FF 4200、BRAM 3.5、DSP 3 | 同目录 `sobel_v3_param_utilization.rpt`，全设计实现资源 |
| WNS +1.105 ns | 同目录 `sobel_v3_param_timing.rpt`，Design Timing Summary |
| RTL 联合仿真 Pass | 同目录 `sobel_v3_param_cosim.rpt`，Verilog Status |
| 应用 11 次 FPGA PASS | `data/evidence/board/application_regression/application_regression.csv`，backend=FPGA |
| 种子连跑 5 次 Count=42 | 同 CSV，case=D1#1~D1#5 |
| 图像结果 | `docs/evidence/screenshots/result_*_sobel_annotated.png` |
| 接口和帧协议 | `hardware/hls/parameterized/src/sobel_pipelined.cpp` 与 `.hwh` |

## 计时和测量边界

PL 计时从驱动内部开始，包含控制寄存器配置、DMA 提交与完成轮询。Dashboard 的显式 `inp.flush()` 和 `out.invalidate()` 位于 `run_frame` 外，不能将其全部计入驱动返回时间。PL E2E 不是从读图到表型输出的总耗时。PS 基线是仓库内 NumPy 参考实现。

最新应用记录有 12 行：6 个 FPGA 场景/算法组合、1 个 PS 对照、5 次 FPGA 稳定性重复。种子 PS/FPGA 的统计结果相同，但 CSV 不含完整输出数组，不能仅凭这些标量宣称表型图逐字节相同。叶片两算法计数均为 7，面积与周长并不相同。

HLS 的资源为核级估计，Vivado 表为全设计实现结果，不混用。源码尺寸上限不代表已对整个范围穷举验证。`source_manifest.csv` 提供引用文件 SHA-256，能锁定检查时的文件内容；指纹相同不证明 bit、源码来自同一次构建，构建追溯仍需工程侧记录。

