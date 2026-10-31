# segmentation Final V4 Candidate 2 —— 寄存器表与接口（A 侧）

配套实现：`hardware/board/frame_service.py`、`hardware/board/frame_server.py`、`hardware/runtime/roi_statistics_board.py`（驱动包装），

板端网络协议正文见 `hardware/board/protocol.md`，phenotyping 的接口说明见

`hardware/hls/integrated_pipeline/register_and_interface.md`。

## 一、版本冻结

**segmentation 没有修改 RTL/HLS**：phenotyping 的 V4 Candidate 1 已经包含本项目需要的全部功能

（RGB2Gray / Gaussian / Sobel / Prewitt / Threshold / Erode / Dilate + 前景像素统计 + ROI 统计），

任务书也明确"若无硬件修改，沿用版本及其报告，不虚构新综合结果"。因此 Candidate 2 **等同于** phenotyping 的

`hardware/firmware/integrated_pipeline_validated.bit`，`segmentation_v4_candidate2` 只作为版本名记录，不重新综合。

| 项 | 值 |
| --- | --- |
| 板卡 / 器件 | PYNQ-Z2 / `xc7z020clg400-1` |
| 核实例 | `sobel_v4_pipeline_0`（HLS top `sobel_v4_pipeline`） |
| DMA | `axi_dma_0`（MM2S 发送 + S2MM 接收） |
| 目标时钟 | 100 MHz（HLS `clock=10`） |
| bit 文件 | `hardware/firmware/integrated_pipeline_validated.bit`（Candidate 2 同一份） |
| bit SHA-256 | `c7e8d00464a5082ef45b7d701d990487e6c1c2e8e254ea9b7f66c7f60eec6c12` |
| hwh 文件 | `hardware/firmware/integrated_pipeline_validated.hwh` |
| hwh SHA-256 | `456d5585e9e779a4f3b2188076f783dbed4be838b037eb458a4292ca1bea98d6` |
| HLS 源码 | `hardware/hls/integrated_pipeline/sobel_v4_pipeline.cpp`、`testbench.cpp`、`baseline_reference.cpp` |
| 综合/实现报告 | `docs/evidence/integrated_pipeline/csynth.rpt`、`utilization_report.rpt`、`timing_report.rpt`、`drc_drc_1.rpt` |
| 资源占用（实现后） | Slice LUT 4701 / 53200 = 8.84%；时序：用户约束全部满足 |
| HLS 估计（`csynth.rpt`） | 主循环 `STREAM_PIXELS` **Achieved II = 1**（Trip count 2,079,363，Latency 22 cycles，Slack +7.30 ns）；`STREAM_PIXELS` 流水段 Interval 2,079,385 cycles ≈ 20.79 ms @100 MHz；估计资源 BRAM 9 (3%) / DSP 5 (2%) / FF 4645 (4%) / LUT 6718 (12%) |
| 时钟 | `clock=10` → 100 MHz；csynth 估计 10 ns/cycle，与目标一致 |

**校验值核对（2026-10-16 上板复算）**：

| 文件 | 板上 `<board_deploy_dir>` | git 里存的 blob | Windows 工作区副本 |
| --- | --- | --- | --- |
| `integrated_pipeline_validated.bit` | `c7e8d00464a5082ef45b7d701d990487e6c1c2e8e254ea9b7f66c7f60eec6c12` | 同左 | 同左（二进制不受换行影响） |
| `integrated_pipeline_validated.hwh` | `b10bef4be59936f6b0476d919789188e826233144f2065b679afa484b625bb5a`（331,105 B） | **完全相同** | `456d5585…`（336,561 B） |

结论：phenotyping 日志记录的 `hwh_sha256 = b10bef4b…` 与仓库里存的**就是同一份**。

`.hwh` 是文本 XML，Windows 检出时会做 CRLF 转换（多出 5456 个 `\r`，正好 5456 字节），

所以在 Windows 上算出来的 SHA-256 会不一样；**去 CR 后与 git 版、板上版逐字节一致**。

今后比对文本类产物（`.hwh`/`.py`/`.md`）一律用 `git show HEAD:<path>` 的字节，不要用工作区副本。

## 二、AXI-Lite 寄存器表（最终版）

偏移一律从同名 `.hwh` 解析（`edge_morph_driver.SobelParamDriver` 的做法），代码里不写死地址。

下表与 `hardware/runtime/roi_statistics_board.py` 的 `OFFSETS` 一致，供人工核对：

| 寄存器 | 偏移 | 写/读 | 含义 |
| --- | --- | --- | --- |
| `ap_ctrl` | `0x00` | 写 | `ap_start` 每帧重新置 1（非 auto-rewind） |
| `IP_IER` / `IP_ISR` | `0x08` / `0x0C` | 读/写 | HLS 标准中断使能/状态（本设计不使用中断，由驱动忽略） |
| `GIER` | `0x2C` | 读/写 | 全局中断使能（同上） |
| `width` | `0x10` | 写 | 图像宽，3–1920 |
| `height` | `0x18` | 写 | 图像高，3–1080 |
| `mode` | `0x20` | 写 | 0 = Sobel，1 = Prewitt |
| `threshold` | `0x28` | 写 | 0 = 饱和输出；>0 = 二值化阈值（含边界） |
| `morph_mode` | `0x30` | 写 | 0 = bypass，1 = erode，2 = dilate |
| `enable_gray` | `0x38` | 写 | 1 = PL 做 RGB2Gray；0 = 透传（取 32 bit 字的低 8 位当灰度） |
| `enable_gauss` | `0x40` | 写 | 1 = 开 3×3 高斯 `[1 2 1; 2 4 2; 1 2 1]/16` |
| `out_sel` | `0x48` | 写 | 0 = morph，1 = gray，2 = gauss，3 = edge；**生产链路固定 0** |
| `roi_x0` | `0x50` | 写 | ROI 左边界（半开区间 `[x0, x1)`） |
| `roi_y0` | `0x58` | 写 | ROI 上边界 |
| `roi_x1` | `0x60` | 写 | ROI 右边界 |
| `roi_y1` | `0x68` | 写 | ROI 下边界 |
| `foreground_count` | `0x70` | 读 | 本帧输出图非零像素数（uint32） |
| `foreground_count_ctrl` | `0x74` | 读 | HLS 为 `foreground_count` 输出端口自动生成的握手寄存器；驱动只读数据寄存器，不读写它 |
| `roi_count` | `0x80` | 读 | 本帧 ROI 内非零像素数（uint32） |
| `roi_count_ctrl` | `0x84` | 读 | 同上，`roi_count` 的握手寄存器 |

统计口径（phenotyping 定稿，segmentation 沿用）：

* 统计对象是 `out_sel` 选中并经过最终边界处理的输出；

* `foreground_count` = 整幅非零像素数，`roi_count` = 半开矩形 `[x0,y0,x1,y1)` 内的非零像素数；

* 每次 `ap_start` 对应一帧，局部累加器从零开始；**只在本帧 `ap_done` 之后读统计值**，

  下一帧开始前 PS 先保存本帧统计；

* ROI 约束：`0 <= x0 <= x1 <= width`、`0 <= y0 <= y1 <= height`；空 ROI 返回 0；越界由软件拒绝；

* 每帧只支持一个统计 ROI；多个 ROI 的验收靠对同一整图重复处理完成；

* ROI 走的是"整图卷积 + 统计窗口"，不是先裁图再卷积。

## 三、输入/输出格式（与协议的关系）

| 项 | 约定 |
| --- | --- |
| 网络输入 | Gray8，H×W，`uint8`，行步长 W 字节（协议 `channels=1`） |
| PL 输入 | `uint32`，每像素 4 字节，低 8 位是灰度 —— **MM2S 长度恒为 `width×height×4`** |
| PL/网络输出 | Gray8，H×W，`uint8`，长度 `width×height` |
| 参数来源 | 帧头 `width`/`height`/`mode`/`threshold`/`morph_mode`；`channels` 决定 `enable_gray`（1→0 透传，4→1 由 PL 转灰度）；`flags.bit1` → `enable_gauss`；`out_sel` 固定 0 |

注意 `channels` 决定 `enable_gray`，不要再看 `flags.bit0`，否则会出现"PC 转一次灰度、PL 又转一次"

——能跑但结果全错。协议帧头**没有**统计字段，统计值只在板端日志里，不能宣称旧客户端已经收到统计。

## 四、缓冲区与缓存一致性

* 输入缓冲 `uint32` × 像素数（4 B/px），输出缓冲 `uint8` × 像素数；

* 两种缓冲按 `(width, height, channels)` 复用，最多缓存 3 种尺寸，超限淘汰最旧并显式 `freebuffer()`；

* **写入后 `flush()`、读取前 `invalidate()`**，否则 CPU 读到旧缓存 / PL 读到旧数据；

* 单板单主控：DMA 是独占资源，服务用锁串行化。

## 五、单帧时序与判定

1. 清 MM2S/S2MM 的 IOC 完成标志（避免上一帧的标志被当成本帧完成）；

2. 写参数寄存器；

3. 两个 DMA 通道挂好长度（MM2S 发 `width×height×4`，S2MM 收 `width×height`）；

4. 置 `ap_start`，轮询 DMA 状态；

5. `DMASR.bit1`（Idle）置起 = 传输结束；`DMASR.bit25–28` 任一置起 = 内部错误（拍数/TLAST 异常）→ `ERR_DMA`；

   超时（默认 5 s）→ `ERR_TIMEOUT`。

耗时口径（两个数不许混写）：板端 `pl_ms` = 参数写入 + DMA 发送 + PL 处理 + DMA 接收 + 完成轮询；

系统端到端 = PC 采集 + 缩放 + 网络发送 + `pl_ms` + 网络回传 + 界面刷新。

