# V4 寄存器映射与场景配置

本文档是「PS 场景判断 → AXI-Lite 动态配置 PL V4」的接口说明。

硬件沿用 V4 overlay，不重新综合、不修改 PL。

- `hardware/firmware/integrated_pipeline_validated.bit`

- `hardware/firmware/integrated_pipeline_validated.hwh`（必须与 bit 同名）

---

## 1. 板端对象

| 对象 | 类型 | 基地址 | 地址范围 | 说明 |
|---|---|---|---|---|
| `sobel_v4_pipeline_0` | HLS IP | `0x40000000` | `0x4000FFFF` | AXI-Lite 控制 + AXI4-Stream 图像 |
| `axi_dma_0` | AXI DMA | `0x41E00000`（integrated_pipeline_validated）<br>`0x40400000`（system_validation） | `0x41E0FFFF` / `0x4040FFFF` | MM2S 32 bit 输入，S2MM 8 bit 输出；**地址由脚本从 hwh 动态读取** |

板端加载（在 Jupyter / 脚本中执行，路径按实际存放位置调整）：

```python

from pynq import Overlay, MMIO, allocate

import numpy as np

ol = Overlay("integrated_pipeline_validated.bit")   # hwh 必须同名同目录

kernel = MMIO(0x40000000, 0x1000)          # 两版都是 0x40000000

dma    = MMIO(ol.ip_dict["axi_dma_0"]["phys_addr"], 0x1000)  # 不要写死

```

---

## 2. 寄存器表

### 2.1 控制寄存器

| 偏移 | 名称 | 取值 | 读/写 | 说明 |
|---|---|---|---|---|
| `0x00` | `AP_CTRL` | bit0=start | 写 1 启动 | `ap_ctrl_hs`，无 auto-restart，**每帧都要重新写 1** |
| `0x00` | `AP_CTRL` | bit1=done | 只读 | 1 表示本帧计算完成；轮询用 |
| `0x00` | `AP_CTRL` | bit2=idle, bit3=ready | 只读 | 调试用 |

### 2.2 数据寄存器（scene 动态配置只写这 8 个）

| 偏移 | 名称 | 取值范围 | 本设计写入值 | 含义 |
|---|---|---|---|---|
| `0x10` | `width` | 3–1920 | 320 / 640 | 图像宽（像素） |
| `0x18` | `height` | 3–1080 | 240 / 480 | 图像高（像素） |
| `0x20` | `edge_mode` | 0 / 1 | 0 | 0=Sobel，1=Prewitt |
| `0x28` | `threshold` | 0–4095 | 见配置表 | 0=不二值化（饱和输出）；>0 时取 `\|gx\|+\|gy\| > threshold` |
| `0x30` | `morph_mode` | 0 / 1 / 2 | 见配置表 | 0=Bypass，1=Erode，2=Dilate |
| `0x38` | `enable_gray` | 0 / 1 | 1 | 1=启用 RGB→Gray 定点转换 |
| `0x40` | `enable_gauss` | 0 / 1 | 1 | 1=启用 3×3 高斯（`[1 2 1; 2 4 2; 1 2 1]`） |
| `0x48` | `out_sel` | 0 / 1 / 2 / 3 | 0 | 0=最终 morph 输出，1=gray，2=gauss，3=edge |

数据通路固定为：

```

RGBX888 → (enable_gray) → (enable_gauss) → Sobel/Prewitt → 阈值 → (morph) → out_sel

```

灰度公式与 PL 一致：`Y = (77R + 150G + 29B + 128) >> 8`。

输出经 S2MM 为 8 bit，每帧 `width × height` 字节，`TLAST` 标记帧尾。

### 2.3 DMA 状态寄存器

| 偏移 | 名称 | 说明 |
|---|---|---|
| `0x04` | `MM2S_DMASR` | 输入通道状态；bit12 `IOC`=0x1000 表示传输完成 |
| `0x34` | `S2MM_DMASR` | 输出通道状态；bit12 `IOC`=0x1000 表示传输完成 |

IOC 是**写 1 清除**：每帧开始前分别向 `0x04`、`0x34` 写 `0x1000`。

内部错误位为 bit25–28（掩码 `0x1E000000`），超时时把它一起打进日志。

### 2.4 integrated_pipeline_validated 新增寄存器

最新版 `integrated_pipeline_validated` 在原有 8 个配置寄存器之后增加了 ROI 与计数输出：

| 偏移 | 名称 | 读/写 | 含义 |
|---|---|---|---|
| `0x50` | `roi_x0` | 只写 | ROI 左边界（像素） |
| `0x58` | `roi_y0` | 只写 | ROI 上边界 |
| `0x60` | `roi_x1` | 只写 | ROI 右边界（不含） |
| `0x68` | `roi_y1` | 只写 | ROI 下边界（不含） |
| `0x70` | `foreground_count` | 只读 | 本帧非零输出像素总数 |
| `0x74` | `foreground_count_ctrl` | 只读 | 计数状态/控制 |
| `0x80` | `roi_count` | 只读 | ROI 内非零输出像素数 |
| `0x84` | `roi_count_ctrl` | 只读 | 计数状态/控制 |

关键结论（已对照 `hardware/hls/integrated_pipeline/sobel_v4_pipeline.cpp`）：

- ROI **只影响 `roi_count` 的统计范围，不影响输出图像**；输出仍是完整一帧；

- 这 4 个 ROI 寄存器复位为 0 时，`roi_count` 恒为 0，但 `foreground_count`

  与输出图仍然有效；

- scene A 只写前 8 个配置寄存器，**不写 ROI**，因此不与旧 system_validation overlay 冲突；

- 后续若要用 `roi_count`，在 `ap_start` 前写

  `roi_x0=0, roi_y0=0, roi_x1=width, roi_y1=height`（与 `hardware/runtime/roi_statistics_board.py`

  的默认值一致）。

---

## 3. 场景分类与三类配置

### 3.1 PS 侧特征（`src/software/fpga/scene_policy/scene_policy.py`）

为控制 PS 开销，特征统一在 **≤160×120** 的降采样图上计算：

| 字段 | 计算方式 | 用途 |
|---|---|---|
| `contrast` | 灰度 p95 − p5 | 低对比度判断 |
| `foreground_ratio` | 前景像素 / 采样像素 | 密集判断 |
| `component_count` | 8 连通域个数（面积 ≥ 采样面积 0.1%） | 密集判断 |
| `largest_component_ratio` | 最大连通域面积 / 前景像素 | 粘连判断 |
| `polarity` | 边框像素中位数 vs Otsu 阈值 | 深色目标 / 亮色目标 |

灰度转换复用 PL 的定点公式；前景判定用 Otsu 阈值，再按边框亮度决定

“深色目标”还是“亮色目标”，避免目标占画面过半时少数类变成背景。

完全无 cv2 时退回纯 numpy 连通域，保证 PC 自检可跑。

### 3.2 判定顺序

```

if contrast < 50                      → low_contrast

elif fg_ratio >= 0.15                 → dense

elif component_count >= 20            → dense

elif largest_component_ratio >= 0.60

     and fg_ratio >= 0.10             → dense

else                                  → normal

```

阈值集中在 `DEFAULT_POLICY`，后续调参只改这一处，并把新值同步回本表。

### 3.3 配置表

| `config_id` | 场景 | enable_gray | enable_gauss | edge_mode | threshold | morph_mode | out_sel | 设计意图 |
|---|---|---|---|---|---|---|---|---|
| 0 | manual | 1 | 1 | 0 (Sobel) | 40 | 2 (Dilate) | 0 | phenotyping Seed Preset，手动模式固定可重复 |
| 1 | normal | 1 | 1 | 0 (Sobel) | 80 | 1 (Erode) | 0 | 稀疏、对比度足够：较高阈值 + 腐蚀去孤立杂边 |
| 2 | low_contrast | 1 | 1 | 0 (Sobel) | 30 | 2 (Dilate) | 0 | 弱边缘：降阈值接边 + 膨胀连接断边 |
| 3 | dense | 1 | 1 | 0 (Sobel) | 120 | 1 (Erode) | 0 | 密集/接触：高阈值压种皮纹理，腐蚀让粘连处变细 |

说明：

1. `manual` **只由手动模式使用**，`auto_config()` 永远不会返回它；

2. 表中阈值是 scene 上午的**起始策略**，下午上板后按 B 的分割效果调整；

   调整时同步改 `CONFIG_TABLE` 与本表，保证代码和文档一致；

3. 三种自动配置的 `out_sel` 固定 0（最终 morph 输出），保证 B 拿到的是同一种图；

4. 不改 `edge_mode`：仍用 Sobel。Prewitt 留给 network 消融对照，不在该项实验声称优劣。

### 3.4 真实图验证（PC 侧，7 张仓库图）

| 图片 | contrast | fg % | 连通域 | largest | 分类 | config_id |
|---|---:|---:|---:|---:|---|---:|
| `015.jpg` | 136.0 | 8.3 | 2 | 0.90 | normal | 1 |
| `060.jpg` | 190.0 | 19.7 | 2 | 0.88 | **dense** | 3 |
| `065.jpg` | 148.0 | 8.1 | 3 | 0.66 | normal | 1 |
| `071.jpg` | 89.0 | 7.8 | 1 | 1.00 | normal | 1 |
| `072.jpg` | 101.0 | 8.0 | 4 | 0.72 | normal | 1 |
| `096.jpg` | 19.0 | 3.4 | 3 | 0.49 | **low_contrast** | 2 |
| `100.jpg` | 21.0 | 3.6 | 1 | 1.00 | **low_contrast** | 2 |

> 连通域数是在 160×120 采样图上的**团块数**，不是种子粒数；这 7 张都是轻度接触，

> 不能拿它当计数真值。合成图自检（普通/低对比度/密集各一张）由

> `python scene_policy.py --selftest` 覆盖，三张全 PASS。

---

## 4. 写入时机与一致性规则

### 4.1 每帧固定顺序

```

1) 等上一帧 ap_done（AP_CTRL bit1）且 MM2S/S2MM 都已 IOC

2) 向 DMA 的 0x04、0x34 各写 0x1000，清 IOC

3) 写 8 个数据寄存器：0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0x40, 0x48

4) arm MM2S（输入 buffer，长度 width*height*4 字节）

5) arm S2MM（输出 buffer，长度 width*height 字节）

6) AP_CTRL ← 1

7) 轮询 AP_CTRL bit1、MM2S IOC、S2MM IOC，任一超时 1 s 判失败

```

> 顺序不能反：segmentation 真板实测“先挂 DMA 再拉 `ap_start`”才稳定。

### 4.2 切换配置的硬规则

| 规则 | 原因 |
|---|---|
| 只在帧间切换，禁止在 `ap_done=0` 时写数据寄存器 | 避免同一帧前后使用不同参数，产生跨帧不一致 |
| 改 `width/height` 后，MM2S 与 S2MM 的长度必须同步改 | 否则 DMA 传输长度与图像尺寸不符 |
| 每帧重新写 `AP_CTRL=1` | `ap_ctrl_hs` 无 auto-restart |
| 每帧开始前清双通道 IOC | IOC 写 1 清除，否则上一帧完成位会误判 |
| 超时后停止本帧，记录两个 DMASR 与 AP_CTRL | 避免带着错误状态继续灌帧 |

### 4.3 一致性检查

- **帧序号**：每帧日志带 `frame_id`，输出图文件名带同一 `frame_id`；

- **同配置可重复**：同一输入、同一配置连续跑两次，输出 PNG 的 SHA256 应相同；

- **切换不串帧**：配置 A 跑 N 帧 → 配置 B 跑 N 帧，B 的前若干帧不能出现 A 的输出；

  用“固定输入 + 已知期望输出”的合成帧最直观；

- **DMA 完整性**：S2MM 实际写入字节数必须等于 `width*height`，IOC 才算有效。

---

## 5. 计时口径（下午 CSV / 日志按此填写）

所有时间用 `time.perf_counter()` 取值，单位 ms；FPS = `1000 / e2e_ms`。

| 字段 | 起点 | 终点 | 含义 |
|---|---|---|---|
| `ps_submit_ms` | 第一个数据寄存器写 | `AP_CTRL ← 1` | PS 写 8 个寄存器 + 启动的开销 |
| `pl_busy_ms` | `AP_CTRL ← 1` | `AP_CTRL` bit1 `ap_done` | PL 计算本帧的时间 |
| `dma_in_ms` | `AP_CTRL ← 1` | MM2S IOC | 输入流传输到 PL 的时间（与 PL 计算重叠） |
| `dma_tail_ms` | `ap_done` | S2MM IOC | 计算结束后输出流回写的尾部时间 |
| `e2e_ms` | 第一个数据寄存器写 | S2MM IOC | 单帧端到端（不含下一帧准备） |
| `ps_decision_ms` | `compute_frame_stats` 开始 | `auto_config` 返回 | PS 场景判断本身耗时，**单独统计**，不计入 e2e |
| `fps` | — | — | `1000 / e2e_ms` |

**口径提醒**：

1. `pl_busy` 与 `dma_in` **重叠**，`e2e ≠ ps_submit + pl_busy + dma_tail`，

   不能把四项相加当端到端；

2. `e2e` 不含采集/显示，也不含 B 的检测与分割；

3. 每个配置至少 30 帧，报告 median / p95，不只报平均值；

4. 连续 300 帧另写一条日志，记录是否死锁、超时次数与最终 DMASR。

---

## 6. 资源口径

scene 不重新综合，资源占用直接引用 `integrated_pipeline_validated`（协作成员工程名 `v4_roi`）的实现报告：

- `docs/evidence/integrated_pipeline/utilization_report.rpt`（Design State: Routed，2026-10-14）

- `docs/evidence/integrated_pipeline/timing_report.rpt`

| 资源 | 使用量 | 占比 |
|---|---:|---:|
| LUT | 4701 | 8.84% |
| FF / Slice Registers | 5925 | 5.57% |
| BRAM | 6.5 | 4.64% |
| DSP | 6 | 2.73% |

时序：WNS `+1.299 ns`，TNS `0`，TNS 失败端点 `0`，全部约束满足；

时钟 `clk_fpga_0` 周期 10.000 ns（100 MHz）。

> 旧 `configurable_pipeline` 版的数字是 LUT 4456 / FF 5531 / BRAM 6.5 /

> DSP 5 / WNS +1.275 ns。scene 实测用的是 `integrated_pipeline_validated`，以上表为准；

> 两版差异主要来自新增 ROI/计数逻辑，不能把旧数字套到新 bit 上。

**结论必须如实表述**：寄存器动态切换只改变同一套硬件的运行参数，

**不会改变 LUT/FF/BRAM/DSP 数量**；不得声称“动态节省逻辑资源”或“动态加速”。

三种配置的差异只体现在图像输出与分割效果上，速度差异以实测 CSV 为准。

---

## 7. 与统一接口的对应

任务书统一接口：输入 `frame_id, width, height, image_format`；

FPGA 返回 `config_id, processed_frame, timing`。

| 统一字段 | A 侧来源 |
|---|---|
| `frame_id` | 调用方传入，写进每帧日志与输出文件名 |
| `width` / `height` | 调用方传入；`config_registers()` 同时写入 `0x10/0x18` |
| `image_format` | 固定 `RGBX888`（32 bit，bit[23:16]=R，[15:8]=G，[7:0]=B） |
| `config_id` | `auto_config()` 或 `manual_config()` 的 `config_id` |
| `processed_frame` | S2MM 输出 buffer，`uint8 [height, width]` |
| `timing` | 第 5 节的 `ps_submit/pl_busy/dma_in/dma_tail/e2e/fps/ps_decision` |
| 附加 `scene` / `decision` | 自动模式的分类结果与判定依据，供 Qt 显示 |

`apply_config(config, write=driver.write_reg, width=w, height=h)` 的

`write` 回调就是板端驱动的寄存器写函数；PC 上省略 `write` 时只返回

`[(offset, value), ...]`，便于单元自检。

---

## 8. 板端实测脚本

脚本：board_test.py。板端把 `board_test.py`、`scene_policy.py`、

`integrated_pipeline_validated.bit`、`integrated_pipeline_validated.hwh` 放在同一目录，然后：

```python

%cd <board_deploy_dir>

%run board_test.py --frames 30 --stability-frames 300

```

脚本默认先找 `integrated_pipeline_validated.bit`，找不到再找 `configurable_pipeline.bit`。

板上只有旧版时也可以显式指定：

```python

%run board_test.py --bit configurable_pipeline.bit \

                   --frames 30 --stability-frames 300

```

快速冒烟（约十几秒，先确认板端路径通）：

```python

%run board_test.py --frames 5 --stability-frames 20 --resolutions 320x240

```

用真实图测试（推荐，输出图可直接给 B 对照）。把 7 张样本图放进板端目录，

`--images` 指向目录，脚本会**逐张**跑三套配置并分别保存输出：

```python

%run board_test.py --images <board_deploy_dir> \

                   --frames 30 --stability-frames 300

```

只测一张图时用 `--image`：

```python

%run board_test.py --image <board_deploy_dir> \

                   --frames 30 --stability-frames 300

```

输出图命名带图名，例如 `640x480_060_dense_cfg3.png`、`640x480_060_input.png`；

合成图命名保持不变（`640x480_normal_cfg1.png`）。稳定性测试优先用 `--image`

指定的图；只给 `--images` 时用目录里的第一张。

仓库 7 张样本图的 PS 自动判定（320×240 实测）：

`015/065/071/072 → normal`、`060 → dense`、`096/100 → low_contrast`。

默认产物（写在 `--out-root`，默认脚本所在目录）：

| 文件 | 内容 |
|---|---|
| `data/evidence/benchmark/scene_pipeline_modes.csv` | 逐帧分项计时（含 300 帧稳定性行，`phase` 区分） |
| `data/evidence/benchmark/scene_300frames.log` | 300 帧稳定性逐帧日志与 SUMMARY |
| `data/evidence/benchmark/scene_ps_decision.csv` | PS 场景判断 median / p95 / min / max |
| `data/evidence/benchmark/scene_summary.json` | 配置、场景特征、计时汇总、验收检查 |
| `data/evidence/demo/pipeline_outputs/` | 每张输入图在三种配置下的 PL 输出 PNG + 输入参考图（文件名带图名） |

判定：脚本最后打印 `PASS` 表示两档分辨率 × 三配置均跑满 30 帧、

无 DMA 内部错误、同配置输出哈希一致、300 帧无超时/无死锁。

若未观察到 `ap_done` 清零，`pl_busy/dma_tail` 留空并给出提示，

但 MM2S/S2MM IOC 与 e2e 计时仍然有效。

