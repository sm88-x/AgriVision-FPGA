# live_stream 寄存器与时序说明（A 侧）

配套实现：`hardware/board/frame_service.py`、`hardware/board/frame_server.py`；协议正文见 `hardware/board/protocol.md`。

## 一、冻结的硬件版本（live_stream 未改 RTL）

live_stream 只把"单帧一次运行"升级为"连续帧 + 网络服务"，**没有改 PL**，因此沿用 pipeline_validation 已验证

的 bit/hwh，并按任务书命名为冻结版本 `stable_v4_pipeline_validation`（`frame_service.py` 里的

`FROZEN_pipeline_validation_BITFILE` 常量）。这样做的好处：pipeline_validation 的 Golden、三档分辨率、寄存器表

全部继续有效，出问题能立刻回退。

| 项 | 值 |
| --- | --- |
| 文件 | `hardware/firmware/configurable_pipeline.bit` |
| SHA256 | `6a25612345246d06298a5903b6b5d5f23cb2087d4930a0b488e14d309008c82a` |
| 文件 | `hardware/firmware/configurable_pipeline.hwh` |
| SHA256 | `79a6c384a652ef363496f619e71047d8ebf63570744cb938db604861931cdce1` |
| 核实例 | `sobel_v4_pipeline_0`（回退候选 `sobel_v4_0` / `sobel_v3_0`，自动识别） |
| DMA | `axi_dma_0`（MM2S 发送 + S2MM 接收） |
| 目标时钟 | 100 MHz |

> 换 bit/hwh 时**必须**同步更新本表与 `frame_service.FROZEN_pipeline_validation_BITFILE`，并在

> `board_300frames.log` 里留下版本记录——验收时要能对上。

## 二、寄存器表

**偏移一律从同名 `.hwh` 解析**（沿用 `edge_morph_driver.SobelParamDriver` 的做法），

代码里不写死地址。下表是 2026-10-10 版 V4 流水线核的实际偏移，供人工核对：

| 寄存器 | 偏移 | 写者 | 含义 |
| --- | --- | --- | --- |
| `ap_ctrl` | `0x00` | 驱动 | `ap_start` 每帧重新置 1（`ap_ctrl_hs` 非 auto-rewind） |
| `width` | `0x10` | 服务 | 每帧写，3–1920 |
| `height` | `0x18` | 服务 | 每帧写，3–1080 |
| `mode` | `0x20` | 服务 | 0=Sobel 1=Prewitt |
| `threshold` | `0x28` | 服务 | 0=饱和输出；>0=二值化阈值（含边界） |
| `morph_mode` | `0x30` | 服务 | 0=bypass 1=erode 2=dilate |
| `enable_gray` | `0x38` | 服务 | 1=PL 做 RGB2Gray；**0=透传**（取 32bit 字的低 8 位当灰度） |
| `enable_gauss` | `0x40` | 服务 | 1=开 3×3 高斯 `[1 2 1;2 4 2;1 2 1]/16` |
| `out_sel` | `0x48` | 服务 | 0=morph 1=gray 2=gauss 3=edge；**live_stream 线上固定 0** |

## 三、协议字段 → 寄存器（live_stream 实时链路）

实时链路 PC 送的是 **gray8（`channels=1`）**。注意**网络格式 ≠ PL 输入格式**：

这份 V4 核的输入接口固定 **32 bit/像素（4 B/px）**，所以板端要把灰度放进 32bit 字的

**低 8 位**再送 DMA（口径与板上实测 PASS 的

`hardware/drivers/integrated_pipeline_smoke_test.py` gray 用例一致）：

| 来源 | 写入 |
| --- | --- |
| 头 `width` / `height` | `width` / `height`（每帧） |
| 头 `mode` | `mode` |
| 头 `threshold` | `threshold` |
| 头 `morph_mode` | `morph_mode` |
| 头 `channels` | `enable_gray`：1 → **0**（透传，灰度放低字节），4 → 1（PL 做 RGB2Gray） |
| 头 `flags.bit1` | `enable_gauss` |
| 固定 | `out_sel = 0` |

要点：

* **`channels` 决定 `enable_gray`**，而不是头里的 `flags.bit0`；否则会出现"PC 已经转过

  灰度、PL 又转一次"——看起来能跑，结果全错，是这类项目最费时间的坑；

* **MM2S 长度恒为 `width×height×4` 字节**（32 bit/像素）。若按 1 B/px 送，核只收到

  1/4 帧、永远不吐 TLAST，现象是 `S2MM_DMASR=0x0` + `AP_CTRL=0x1` + 超时；

* `channels=4` 时 PC 送的就是 `(R<<16)|(G<<8)|B` 小端字，板端直接转发；

* `channels=1` 时板端 `words = gray.astype(uint32)`（高 24 位为 0）。

`pipeline_mode=1`（Seed Preset）时忽略上面除 `channels` 外的字段，直接用

Gaussian+Sobel+threshold 40+Dilate。

hwh 里不存在的寄存器名**不会被写**，只记进 `driver.unknown_user_regs`；自测会打印

"哪些寄存器缺席"，避免"以为写了其实没写"。

## 四、时序与性能口径

单帧流程（`SobelParamDriver.run_frame` 内部）：

1. 清 MM2S/S2MM 的 IOC 完成标志（否则上一帧的标志会被误判成本帧完成）；

2. 写参数寄存器（上表）；

3. 两个 DMA 通道挂好长度 = `width×height` 字节（MM2S 发送、S2MM 接收）；

4. 置 `ap_start`，轮询 DMA 状态；

5. 判定：`DMASR.bit1`（Idle）置起 = 传输结束；`DMASR.bit25-28` 任一置起 = 内部错误

   （拍数 / TLAST 异常）→ 立即报 `ERR_DMA`；超时（默认 5 s）→ `ERR_TIMEOUT`。

测量口径（与任务书第七节一致，两个数不许混写）：

| 指标 | 范围 |
| --- | --- |
| 板端 `pl_ms` | 参数寄存器写入 + DMA 发送 + PL 处理 + DMA 接收 + 完成轮询 |
| 系统端到端 | PC 采集 + letterbox 缩放 + 网络发送 + `pl_ms` + 网络回传 + Qt 刷新 |

缓冲区与缓存一致性（`frame_service.FrameService`）：

* 输入缓冲 **`uint32` × 像素数（4 B/px）**，输出缓冲 `uint8` × 像素数；

* 两种缓冲都按 `(width, height, channels)` 复用，最多缓存 3 种尺寸，换尺寸不重新

  申请、超限淘汰最旧并显式 `freebuffer()`；

* **写入后 `flush()`、读取前 `invalidate()`**，否则 CPU 侧会读到旧缓存或 PL 读到旧数据；

* 输出长度恒为 `width×height`（8 bit 灰度）。

## 五、真板实测登记（2026-10-11，PYNQ-Z2）

两份日志与逐帧记录在 `data/evidence/board/live_stream/`，这里是登记摘要。

**自测**（`frame_service.py --selftest 10`）：三档尺寸各 10 帧，共 30 帧，逐像素 Golden

全部 PASS；错误路径四项 PASS（坏长度 `ERR_BAD_LENGTH`、坏通道 `ERR_BAD_CHANNELS`、

坏尺寸 `ERR_BAD_SIZE`、DMA 错误分类由 mock 覆盖）。

**300 帧连续帧压力测试**（`frame_server.py --frames 300`，PC 经网络推帧）：

| 分辨率 | 帧数 | `pl_ms` mean | median | min | max |
| --- | --- | --- | --- | --- | --- |
| 640×480 | 200 | 3.9331 | 3.9213 | 3.8993 | 4.0908 |
| 320×240 | 50 | 1.5436 | 1.5307 | 1.5206 | 1.9027 |
| 256×256 | 50 | 1.5589 | 1.5246 | 1.4708 | 2.1653 |
| 验收项 | 结果 |
| --- | --- |
| 成功帧 / 失败帧 | 300 / 0 |
| 超时 / DMA 错误 / 协议错误 | 0 / **0** / 0 |
| 逐帧 Golden | 300/300 PASS（PC 端比对） |
| 版本记录 | `configurable_pipeline.bit`，SHA256 `6a25612346…` |

与 pipeline_validation 的 V4 基准同口径对比（`data/evidence/board/configurable_pipeline/bench_v3_v4.csv` median）：

256×256 1.4424 → 1.5246 ms（+5.7%）、320×240 1.6767 → 1.5307 ms（−8.7%）、

640×480 3.8065 → 3.9213 ms（+3.0%），均在测量抖动范围内，**无性能回归**。

> 板端无 RTC，`board_300frames.log` 的时间戳为板子默认时钟（2019-10-01），引用时以

> `data/evidence/board/live_stream/README.md` 为准。

