# pipeline_validation 板端验证报告：V4 可配置流水线（RGB2Gray → Gaussian → Edge → Threshold → Morphology）

> 被测 overlay：A 交付的 `hardware/drivers/configurable_pipeline.bit / .hwh`

> （核实例 `sobel_v4_pipeline_0` @ `0x40000000`，DMA `axi_dma_0` @ `0x40400000`）

> 对照 overlay：`hardware/drivers/parameterized_edge.bit / .hwh`（核 `sobel_v3_0`）

> 验证方：B 组；测试日期：2026-10-10；全部在真实板卡上运行

## 1. 结论

**共 69 个板端用例，全部 Golden PASS，不同像素一律为 0，最大误差一律为 0。**

| 验证维度 | 用例数 | 结果 | 数据文件 |
|---|---|---|---|
| 三档分辨率 × 逐级对拍（gray / gauss / edge / morph） | 12 | 全 PASS | `data/evidence/board/configurable_pipeline/pipeline_regression.csv` |
| 三档分辨率 × 关高斯走完整链 | 3 | 全 PASS | 同上 |
| 三档分辨率 × 膨胀（morph_mode=2） | 3 | 全 PASS | 同上 |
| 三档分辨率 × Prewitt 完整链 | 3 | 全 PASS | 同上 |
| **12 张农业图 × 4 种流水线模式（640×480）** | **48** | **全 PASS** | `data/evidence/board/configurable_pipeline/dataset_regression.csv` |
| V3 / V4 同口径性能对比 | 24 | 全 PASS | `data/evidence/board/configurable_pipeline/bench_v3_v4.csv` |

**也就是说**：RGB2Gray、Gaussian、Edge、Threshold、Morphology 五级在板上真实执行，

且每一级的输出都与 B 组的软件 Golden **逐像素一致**（不同像素 = 0）。

## 2. 逐级对拍（关键证据）

A 的核提供了 `out_sel` 寄存器，可以把**任意一级的中间结果**单独输出，

因此这次不是"只比对最终输出"，而是**每一级都能独立对拍**：

| out_sel | 输出级 | 256×256 | 320×240 | 640×480 |
|---|---|---|---|---|
| 1 | gray（RGB2Gray，全帧有效、不外圈清零） | PASS | PASS | PASS |
| 2 | gauss（3×3 高斯，外圈 0） | PASS | PASS | PASS |
| 3 | edge/threshold | PASS | PASS | PASS |
| 0 | morph（完整链最终输出） | PASS | PASS | PASS |

**这是"逐级正确"的直接证据**，而不是"最后碰巧对了"。

## 3. 数据集批量验证（12 张农业图 × 4 种模式）

任务书要求"对 Seed 数据集批量验证各 Pipeline 模式"。上一轮 21 个用例其实只用了排序第一张图

（按分辨率跑），**回答不了"每张图是不是都过"**；该项验证 48 个用例逐张过：

| Pipeline 模式 | 含义 | 张数 | 结果 |
|---|---|---|---|
| full | RGB2Gray → Gaussian → Edge → 输出（pipeline_validation 完整链） | 12 | 全 PASS |
| full_dilate | 完整链 + 膨胀（morph_mode=2） | 12 | 全 PASS |
| nogauss | 关高斯走完整链（验证旁路不回退） | 12 | 全 PASS |
| prewitt | 完整链换成 Prewitt | 12 | 全 PASS |

640×480 下逐图中位延迟全部落在 **3.794 ~ 3.867 ms**（48 条记录无一条离群），

说明上一轮那个 11.7 ms 是测量时的偶发抖动，不是核的问题。

**顺带得到一个可用的量化口径**：输出非零像素占比（CSV 里的 `nonzero_pct`）能直接当"边缘密度"用。

同样阈值下 Prewitt 比 Sobel 低 2~4 个百分点（例：leaf_02 Sobel 31.88% / Prewitt 28.56%），

方向和两种算子的梯度幅值差异一致，可以作为"选哪种算法"的量化依据。

## 4. 性能（每档 warm-up 3 次 + 正式 20 次）

| 分辨率 | mean | **中位** | min | max | FPS（按中位） |
|---|---|---|---|---|---|
| 256×256 | 1.479 ms | **1.465 ms** | 1.392 | 1.658 | 683 |
| 320×240 | 1.623 ms | **1.633 ms** | 1.427 | 1.639 | 612 |
| 640×480 | 4.254 ms | **3.847 ms** | 3.777 | 11.747 | 260 |

> **640×480 的 mean 被一个 11.7 ms 的离群值抬高**（20 次里其余都在 3.78~3.93 ms）。

> 以中位数 3.847 ms 为准更稳妥；建议后续增加测量次数或去掉离群值再统计。

## 5. V3 与 V4 同口径性能对比（同一次上板、同一张图）

同一会话、同一张图、同一 warm-up（3 次）+ 正式（30 次）：

| 分辨率 | V3-param（Edge→Threshold） | V4 `edge`（+RGB2Gray） | V4 `full`（+Gaussian） |
|---|---|---|---|
| 256×256 | 1.303 ms / 767 FPS | 1.409 ms / 710 FPS（+8.1%） | 1.442 ms / 693 FPS（+10.7%） |
| 320×240 | 1.539 ms / 650 FPS | 1.658 ms / 603 FPS（+7.7%） | 1.677 ms / 596 FPS（+9.0%） |
| 640×480 | 3.694 ms / 271 FPS | 3.804 ms / 263 FPS（+3.0%） | 3.807 ms / 263 FPS（+3.1%） |

几点读法（避免误读这张表）：

1. **V4 比 V3 多做的事不止一点**：多一级 RGB2Gray，输入还从 8bit/像素变成 32bit/像素，

   DMA 搬运量从 0.61 MB 涨到 1.53 MB。即使这样，640×480 也只慢 0.11 ms（+3.0%）。

2. **Gaussian 几乎是免费的**：V4 `full` 相比 V4 `edge` 只多 0.003 ms（640×480，+0.1%），

   因为高斯和边缘共用同一套 Circular Line Buffer + 3×3 滑窗，没有多跑一遍数据通路。

3. **膨胀（morph_mode=2）同样几乎零成本**：640×480 下 3.807 → 3.802 ms。

   Seed Preset 里"高斯 + 膨胀"的组合，性能上不用付代价。

4. **小图反而更吃亏**：256×256 下 V4 慢 10.7%，因为 DMA 固定开销占比更大。

   结论是"分辨率越高，V4 加的这些级越划算"。

## 6. 三个口径问题的板端实测（与 A 对齐）

A 问过三个最容易"两边差 1"的地方，这次用纯色图和纯白平场在板上直接测：

| 问题 | 板端实测 | 我们的 Golden | 结论 |
|---|---|---|---|
| 灰度式 `(77R+150G+29B+128)>>8` 下纯绿等于多少 | 纯绿 **149**、纯红 77、纯蓝 29、纯白 255 | 149 / 77 / 29 / 255 | 完全一致，A 没有偷改权重或舍入 |
| 高斯取整是加 8 再右移还是直接右移 | 平场 255 进 → 255 出，与 `(sum+8)>>4` 一致 | `(sum+8)>>4` | 一致 |
| 最外圈补 0 还是复制边缘 | 纯白图高斯级最外圈 = 0、第 2 圈 = 255 | 补 0 + 外圈强制 0 | 是"补 0"，不是"复制边缘" |

### 6.1 必须记录在案的副作用：亮平场会画出一整圈假边缘

纯白 640×480 走完整链，输出非零像素 **2228 个**，且**全部落在离边框 1 像素的那一圈**

（最外圈 0 个、第 2 圈 2228 个、第 3 圈只有 4 个角）——2228 正好是这一圈的周长减四个角。

原因是"高斯补 0 + 外圈清零"：高斯把最外圈亮度压到 0，第 2 圈因此出现巨大的亮度跳变，

边缘算子把它当成一整圈真实边缘。

这不是 bug（两边行为完全一致，Golden 也对得上），但**对下游计数有实际影响**：

凡是边框一圈是亮平场的图（比如 `load_canvas` 用白底 letterbox 出来的画布），

完整链输出会多出一圈 **≈2200 像素的连通块**，形态学也吃不掉。

后续做表型计数必须显式忽略最外 2 圈，或把边缘输出的边框区域清零。

## 7. 接口兼容性（这次验证顺带证明的事）

1. **寄存器偏移自动从 hwh 解析**：`edge_morph_driver.py` 里的 `parse_hwh_registers()`

   直接读 `.hwh` 得到 `{width:16, height:24, mode:32, threshold:40, morph_mode:48,

   enable_gray:56, enable_gauss:64, out_sel:72}`，**脚本里没有写死任何偏移**。

   A 每加一代核，只要 hwh 更新，脚本不用改。

2. **核实例名自动识别**：这次核名从 `sobel_v4_0` 又变成了 `sobel_v4_pipeline_0`，

   脚本靠"候选表 + 兜底挑非 DMA 的那个 IP"自动认出来，没有卡住。

3. **新寄存器用 `extra_regs` 传入**：`run_frame(..., extra_regs={"enable_gauss": 1, "out_sel": 2})`，

   不需要为每一代核改驱动函数签名。

## 8. 复现方式

板端把这几样放同一目录，然后一条命令：

```

configurable_pipeline.bit / .hwh

edge_morph_driver.py        驱动（含 hwh 寄存器表解析）

run_pipeline.py           B 组软件参考实现（Gray / Gaussian / Edge / Morph Golden）

pipeline_regression.py     按分辨率的逐级对拍

dataset_regression.py      按图片的完整链批量验证

pipeline_benchmark.py      V3 / V4 同口径性能对比

edge_validation_images/              12 张农业测试图

sudo python3 pipeline_regression.py  --images edge_validation_images --out results/pipeline_regression.csv --warmup 3 --repeats 20

sudo python3 dataset_regression.py   --images edge_validation_images --out results/dataset_regression.csv  --warmup 3 --repeats 20

sudo python3 pipeline_benchmark.py --tag v3 --bit v3/parameterized_edge.bit --input gray8  --out results/bench_v3.csv

sudo python3 pipeline_benchmark.py --tag v4 --bit configurable_pipeline.bit           --input rgbx32 --out results/bench_v4.csv

```

原始数据见 `data/evidence/board/configurable_pipeline/`：`pipeline_regression.csv`（21 行）、`dataset_regression.csv`（48 行）、

`bench_v3_v4.csv`（24 行，V3 + V4 合并）。

## 9. 遗留

- 第 6.1 节的假边缘圈还没在表型计数里处理（计数口径要加"忽略最外 2 圈"）。

- 泛化测试（不挑图，30~50 张）尚未做，这是"能不能讲到应用层"的判据。

- 该项验证 320×240 的逐图模式没跑（三档分辨率的性能已覆盖，内容等价，风险低）。

