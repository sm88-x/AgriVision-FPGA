# 开发阶段 板端结果（USB 摄像头实时链路 + 连续 DMA 帧服务）

测于 2026-10-11，板卡单主控（硬件侧自测），固件沿用 开发阶段 冻结版本。

## 一、这次跑了什么

| 项 | 内容 |
| --- | --- |
| 硬件 | PYNQ-Z2，静态 IP `192.168.2.99`，PC 网卡 `192.168.2.1/24` 直连 |
| 固件 | `configurable_pipeline.bit/.hwh`（= 冻结版本 `stable_v4_开发阶段`） |
| bit SHA256 | `6a25612345246d06298a5903b6b5d5f23cb2087d4930a0b488e14d309008c82a` |
| 板端脚本 | `hardware/board/frame_service.py` + `hardware/board/frame_server.py`（端口 9000） |
| 输入 | 灰度 8bit 随机图（640×480 / 320×240 / 256×256），协议 `channels=1` |

运行命令（板端）：

```bash

sudo python3 frame_service.py --selftest 10 --bitfile configurable_pipeline.bit

sudo python3 frame_server.py --bitfile configurable_pipeline.bit \

    --port 9000 --out . --log board_300frames.log --frames 300

```

## 二、板端自测（`--selftest 10`，三档尺寸各 10 帧）

30 帧逐像素 Golden **全部 PASS**（`different_pixels = 0`），错误路径四项 PASS。

## 三、300 帧连续帧压力测试（PC 推帧 → 网络 → 板端 DMA/PL → 回传）

板端统计（`board_300frames.log`）：

| 项 | 值 |
| --- | --- |
| 帧数 / 成功 / 失败 | 300 / **300** / 0 |
| 超时 / DMA 错误 / 协议错误 | 0 / **0** / 0 |
| 板端耗时 `pl_ms` | mean 3.1391 · median 3.9128 · min 1.4708 · max 4.0908 ms |
| 传输量 | 收 68,556,800 B / 发 68,556,800 B |
| Golden | 300/300 PASS（PC 端逐帧软件比对） |

按分辨率拆开（板端 `pl_ms`，取自 `frame_metrics.csv`）：

| 分辨率 | 帧数 | mean | median | min | max | 换算 FPS |
| --- | --- | --- | --- | --- | --- | --- |
| 640×480 | 200 | 3.9331 | 3.9213 | 3.8993 | 4.0908 | 254.3 |
| 320×240 | 50 | 1.5436 | 1.5307 | 1.5206 | 1.9027 | 647.8 |
| 256×256 | 50 | 1.5589 | 1.5246 | 1.4708 | 2.1653 | 641.5 |

与 开发阶段 的 V4 基准对比（同 bit，开发阶段 取 `data/evidence/board/configurable_pipeline/bench_v3_v4.csv` 的 median）：

| 分辨率 | 开发阶段 V4 full | 开发阶段 连续帧 | 差异 |
| --- | --- | --- | --- |
| 256×256（含 Gaussian+Dilate） | 1.4424 ms | 1.5246 ms | +5.7% |
| 320×240 | 1.6767 ms | 1.5307 ms | −8.7% |
| 640×480 | 3.8065 ms | 3.9213 ms | +3.0% |

差异在测量抖动范围内（板端耗时含寄存器写入 + 轮询），**同一批固件、同一口径**，没有性能回归。

系统端到端（`pc_side_300.csv`，P界面侧计时，含 PC 采集/发送/回传/收帧，不含 Qt 刷新）：

| 项 | 值 |
| --- | --- |
| 墙钟（300 帧，含 PC 端 Golden 计算） | 16.8 s |
| 单帧往返 mean / median / max | 31.51 / 40.85 / 70.25 ms |

> 口径说明：`pl_ms` 只含 PL 参数写入 + DMA 收发 + 完成轮询；往返时间还含网络与 PC 处理。

> 两者不能混写成"FPGA 性能"。640×480 往返中位数 ~41 ms 主要是 300 KB×2 走网络的开销。

## 四、这次踩到的坑（写下来避免重犯）

1. **PL 输入固定 32 bit/像素**：这份 V4 核的输入接口是 4 B/px，`enable_gray=0` 的含义是

   "取 32bit 字的低 8 位当灰度"。按 1 B/px 送会只喂到 1/4 帧，核永远不吐 TLAST，

   表现为 `S2MM_DMASR=0x0` + `AP_CTRL=0x1` + 5 s 超时。灰度要放在**低字节**再送。

2. **板端 Python 3.6.5 / numpy 1.13.3**：不能用 `np.random.default_rng`（1.17+）、

   `subprocess.run(capture_output=)`（3.7+）等新 API。

3. **板上没有 RTC**：`board_300frames.log` 里的时间戳是板子的默认时钟（2019-10-01），

   不是真实日期，引用时以文件名与本文为准。

## 五、复现

1. 把 `frame_service.py`、`frame_server.py`、`frame_protocol.py`、`edge_morph_driver.py`、

   `run_pipeline.py` 与 `configurable_pipeline.bit/.hwh` 放板端同一目录；

2. 按第一节两条命令跑自测与服务；

3. PC 端用 `src/software/app/pc_client.py`（或本目录记录的推帧脚本配置）连 `192.168.2.99:9000`，

   推 300 帧并逐帧与 `run_pipeline.software_pipeline` 比对；

4. 比对结果与本目录三份记录应一致。

