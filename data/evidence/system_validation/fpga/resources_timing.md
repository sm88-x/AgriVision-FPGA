# V0 / V3 / V3-param / V4 资源与时序（system_validation A）

数据**直接取自仓库里已有的 Vivado / HLS 报告**，没有重新综合；

每行都标了来源文件，引用时请连同"历史实测"一起说明，不要当成该项实验新测的。

| 版本 | LUT（用/占） | FF（用/占） | BRAM（用/占） | DSP（用/占） | WNS（ns） | 时序 | 来源报告 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| V0 基线核 | 2727 / 5.13% | 3472 / 3.26% | 3.5 / 2.50% | 0 / 0% | **+1.579** | 满足 | `hardware/build/reports/sobel_v0/sobel_{utilization,timing}.rpt` |
| V3 流水核 | 2729 / 5.13% | 3515 / 3.30% | 3.5 / 2.50% | 0 / 0% | **+1.587** | 满足 | `hardware/build/reports/sobel_v3/sobel_v3_{utilization,timing}.rpt` |
| V3-param 参数化 | 3345 / 6.29% | 4200 / 3.95% | 3.5 / 2.50% | 3 / 1.36% | **+1.105** | 满足 | `hardware/build/reports/sobel_v3_param/sobel_v3_param_{utilization,timing}.rpt` |
| **V4（Candidate 2）** | 4701 / 8.84% | 5925 / 5.57% | 6.5 / 4.64% | 6 / 2.73% | **+1.299** | 满足 | `docs/evidence/integrated_pipeline/utilization_report.rpt`、`docs/evidence/integrated_pipeline/timing_report.rpt`、`docs/evidence/integrated_pipeline/drc_drc_1.rpt` |

## HLS（V4 流水线核，`docs/evidence/integrated_pipeline/csynth.rpt`）

| 项 | 值 |
| --- | --- |
| 目标时钟 | 10 ns（100 MHz） |
| 主循环 `STREAM_PIXELS` Achieved II | **1** |
| 主循环 Latency | 22 cycles（Slack +7.30 ns） |
| 流水段 Interval | 2,079,385 cycles ≈ 20.79 ms @100 MHz（HLS 估计，按 640×480 配置） |
| HLS 估计资源 | BRAM 9 (3%) / DSP 5 (2%) / FF 4645 (4%) / LUT 6718 (12%) |

> 注意：HLS 估计与实现后（上表）数值不同是正常的（前者是综合估计，后者是 Vivado 实现后统计）。

> **动态旁路/参数切换不会改变已实现硬件的 LUT/FF/BRAM 数量**，不能写成"动态节省逻辑资源"。

## 性能对照（历史实测，明细见 `benchmark_v0_v3_v4.csv`）

640×480 / Sobel / 每配置预热 3 次、正式 20~30 次，单位 ms（median）：

| 版本 | median ms | 相对 PS 软件 |
| --- | ---: | ---: |
| PS 软件（V0 参考） | 216.308 | 1.0× |
| V3-param | 3.607 | ~60× |
| V3 | 3.694 | ~58.6× |
| V4 | 3.804 | ~56.9× |

**V4 比 V3 多做了高斯 + 形态学 + 前景/ROI 统计，耗时略高是预期行为**；

不要把"V4 比 V3 慢"写成性能回退，也不要把"V3 比 V4 快"当成优化成绩 —— 两者功能不同。

