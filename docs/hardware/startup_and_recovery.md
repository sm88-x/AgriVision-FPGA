# 板端启动与恢复（segmentation A 侧）

配合 `hardware/hls/integrated_pipeline/register_map_final.md`、`hardware/board/frame_server.py`、`hardware/board/frame_service.py`、

`hardware/runtime/roi_statistics_board.py` 使用。本文写清"从冷启动到跑起来"和"出问题怎么恢复"两条路径，

以及 segmentation 两个测试（三档回归、30 分钟稳定性）的准确命令。

## 一、连接与上电

| 项 | 值 |
| --- | --- |
| 板卡 | PYNQ-Z2，静态 IP `192.168.2.99` |
| PC 网卡 | `192.168.2.1/24`，与板卡网口直连 |
| 板端服务 | Jupyter `:9090`（`xilinx/xilinx`）里的终端 |
| 固件 | `hardware/firmware/integrated_pipeline_validated.bit` + `.hwh`（= segmentation Candidate 2，见寄存器表文档） |

上电顺序：接好网线 → 插 USB 摄像头（若要做实时链路）→ 上电 → 等 PYNQ 启动完

（板上的 `DONE` 灯亮起，约 40–60 s）。

## 二、冷启动到可跑（板端）

1. 把这三样放进 Jupyter 工作目录（同一目录）：

   `integrated_pipeline_validated.bit`、`integrated_pipeline_validated.hwh`、`hardware/board/frame_server.py`、`hardware/board/frame_service.py`；

2. 打开 Jupyter 终端，先做自测（会逐像素和软件 Golden 比对，三档尺寸各 10 帧）：

```bash

sudo python3 frame_service.py --selftest 10 --bitfile integrated_pipeline_validated.bit

```

3. 自测 PASS 后再起帧服务（示例为 30 分钟稳定性测试，见第四节）：

```bash

sudo python3 frame_server.py --bitfile integrated_pipeline_validated.bit --port 9000 --out . \

    --log stability_30min.log --csv stability_30min.csv --frames <N>

```

`frame_server` 会打印每帧归属与错误分类，并逐帧落 CSV；`--frames 0` 表示不限帧数。

## 三、PC 侧连上与自检

```powershell

# 1) 通路探测：发一帧小图，确认板端在听

python src/software/app/pc_client.py probe --host 192.168.2.99 --port 9000

# 2) 逐像素 Golden 对拍（用固定输入图）

python src/software/app/pc_client.py golden-check --host 192.168.2.99 <图片路径> ...

```

实时链路（Camera → FPGA → PC，phenotyping 的跑法）用 PC 侧统一入口：

```powershell

python src/software/app/live_backend.py --request <请求.json>

```

请求 JSON 关键字段：`mode="live"`、`host=192.168.2.99`、`port=9000`、`camera_index=0`、

`width=640`、`height=480`、`frames=<N>`、`pipeline={"mode":0,"threshold":40,"morph":2,"en_gauss":1}`。

板端与 PC 两侧各自落 CSV，**板端 `pl_ms` 与 PC 端到端耗时分开记录，不许混写**。

## 四、segmentation 两个测试怎么跑

### 1. 三档分辨率核心回归（A1）

输入是仓库里固定的 10 张 640×480 图（`data/fixtures/agricultural_images/`），7 个用例 × 3 档分辨率：

```powershell

python hardware/runtime/roi_statistics_board.py regression --bit hardware/firmware/integrated_pipeline_validated.bit `

    --images data/images/edge_validation --out hardware/regression_segmentation

```

产物：`hardware/runtime/regression_segmentation/pipeline_regression.csv`（脚本内部名）、`manifest.csv`（bit/hwh/脚本/输入图的 SHA-256）、

`failures/`。交付时把逐用例结果复制成 `data/evidence/hardware/application_regression.csv`。

回归覆盖的用例：`rgb2gray / gaussian / sobel / prewitt / threshold / erode / dilate`，

判定标准是逐像素 `different_pixels == 0` 且 `max_abs_diff == 0`（PL vs 软件 Golden）。

前景/ROI 像素统计（phenotyping 新增逻辑）单独跑一遍：

```powershell

python hardware/runtime/roi_statistics_board.py roi --bit hardware/firmware/integrated_pipeline_validated.bit `

    --images data/images/edge_validation --rois data/evidence/hardware/roi_definitions.json --out hardware/regression_segmentation

```

产物 `roi_comparison.csv`：每个 ROI 的 `pl_count` / `sw_count` / `count_error` / `different_pixels`，

≥10 个 ROI。

### 2. 30 分钟连续运行（A2）

目标是**真实链路连续 30 分钟无 DMA 死锁**，两侧同时记录：

* 板端：`frame_server.py --frames <N>`（`N` = 30 分钟的帧数，按实测帧率换算），

  产出 `board_frames.csv` + `stability_30min.csv` + `stability_30min.log`；

* PC 端：`live_backend.py` 的 `live` 模式跑同样帧数，产出 `pc_frames.csv` + 落盘帧；

* 记录字段：`frame_id`、`width`/`height`、成功帧数、错误数、DMA 超时数、`pl_ms`（均值/中位/最大）、

  PC 端端到端耗时。

判定：成功帧 = 请求帧数、错误 0、超时 0；出现任一错误就记录并重新计时（任务书要求"出现错误必须修复并重新计时"）。

## 五、故障恢复

| 现象 | 处理 |
| --- | --- |
| PC 连不上板端（`probe` 超时） | 先 `ping 192.168.2.99`；不通 → 检查网线/网卡 `192.168.2.1/24`/板子是否启动完；通则看板端 `frame_server` 是否在跑 |
| 单帧 `ERR_TIMEOUT`（默认 5 s） | DMA 没吐完成标志。先看 MM2S 长度是否 `width×height×4`；再查 `out_sel`、尺寸是否超 3–1920 / 3–1080 |
| `ERR_DMA`（`DMASR.bit25–28` 置起） | 拍数/TLAST 异常，多半是输入长度或通道数不匹配；确认协议 `channels` 与 `enable_gray` 的对应关系 |
| 图像全黑/全白或结果不对 | 检查 `channels` 与 `enable_gray` 是否重复转灰度；检查 `threshold`/`morph_mode` 是否被上一轮残留参数影响 |
| 帧错位 / 结果显示上一帧 | 检查写入后 `flush()`、读取前 `invalidate()`；确认单板单主控（DMA 独占） |
| Start/Stop 后服务没反应 | 停掉 `frame_server` 进程再重新起（`Ctrl+C` 或 `kill`），重新跑一次 `--selftest` 确认 overlay 还在 |
| 换过 bit/hwh 后行为异常 | 必须 `.bit` 与同名 `.hwh` 成对替换，并在日志里记录两者的 SHA-256 |
| 板子卡死、Jupyter 进不去 | 先尝试在终端里结束推理内核再 `poweroff`（正常断电），等指示灯灭后再拔电 |

## 六、本次记录（segmentation，2026-10-16 上板）

板子：PYNQ-Z2 静态 `192.168.2.99`，PC 网卡 `192.168.2.1/24` 直连，摄像头接 PC（`camera_index=0`，640×480）。

固件：`hardware/firmware/integrated_pipeline_validated.bit` + `.hwh` = segmentation Candidate 2；

bit SHA-256 `c7e8d004…6c12`，与板上、与 phenotyping 日志记录一致；

hwh 在板上/git 里是 `b10bef4b…bb5a`（Windows 工作区那份因 CRLF 不同，见寄存器表文档）。

### 1. 三档分辨率核心回归（A1）

命令：`hardware/runtime/roi_statistics_board.py regression --bit hardware/firmware/integrated_pipeline_validated.bit --images data/images/edge_validation`

| 项 | 结果 |
| --- | --- |
| 用例 × 分辨率 × 图 | 7 用例 × 3 档（256×256 / 320×240 / 640×480） × 10 张图 = **210 次** |
| 结果 | **210 / 210 PASS**（`different_pixels = 0`、`max_abs_diff = 0`，PL vs 软件 Golden） |
| 640×480 `pl_e2e_ms` | 中位约 4.08 ms |
| 明细 | `data/evidence/hardware/application_regression.csv`；来源清单 `data/evidence/hardware/application_regression_manifest.csv`（bit/hwh/脚本/输入图的 SHA-256） |

### 2. 前景 / ROI 像素统计（A1）

命令：`hardware/runtime/roi_statistics_board.py roi --bit hardware/firmware/integrated_pipeline_validated.bit --rois data/evidence/hardware/roi_definitions.json`

| 项 | 结果 |
| --- | --- |
| ROI 数 | 14 个（≥10 个要求） |
| 结果 | **14 / 14 PASS**，`pl_count == sw_count`，`count_error` 合计 0，逐像素 `different_pixels = 0` |
| 明细 | `data/evidence/hardware/roi_comparison.csv` |

### 3. 30 分钟连续运行（A2）

命令：板端 `hardware/runtime/roi_statistics_board.py serve --frames 8000 --port 9000`，

PC 端 `src/software/app/live_backend.py`（`mode=live`、`preset=seed_preset`、`frames=8000`、

`golden_audit=true`）；`8000` 帧按试跑实测 ~4.3 帧/秒换算 ≈ 31 分钟。

| 项 | 结果 |
| --- | --- |
| 稳定性明细 | `data/evidence/hardware/stability_30min.csv`（8000 行，板端与 PC 端字段并排） |
| 连续运行时长 | **32.4 分钟**（21:44:55 → 22:17:19，≥30 分钟要求） |
| 帧数 | **8000 / 8000 成功**，超时 0、DMA 错误 0、协议错误 0、丢包 0 |
| 板端 `pl_ms` | mean 4.1001 · median 4.0709 · min 3.9705 · max 13.3824 ms |
| 板端帧内 E2E `pl_e2e_ms` | 与 `pl_ms` 同口径，逐帧记录在 CSV |
| PC 端 `round_trip_ms` | mean 165.7 · median 165.0 · min 164.1 · max 234.5 ms |
| 前景/ROI 计数 | 逐帧 `pl_count == 软件参考值`（8000 帧全一致，`count_match=True`） |
| Golden 抽查 | 10 帧，`different_pixels = 0`（0 帧不符） |
| 传输量 | 收 / 发各 2,457,600,000 字节（= 8000 × 640×480） |
| 相机 | 采集 29.7 fps，链路 4.1 fps（丢帧 49,655 属预期；瓶颈在 PC/网络侧，PL 本身 243.9 fps） |

判定：成功帧 = 请求帧、错误 0、DMA 超时 0 ✓（过程中未出现需要修复的错误，因此无需重新计时）。

### 4. Start/Stop 与模式切换（A2）

板端一次常驻服务，PC 端按顺序切换；板端日志逐次记录了接入/断开与本次帧数：

| 轮次 | 操作 | 结果 |
| --- | --- | --- |
| 1 | Live 100 帧（`seed_preset`） | OK，0 错误 |
| 2 | 切 **Image 单张**（显式 `sobel_thr80_erode`，与上面的 dilate 不同组合） | OK，逐像素 Golden PASS |
| 3 | Live 200 帧，跑 12 秒后**强制中断客户端**（模拟异常退出） | 板端记录"客户端断开（本次 55 帧）"，服务继续待命，无错误 |
| 4 | **重新 Start** Live 50 帧 | OK，0 错误（异常后可恢复） |
| 5 | 切 **Batch**（`data/fixtures/agricultural_images` 12 张） | 12 / 12 OK，0 超时、0 DMA 错误 |

### 5. 服务重启（A2）

停掉 `frame_server`（`pkill -f 'roi_statistics_board.py serve'`）→ 确认 9000 端口释放 → 重新起服务 → Live 50 帧：

**OK，0 错误**。说明"Stop → 重启 → 再 Start"这条恢复路径可用，

无需重启板卡；换 overlay / 换 bit 的恢复路径见第五节。

