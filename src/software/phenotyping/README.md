# phenotyping —— 单粒种子表型测量（开发阶段 B）

把 开发阶段 的 YOLO bbox 变成「每一粒种子的可解释测量」：

**YOLO 负责「目标在哪里」，这里负责「目标多大、什么形状」**。

形状来自板端 PL（FPGA V4）的二值结果，彩色原图只用来定位和裁 ROI。

产出：`phenotyping.py`（测量）、`roi_mapping.py`（坐标规范）、`verify_measurements.py`（独立复核），

外加 30 粒人工核查表、核查拼图、标注图和失败案例。

## 1. 用了哪些数据

数据是 `hardware/runtime/image_run01_pc/`（开发阶段 上板采集落盘，10 帧，每帧一对：

彩色原图 `raw_bgr_0000NN.png` + 板端 PL 二值结果 `pl_gray_0000NN.png`，用 `frame_id` 对齐）。

**这批 10 帧里只有后 5 帧是种子**：人工看图确认第 0–4 帧拍的是**叶片**，第 5–9 帧才是种子。

所以本模块只用 **第 5–9 帧**（复现命令里用 `--frames 5,6,7,8,9` 固定这一点）；

叶片那几帧的原始数据仍留在 `hardware/runtime/image_run01_pc/`，只是不参与表型统计。

另外两批真板数据（`hardware/runtime/live_run01_pc`、`hardware/runtime/开发阶段_live_run01_pc`）没有采用：

它们整体偏暗（彩色帧灰度中位 82 / 65，正常应是 235），板端二值图对比度差，跑出来的异常比例高达 34% / 51%。

> **检出数以协作成员那台机器为准（73 个）**：两个人跑同一脚本，本机多出 5 个框，逐个核对后是

> **同一粒种子被框了两次**（每个多出的框都和对方那版的一个框重叠 IoU 0.48–0.80；本机第 5 帧最低置信度

> 0.348，对方 0.808）。人工看图也确认本机那版"多数了种子"，协作成员重跑后以 **73** 个为准。

> 两台机器为什么差这么多（权重文件不同 vs 推理库版本不同）**还没定论、待确认**；

> `--detections-csv` 可以沿用别人的检测框、在本地把测量重算到完全一致（见第 6 节），

> 复现性差异留给 开发阶段 的复现性实验详细记录。

## 2. bbox → ROI 坐标规范（roi_mapping.py）

1. YOLO 输出统一为 `[x1, y1, x2, y2]`，坐标系 = **送进模型的那张原图**（不是缩放图、不是 Qt 显示图）。

2. 网络/板端尺寸与原图之间的缩放走 **letterbox**（等比缩放 + 居中填充）；

   拿到缩放坐标的 bbox 必须用 `bbox_to_src()` 反算回原图坐标再裁 ROI，禁止直接拿缩放坐标裁原图。

3. 每个目标一帧内唯一 ID：`Seed #001`、`Seed #002`…，按 上→下、再左→右 排序，编号稳定。

4. 帧号 `frame_id` 一律取自文件名（`raw_bgr_000006.png` → 6），保证 bbox / 板端结果 / 原图对齐。

## 3. 六项指标与口径（phenotyping.py）

ROI = bbox 往外放 2 px。ROI 掩膜先做固定的 **5×5 椭圆核 开 + 闭**（开运算去种皮纹理毛刺、

闭运算补小暗斑），再取面积最大的**外轮廓**作为主区域。

| 字段 | 定义 | 单位 |
| --- | --- | --- |
| `area_px` | 主区域外轮廓面积 `cv2.contourArea`（边界积分） | px² |
| `perimeter_px` | 轮廓周长 `cv2.arcLength(closed=True)` | px |
| `length_px` | `cv2.minAreaRect` 的长边 | px |
| `width_px` | `cv2.minAreaRect` 的短边 | px |
| `aspect_ratio` | `length_px / width_px`（恒 ≥ 1） | 无量纲 |
| `circularity` | `4πA / P²`，`P = 0` 时返回 0.0 | 无量纲 |

**单位：还没有做毫米标定，Length/Width/Perimeter 一律 px、Area 一律 px²，不是 mm。**

长宽用旋转外接矩形而不是水平 bbox，所以种子朝向不会改变 Length/Width 的定义。

## 4. status 异常处理（B3）

| status | 判定规则 |
| --- | --- |
| `OK` | ROI 里一块完整区域，正常测出来 |
| `MERGED` | ROI 里出现 **≥2 个显著区域**（次区域 ≥ 主区域 30%）→ 疑似两粒粘连，或 YOLO 框横跨了两粒 |
| `BROKEN_CONTOUR` | 四选一：① 目标被**画面边界截断**（bbox 顶到图像边缘且边缘内侧 2 px 确有前景）；② 主区域 < 30 px²；③ **轮廓破碎**（圆形度 < 0.10，周长是等面积光滑轮廓的 2 倍以上）；④ **板端结果在 bbox 内饱和**（bbox 内白色占比 ≥ 90%，轮廓糊成一整块，量出来的面积/长宽其实就是 bbox 的尺寸） |
| `INVALID` | ROI 里一个前景轮廓都没有 |

每个目标另带 `status_reason`（一句话写清为什么不是 OK，Qt 详情卡和报告可直接引用）。

辅助字段（只提示、不参与判定）：`bbox_white_ratio`、`truncated_by_image`、`region_count`、

`border_contact_ratio`、`area_px_cross` / `area_diff_pct`（同一面积的两套独立算法）。

## 5. 输出文件

| 文件 | 说明 |
| --- | --- |
| `objects.json` | 逐帧结构化结果：每帧 `frame_id` / `seed_count` / `letterbox` / `objects[]`；顶层另有一份扁平 `objects[]` 供批量统计 |
| `measurements.csv` | 全部目标一行一粒，字段名与 Qt 显示名称一致 |
| `manual_check_30seeds.csv` | 人工核查表（30 粒 = 每帧 5–7 粒，帧内按序号等间隔挑） |
| `manual_check_30seeds_sheet.png` | 核查拼图：每格上=彩色 ROI（蓝框=YOLO bbox）、下=板端 PL 二值 ROI |
| `recheck_30seeds.csv` | `verify_measurements.py` 独立复算同样 30 粒的对照表 |
| `annotated/frame_00N_annotated.png` | 标注图：bbox + Seed ID + A/L/W（第 5–9 帧） |
| `failure_cases/` | 失败案例留档 + 说明（本批 16 个"饱和"目标里挑 6 个代表） |

JSON 里每个 object 至少含：`id`、`seed_id`、`bbox_xyxy_src`、`confidence`、`area_px`、`length_px`、

`width_px`、`perimeter_px`、`aspect_ratio`、`circularity`、`status`，外加 `status_reason`、

`bbox_xyxy_model`、`bbox_white_ratio`、`area_px_cross`、`area_diff_pct`、`region_count`、

`truncated_by_image`、`border_contact_ratio`、`unit`。

**与 Qt 对接**：字段名就是给 界面侧直接消费的接口 —— 目标列表用 `seed_id` / `confidence` /

`length_px` / `width_px` / `area_px`；详情卡用 `area_px` / `length_px` / `width_px` /

`perimeter_px` / `aspect_ratio` / `circularity` / `status`（可加 `status_reason`）。界面侧只负责显示。

## 6. 复现

```bash

python src/software/phenotyping/phenotyping.py --frames-dir hardware/image_run01_pc \

  --pl-dir hardware/image_run01_pc --conf 0.25 --frames 5,6,7,8,9

python src/software/phenotyping/verify_measurements.py

```

`--model-root` 默认就是仓库根目录（读 `src/software/ai/inference/infer_seed.py` 与 `data/models/best.pt`）。

换一台机器（推理环境不同、检出会差几个框）时，要复现仓库里这版数字，用已有的检测结果重算：

```bash

python src/software/phenotyping/phenotyping.py --frames-dir hardware/image_run01_pc \

  --pl-dir hardware/image_run01_pc --frames 5,6,7,8,9 \

  --detections-csv data/evidence/phenotyping/measurements.csv

python src/software/phenotyping/verify_measurements.py

```

`--detections-csv` 只读 CSV 里的 `bbox_xyxy_model` / `confidence`，不跑 YOLO，

所以测量与出图在任何机器上都一致（实测：本机用仓库 CSV 重算出的 `measurements.csv` 与仓库里那份逐字节相同）。

同参数连跑两次输出完全一致。SHA-256：

`objects.json` = `02CD0D26C121EEDE…`，`measurements.csv` = `8AF4FD0111AAD91B…`。

（两张核查表带人工填写的 `human_verdict`，哈希随人工结论变化，不列。）

## 7. 本批结果（第 5–9 帧，真种子）

| 项 | 值 |
| --- | --- |
| 目标总数 | 73（按帧：14 / 37 / 5 / 11 / 6） |
| `OK` | 57 |
| `BROKEN_CONTOUR` | 16（**全部**是"bbox 内板端结果饱和"；按帧：第 6 帧 13 个、第 8 帧 1 个、第 9 帧 2 个） |
| `MERGED` / `INVALID` | 0 / 0 |

`OK` 子集：面积 1378–14730 px²（中位 3593），圆形度中位 0.74（最低 0.32），AR 1.00–1.98。

**那 16 个为什么饱和**：板端 V4 的形态学（morph）对二值结果做了固定半径的膨胀，

目标越小、膨胀占比越大 —— 第 7 帧的种子约 183 px 时 ROI 白色占比只有 0.32–0.58（能测出形状），

第 6 帧的种子只有约 48 px，膨胀后整块糊成白色，量出来就等于 bbox 尺寸。

所以这条规则把它们判掉而不是让假数据混进 开发阶段 的分级。小粒种子要测量，需要放大成像或调小膨胀半径。

## 8. 人工核查（B4）

* **抽样**：30 粒，每帧 5–7 粒、帧内按序号等间隔挑，覆盖 5 帧的不同位置和大小。

* **人工看图（2026-10-14）**：逐格核对「bbox 是否框住整粒种子 / 轮廓是否对应 / 长宽方向是否合理」。

  人工报出 5 粒问题 —— `f6 Seed #016`、`f6 Seed #023`、`f6 Seed #031`、`f8 Seed #011`、`f9 Seed #002`

  （下半的板端二值图糊成一整块白）；另有 1 粒 `f9 Seed #005` 由同一条规则判出。结论逐行写在 `human_verdict` 列。

  当前这版（协作成员那台机器的检出）里，30 粒中 24 粒 OK、6 粒异常，其中 **29 粒已有人工结论**，

  `f5 Seed #003` 是重跑后新进样本、没单独看过，按各项数值正常（A=4286.5 px²、L/W=104/61.5、

  AR=1.69、圆形度 0.62、无异常标记）记为"数值正常、未单独人看"，不是伪造的人工结论。

* **独立脚本复核**（`verify_measurements.py` → `recheck_30seeds.csv`）：换一套代码把同样 30 粒重算 ——

  面积改成"填洞后数连通域像素"，长宽改成"暴力旋转卡壳求最小外接矩形（0.25° 步进）"，

  周长改成"自己把轮廓相邻点距离加和"。结果：**长/宽最大差 0.08 / 0.06 px，周长 0 差**，

  面积中位差 3.04%（差的那点主要是"数像素"天然比"边界积分"多半个周长左右，是口径性质不是错误）。

* 重跑 `phenotyping.py` 会保留已填的 `human_verdict` 和已核查过的样本（按 frame_id + seed_id 对齐），

  既不会冲掉人工结论，也不会换样本。

## 9. 已知局限

* **没有毫米标定**：只有像素口径；要 mm 需要标定物（如 1 元硬币，直径 25.0 mm）和一次上板采集。

* **小目标受板端形态学膨胀影响大**：本批 16/73 因此判为饱和（第 6 帧最集中）；想测小粒种子要放大成像或调小膨胀。

* **`MERGED` 只判"ROI 里有几块区域"**：两粒完全贴合、在板端结果里连成一整块时仍会记 `OK`，

  拆框留给 开发阶段 的分级规则或人工复核，这里不猜。

* 第 0–4 帧是叶片画面，本模块不使用；换题材（叶片）时这套指标同样适用，但要按叶片重新定"正常/异常"的阈值。

