# 统一口径板端 Benchmark（开发阶段 性能证据链）

> 口径：PL E2E = 挂 DMA + 收发一帧 + 等完成，全程计时；PS = 只计软件算法本身

> （读图与内存在计时外）；FPS = 1000 / 中位数；加速比 = PS 中位数 / PL E2E 中位数。

> 每项 warm-up 2 次 + 正式 20 次，**逐次原始数据全部保留在 r1~r6 的 CSV 里**。

## 1. 为什么要有这一批数据

早期（2026-10-03）的 V0 / V3 数据是用 PYNQ 自带 `wait()` 计的，

V3 参数化版用 DMA 完成标志轮询计——**两把尺子**，跨版本绝对值不能直接比。

本批数据用同一个脚本 `hardware/drivers/board_benchmark.py` 测四个后端，

**同一输入、同一尺寸、同一完成判定方法**，因此可以横向比较。

## 2. 测试环境

| 项 | 值 |
|---|---|
| 板卡 | PYNQ-Z2（xc7z020clg400-1） |
| 板端环境 | PYNQ Linux + Jupyter + Python 3 |
| 测量脚本 | `hardware/drivers/board_benchmark.py` |
| 完成判定 | 轮询 AXI DMA 状态寄存器的 IOC 位（不依赖中断） |
| 地址来源 | 从各 `.hwh` **动态读取**，代码中未写死 |
| 测试日期 | 2026-10-05 |

四个后端：

| 后端 | 实现 | 接口 | 尺寸 |
|---|---|---|---|
| `ps` | 板上 PS 软件（numpy 参考实现） | — | 任意 |
| `v0` | 开发阶段 流式核 | AXI4-Stream，`ap_ctrl_none`（无寄存器） | 固定 256x256 |
| `v3` | 开发阶段 流式核（流水线化） | 同上 | 固定 256x256 |
| `v3p` | 开发阶段 参数化核 | AXI4-Stream + `s_axilite` | 任意，3~1920 / 3~1080 |

> `v0` / `v3` 只有 Sobel 通路，且尺寸写死，因此另外几组会自动跳过。

## 3. 原始数据

| 文件 | 内容 |
|---|---|
| `r1_256_sobel.csv` | 256x256 Sobel，四个后端（ps / v0 / v3 / v3p），各 20 次 |
| `r2_256_prewitt.csv` | 256x256 Prewitt，ps + v3p，各 20 次 |
| `r3_320_sobel.csv` | 320x240 Sobel，ps + v3p |
| `r4_640_sobel.csv` | 640x480 Sobel，ps + v3p |
| `r5_320_prewitt.csv` | 320x240 Prewitt，ps + v3p |
| `r6_640_prewitt.csv` | 640x480 Prewitt，ps + v3p |

合计 **6 组 × 40~80 次 = 320 次传输，全部 PASS，不同像素一律为 0。**

## 4. 结果（中位数，单位 ms）

### 4.1 四代对比（256x256 Sobel）

| 实现 | 延迟 | FPS | 相对 PS |
|---|---|---|---|
| PS 软件 | 39.565 | 25.3 | 1× |
| V0 流式（未流水） | 6.295 | 158.9 | **6.3×** |
| V3 流式（II=1） | **1.079** | **926.8** | **36.7×** |
| V3 参数化 | 1.209 | 827.4 | 32.7× |

**参数化的代价**：V3 → V3 参数化，慢 **0.130 ms（12%）**。原因是每帧要多写

4 个 s_axilite 寄存器并拉一次 `ap_start`（V0/V3 是自由流，不需要），

核内部也多了运行时权重的乘法。**花 0.13 ms 换"任意尺寸 + 双算法切换"，划算。**

### 4.2 多尺寸 × 双算法（V3 参数化核）

| 尺寸 | 算法 | PS (ms) | PL E2E (ms) | PL FPS | 加速比 |
|---|---|---|---|---|---|
| 256x256 | Sobel | 39.565 | 1.209 | 827.4 | 32.7× |
| 256x256 | Prewitt | 33.932 | 1.214 | 823.4 | 28.0× |
| 320x240 | Sobel | 47.061 | 1.449 | 690.2 | 32.5× |
| 320x240 | Prewitt | 36.931 | 1.448 | 690.8 | 25.5× |
| 640x480 | Sobel | 216.308 | 3.607 | 277.3 | **60.0×** |
| 640x480 | Prewitt | 179.190 | 3.590 | 278.5 | **49.9×** |

### 4.3 单像素成本（Sobel）

| 尺寸 | PS (µs/像素) | PL (ns/像素) |
|---|---|---|
| 256x256 | 0.604 | 18.45 |
| 320x240 | 0.613 | 18.87 |
| 640x480 | **0.704** | **11.74** |

## 5. 四组核心对比的结论

**对比 1 · PS vs PL**：256x256 下 PS 39.565 ms → V3 1.079 ms，**36.7 倍**。

这是"为什么用 FPGA"的直接答案。

**对比 2 · V0 vs V3**：6.295 ms → 1.079 ms，**5.8 倍**。流水线优化没白做。

**对比 3 · 多尺寸**：加速比随尺寸**上升**（32.7× → 32.5× → **60.0×**）。

原因有两层：软件端图变大后放不进 CPU 缓存，单像素成本涨了 17%（0.604 → 0.704 µs）；

硬件端单像素成本反而降了 36%（18.45 → 11.74 ns），因为固定开销被更多像素摊薄。

**图越大，FPGA 优势越明显。**

**对比 4 · Sobel vs Prewitt**：硬件端两者几乎相同（1.209 vs 1.214；3.607 vs 3.590），

因为**共用同一条数据通路**，只差一个运行时权重；软件端 Prewitt 明显更快

（216.3 → 179.2 ms），因为它的系数都是 1，不用做乘法。

**算法切换对硬件时序没有影响**，这正是参数化设计的价值。

## 6. 怎么复现

板端需要的文件与命令写在脚本头部注释里。核心命令：

```python

%run board_benchmark.py --image plant_a_256x256.bin --backends ps,v0,v3,v3p \

    --repeats 20 --out results/r1_256_sobel.csv

```

汇总与图表：

```bash

python hardware/build/reports/make_summary.py        # → hardware/build/reports/summary.csv

python docs/evidence/charts/make_charts.py     # → report/charts/*.png

```

两个脚本都**直接从原始 CSV / 综合报告算数**，不含任何手抄数值。

> 早期（2026-10-03）的 `sobel_v0/`、`sobel_v3/` 与 2026-10-04 的

> `sobel_v3_param_board/` 数据保留作历史记录，`summary.csv` 的 `batch` 列

> 用于区分；本报告与图表**只使用统一口径的那一批**。

## 7. 给 Dashboard 的性能数据接口

Dashboard 的性能卡片**必须走这个接口取数，不允许手写数字**。

模块：`hardware/drivers/perf_data.py`（PC 与板端都能跑）

用法：把模块和 Notebook 放在同一目录，直接导入即可（板端就是 `开发阶段/` 这个目录）。

```python

from perf_data import get_card, available, format_line, card_to_html

available()          # 列出所有可用的 (尺寸, 算法, 版本) 组合

card = get_card("640x480", "sobel")

print(format_line(card))

from IPython.display import HTML

HTML(card_to_html(card))     # 直接渲染成一块性能卡片

```

`get_card()` 返回的字段正好对应任务书要求的五张卡片：

| 字段 | 含义 |
|---|---|
| `ps_ms` | PS Latency（软件延迟中位数） |
| `pl_ms` | FPGA E2E Latency |
| `pl_fps` | FPS |
| `speedup` | Speedup（`ps_ms / pl_ms`） |
| `golden` | Golden Reference PASS |

另有 `resolution` / `src/software/algorithm` / `backend` / `runs` / `batch` / `source`

用于溯源——`source` 指向原始逐次 CSV，任何数字都能追到板上的测量记录。

数据链路：

```

board/results/board_benchmark/r1~r6_*.csv   原始逐次记录（板上跑出来的）

        ↓  make_summary.py

hardware/build/reports/summary.csv                   聚合结果

        ↓  hardware/drivers/perf_data.py

Dashboard 性能卡片

```

接口默认只读**统一口径**那一批（`batch` 以 `2026-10-05` 开头）；

跨批次口径不同，混用会误导，所以不放进同一张卡片。

