# Sobel HLS 核优化实验：PIPELINE 前后对比

## 实验设置

| 项目 | 值 |
|---|---|
| 目标器件 | `xc7z020clg400-1`（PYNQ-Z2，Zynq-7020） |
| 时钟频率 | 100 MHz（周期 10 ns） |
| 工具 | Vitis 2026.1（`vitis-run` C Simulation / `v++` Synthesis） |
| 核接口 | `m_axi` ×2（src 65536 B / dst 64516 B）+ `s_axilite` 控制 |
| 数据结构 | 3 行行缓存 `lbuf[3][256]`，`array_partition complete dim=1` 完全分割 |

对照版本仅有一处差异：

- **基线版** `sobel_kernel.cpp`：四个内层循环显式 `#pragma HLS pipeline off`

  （Vitis 2026.1 默认开启自动流水线，若不显式关闭基线将失去意义）；

- **优化版** `sobel_kernel_pipelined.cpp`：同一四个循环应用 `#pragma HLS pipeline`。

综合报告存档于 `hardware/build/reports/sobel_synth_*.rpt`（`reports/`，原引用文件缺失或已排除，见提交问题清单），对比图由

`docs/evidence/charts/make_chart.py`（`charts/make_chart.py`，原引用文件缺失或已排除，见提交问题清单） 直接从报告解析生成。

## 优化对比

!HLS 优化前后对比（`charts/hls_optimization.png`，原引用文件缺失或已排除，见提交问题清单）

| 指标 | 基线版 | PIPELINE 版 | 变化 |
|---|---|---|---|
| 总延迟（cycles，单帧 65536 像素） | 716,319 | 199,675 | **−72.1%（3.6×）** |
| INIT 吞吐（cycles/像素） | 12 | 2 | −83% |
| LOAD 吞吐（cycles/像素） | 3 | 1 | −67% |
| COMPUTE 吞吐（cycles/像素） | 6 | 1 | −83% |
| SHIFT 吞吐（cycles/像素） | 2 | 1 | −50% |
| LUT | 2,154 | 3,642 | +69% |
| FF | 1,597 | 1,905 | +19% |
| BRAM | 5 | 3 | −40% |

> 吞吐口径：未流水线版本每次迭代固定占用其「迭代延迟」个时钟；流水线

> 版本稳定后每「II」个时钟完成一次迭代。表中均折算为 cycles/像素，可直接比较。

## 结论

- **速度**：PIPELINE 使 LOAD / COMPUTE / SHIFT 三个关键循环达到 **II=1**（每时钟

  周期流入一个像素），单帧延迟从 7.16 ms 降至 2.00 ms（100 MHz 下），约 **3.6× 加速**；

  INIT 只执行一次且为帧间一次性开销，II=2 已足够。

- **面积代价**：流水线为每个循环展开调度，LUT +69%、FF +19%，换来速度提升；

  两版 LUT 占用均低于器件容量的 7%，面积余量充裕。

- **BRAM 不升反降**（5→3）：行缓存综合实现随调度变化而重映射，未造成存储开销增加。

- **权衡**：在本设计中 3.6× 加速换取约 +50% 逻辑资源是可接受的交易；若后续上板

  出现资源紧张，可对 INIT/SHIFT 关闭流水线（对整体延迟影响 <0.1%），仅保留

  LOAD + COMPUTE 的 II=1。

