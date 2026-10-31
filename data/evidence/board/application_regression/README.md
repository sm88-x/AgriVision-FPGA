# 开发阶段 完整演示回归记录（D1~D4）

> 口径：PL E2E = 驱动 `run_frame` 全程（含参数配置、DMA 收发、缓存维护与完成轮询）；

> PS = 板端 ARM 软件边缘计算，预热 1 次、测量 5 次取中位数；

> Speedup = PS 中位数 / PL E2E；Golden = FPGA 输出与软件参考**逐像素**比对。

## 1. 测试环境

| 项 | 值 |
|---|---|
| 板卡 | PYNQ-Z2（xc7z020clg400-1） |
| Overlay | `parameterized_edge.bit` / `.hwh` |
| 核实例 | `sobel_v3_0`（s_axilite），基址 `0x40000000` |
| DMA 实例 | `axi_dma_0`，基址 `0x40400000` |
| 演示界面 | `hardware/drivers/dashboard.py` |
| 回归脚本 | `hardware/drivers/application_regression.py` |
| 参数 | threshold = 40（>0 即二值化）、min_area = 100 px²、闭运算 3×3 × 1 次 |
| 测试日期 | 2026-10-05（首轮）/ 2026-10-06（叶片素材更新，全量重跑） |

## 2. 演示回归矩阵（D1~D4）

| 用例 | 图片 | 算法 | 后端 | PS (ms) | FPGA E2E (ms) | 加速比 | Golden | Count | 平均面积 (px²) |
|---|---|---|---|---|---|---|---|---|---|
| D1 | `demo_seed` | Sobel | FPGA | 224.12 | 3.810 | 58.8× | PASS | **42** | 883.7 |
| D1b | `demo_seed` | Prewitt | FPGA | 223.12 | 3.789 | 58.9× | PASS | **42** | 860.5 |
| D2 | `demo_leaf` | Sobel | FPGA | 225.55 | 3.739 | 60.3× | PASS | **7** | 9393.2 |
| D2b | `demo_leaf` | Prewitt | FPGA | 225.19 | 3.749 | 60.1× | PASS | **7** | 8911.6 |
| D3a | `demo_microscopy` | Sobel | FPGA | 224.51 | 3.727 | 60.2× | PASS | **1** | 95580.5 |
| D3b | `demo_microscopy` | Prewitt | FPGA | 223.47 | 3.739 | 59.8× | PASS | **1** | 94903.0 |
| D4 | `demo_seed` | Sobel | PS | 223.45 | — | — | — | 42 | 883.7 |

五个表型指标（Count / 平均面积 / 平均长度 / 平均宽度 / 平均周长）全部逐次落盘在

`application_regression.csv`，单位分别是 个 / px² / px / px / px。

**三组一致性全部满足**：

- **双算法一致**：D1/D1b 都是 42；D2/D2b 都是 7；D3a/D3b 都是 1

- **软硬一致**：D4（PS）42 = D1（FPGA）42，平均面积也完全相同

- **Golden 一致**：7 次 FPGA 运行全部 PASS，不同像素一律为 0

## 3. 稳定性连跑（农业主 Demo × 5）

| 次序 | PS (ms) | FPGA E2E (ms) | 加速比 | Golden | Count | 平均面积 (px²) |
|---|---|---|---|---|---|---|
| #1 | 223.92 | 3.750 | 59.7× | PASS | 42 | 883.65 |
| #2 | 224.17 | 3.758 | 59.7× | PASS | 42 | 883.65 |
| #3 | 225.24 | 3.744 | 60.2× | PASS | 42 | 883.65 |
| #4 | 226.05 | 3.732 | 60.6× | PASS | 42 | 883.65 |
| #5 | 224.41 | 3.761 | 59.7× | PASS | 42 | 883.65 |

连跑 5 次总耗时 9.1 s（平均 1.83 s/次），**无 DMA 卡死、无尺寸错位、结果不更新等问题**；

Count 恒为 42，**平均面积精确到小数点后两位都完全一致**（883.65），结果可复现。

## 4. 系统适用条件（重要）

回归过程中对比了两类输入，结论对本系统的适用范围有直接约束：

| 输入类型 | 边缘像素占比 | Count 随阈值变化 | 判定 |
|---|---|---|---|
| **白底、目标分离**（种子 / 多片叶片） | 13% ~ 15% | **恒定不变** | ✅ 适用 |
| 背景纹理丰富的自然照片 | 7% ~ 40% | 8 → 0 → 4 → 4 → 8（乱跳） | ❌ 不适用 |

原因：本系统用 `RETR_EXTERNAL` 提取**最外层闭合轮廓**并按面积过滤。自然照片的背景自身产生

大量边缘，与目标边缘连成一片，最外层轮廓退化为整幅图、被边界过滤规则剔除；阈值稍变，结果就

剧烈跳变。**这不是算法缺陷，是输入前提**——表型计数本来就要求目标与背景可分离。

因此演示与宣传中应明确适用条件：**面向背景可控的批量图像（白底种子、离体叶片等）**，

不要宣称可用于任意自然场景。

### 4.1 一次"误传素材"留下的直接证据（2026-10-06）

重跑时不小心把 `data/fixtures/images/plant_a_640x480.png`（背景复杂的自然照片）当成 D1 的输入传上了板子，

于是同一套系统、同一个阈值 40，只换了一张输入图，结果变成：

| 输入 | Sobel Count | Prewitt Count |
|---|---|---|
| 白底种子图（正确素材） | 42 | 42 |
| 自然照片（误传素材） | **0** | **3** |

代码一行没改，输出却是 42 / 0 / 3 —— 这是"**输入前提决定成败**"最直接的一次现场演示，

比任何文字说明都有说服力。

## 5. 最稳定演示参数（可复现）

| 项 | 值 |
|---|---|
| 应用模式 | Plant Phenotyping |
| 图片 | `demo_seed.jpeg`（42 颗种子）、`demo_leaf.jpeg`（7 片叶片）、`demo_microscopy.jpeg`（显微，仅轮廓展示） |
| 算法 | Sobel / Prewitt 均可（两者结果一致） |
| 阈值 | 40 |
| min_area | 100 px² |
| 闭运算 | 3×3 椭圆核 × 1 次 |
| 后端 | FPGA |

## 6. D3 Microscopy 的说明

任务书对 D3 的要求是"**边缘增强 / 轮廓展示**"，并明确写到"不要求增加复杂细胞计数算法"。

该组实验实测：显微图在两种算法下都得到 **Count = 1、平均面积约 95000 px²**（约占画面 31%）。

也就是说系统把显微图当作**一个整体区域**提取了轮廓，**能展示边缘增强效果，但不构成细胞级计数**。

宣传口径需与实测一致：**D3 用于展示轮廓提取能力，不代表系统具备细胞计数功能。**

## 7. 标注图（成果展示）

结果图按仓库约定统一放在 `docs/evidence/screenshots/`（板端脚本运行时仍写在

`data/evidence/analysis/annotated/`，这里只是归档副本），格式为

**原图 + 轮廓叠加 + 目标编号 + 边界框**：

| 文件 | 对应 |
|---|---|
| `result_seed_sobel_annotated.png` / `result_seed_prewitt_annotated.png` | 白底种子 × Sobel / Prewitt（D1 / D1b） |
| `result_leaf_sobel_annotated.png` / `result_leaf_prewitt_annotated.png` | 白底叶片 × Sobel / Prewitt（D2 / D2b） |
| `result_microscopy_sobel_annotated.png` / `result_microscopy_prewitt_annotated.png` | 显微图 × Sobel / Prewitt（D3a / D3b） |

> D4（白底种子 × PS 后端）与 D1 的输出逐字节相同，不再单独保存一份。

## 8. 怎么复现

板端把 `dashboard.py`、`application_regression.py`、`edge_morph_driver.py`、唯一的

`parameterized_edge.bit/.hwh` 和三张演示图放在同一目录，先跑界面再跑回归：

```python

%run dashboard.py

%run -i application_regression.py        # -i：复用 Dashboard 建立的命名空间

```

> `-i` 不能省：`%run` 默认在空命名空间执行，看不到 Dashboard 的驱动与函数。

### 板端文件名对照（重要）

回归脚本按固定文件名读图，**必须把本目录的演示图改成下面这些名字再放上板**，

否则会读到 `data/fixtures/images/` 里的同名的另一批图（那两张是背景复杂的自然照片，

跑出来的 Count 与本报告完全不同）：

| 本目录文件 | 上板后改名成 |
|---|---|
| `demo_seed.jpeg` | `plant_a_640x480.png` |
| `demo_leaf.jpeg` | `plant_leaf_640x480.png` |
| `demo_microscopy.jpeg` | `microscopy_640x480.png` |

> 这三张演示图是**独立素材**，没有覆盖 `data/fixtures/images/` 里已有的 `plant_a_*` / `plant_b_*`，

> 因此本报告与前面 `board_benchmark/`、`sobel_v3_param_board/` 的历史数据互不影响。

逐次原始记录见同目录 `application_regression.csv`。

