# live_stream 帧传输协议（PC ↔ PYNQ-Z2）v1

> 状态：**A 侧定稿**（2026-10-11）。字节布局与 B 组草案 `src/software/app/frame_protocol.py`

> 完全一致，即本文与那份实现**共用同一份定义**——板端 `hardware/board/frame_server.py` 直接

> `import frame_protocol`，不另抄一份常量，避免两侧"以为写的是同一个通道数"。

>

> 任何一方要改字段，必须先改 `src/software/app/frame_protocol.py` 并同步本文，再通知三人。

## 一、传输层

* TCP 长连接（一条连接连续发多帧，不每帧重连；建连时间不计入单帧延迟）。

* 客户端默认连 `192.168.2.99:9000`（板端静态 IP 由当天接线确认，端口 `9000`）。

* 所有多字节整数**小端**（little-endian），与 x86 / ARM 一致。

* 一帧 = **固定 40 字节头 + payload**，payload 为裸像素，不带压缩。

## 二、40 字节头

| 偏移 | 字节 | 字段 | 说明 |
| --- | --- | --- | --- |
| 0 | 4 | `magic` | `b"AGV1"` |
| 4 | 2 | `version` | 协议版本，当前 `1` |
| 6 | 2 | `msg_type` | 1=REQUEST 2=RESPONSE 3=ERROR |
| 8 | 4 | `frame_id` | 帧号；响应原样回填 |
| 12 | 4 | `timestamp_ms` | 发送端单调时钟毫秒；响应原样回填（只用于比对，不参与算法） |
| 16 | 2 | `width` | 宽（像素），3 ≤ w ≤ 1920 |
| 18 | 2 | `height` | 高（像素），3 ≤ h ≤ 1080 |
| 20 | 1 | `channels` | 1=gray8，3=rgb24，4=rgbx32；**live_stream 实时链路固定 1** |
| 21 | 1 | `mode` | 0=Sobel 1=Prewitt |
| 22 | 1 | `morph_mode` | 0=bypass 1=erode 2=dilate |
| 23 | 1 | `flags` | bit0=enable_gray，bit1=enable_gauss |
| 24 | 1 | `pipeline_mode` | 0=显式组合（用上面各字段）1=Seed Preset（忽略其余字段） |
| 25 | 3 | `reserved` | 必须为 0，非 0 直接报错断开 |
| 28 | 4 | `threshold` | 0=饱和输出；>0=二值化阈值 |
| 32 | 4 | `payload_len` | payload 字节数，必须 == `width*height*channels` |
| 36 | 4 | `status` | 0=OK；非 0 见第四节错误码（仅响应/错误帧有意义） |

合计 **40 字节**（结构体格式 `"<4sHHIIHHBBBBBBBBIII"`，`struct.calcsize` 校验为 40）。

## 三、收发纪律（双方都必须遵守）

1. 先**循环收满** 40 字节头，校验 magic / version / payload_len，再按 `payload_len`

   循环收满 payload。**任何情况下都不能假设一次 `recv` 就拿到一整帧**——实测 TCP

   会把一帧拆成几百片，B 组自测里最小切成 7 字节一片仍要能收满。

2. `payload_len` 必须等于 `width*height*channels` 且不超过上限（1920×1080×4），

   不满足直接回 ERROR 帧（status 见下）并断开该连接。

3. 板端只认 `msg_type=REQUEST`；收到别的类型按协议错误处理。

4. 超时（默认每帧 5 s，可配）：回 `ERR_TIMEOUT` 后断开，**不允许把超时当成像素错误**。

5. 每次 socket 读都可能只给几个字节；`reserved` 非 0 视为长度/格式错误。

## 四、错误码（`status`）

| 值 | 名称 | 触发条件 |
| --- | --- | --- |
| 0 | `ERR_OK` | 正常 |
| 1 | `ERR_BAD_MAGIC` | 魔数不是 `AGV1` |
| 2 | `ERR_BAD_VERSION` | 版本不是 1 |
| 3 | `ERR_BAD_LENGTH` | payload 长度不符 / 超过上限 / reserved 非 0 |
| 4 | `ERR_BAD_SIZE` | 宽高越界（3~1920 / 3~1080） |
| 5 | `ERR_BAD_CHANNELS` | 通道数不在 1/3/4 内 |
| 6 | `ERR_TIMEOUT` | DMA 或接收超时 |
| 7 | `ERR_DMA` | DMA 内部错误（内部错误位被置起） |
| 8 | `ERR_INTERNAL` | 板端内部异常（形状不符、寄存器缺失等） |

错误帧同样是 40 字节头 + UTF-8 文本 body（`payload_len` = 文本长度），`frame_id`

回填请求的帧号，方便客户端定位是哪一帧出的问题。

## 五、live_stream 的像素与算法约定

* **实时链路固定 `channels=1`（gray8）**：PC 侧用 `run_pipeline.rgb2gray` 的定点式

  `(77R+150G+29B+128)>>8` 转灰度后再发送。**不要用 `cv2.cvtColor`**，BT.601 舍入

  不同会让逐像素 Golden 差 ±1。

* 因为输入已经是 8 bit 灰度，板端把 `enable_gray` 寄存器写 **0（透传）**：

  * `channels == 1` → `enable_gray = 0`

  * `channels == 4`（RGBX888，pipeline_validation 遗留路径）→ `enable_gray = 1`，此时由 PL 做 RGB2Gray

  * 头里的 `flags.bit0` 只作提示，**板端以 `channels` 为准**，避免"PC 已转灰度、

    PL 又转一次"这类看似成功实则全错的情况。

* ⚠️ **网络格式 ≠ PL 输入格式**：这份 V4 核的 PL 输入接口固定 **32 bit/像素（4 B/px）**，

  `channels=1` 时板端把灰度放进 32bit 字的**低 8 位**再送 DMA（`enable_gray=0` 即取低字节）。

  实测教训：按 1 B/px 送会让核只收到 1/4 帧、永远不吐 TLAST，

  现象是 `S2MM_DMASR=0x0` + `AP_CTRL=0x1` + 单帧超时。协议侧不用改，

  但**任何绕过 `frame_server.py` 直接写 DMA 的脚本都必须按 4 B/px 送**。

* `out_sel` 固定为 **0（最终 morph 输出）**：即

  `RGB2Gray(或透传) → Gaussian(可选) → Edge/Sobel|Prewitt → Threshold → Morphology`。

  需要看中间级（gray/gauss/edge）时走 Jupyter 调试脚本，不走线上协议。

* `pipeline_mode=1`（Seed Preset）等价于 `enable_gauss=1, mode=Sobel,

  threshold=40, morph_mode=Dilate`，板端忽略头里其余字段；PC 侧软件 Golden 用

  `run_pipeline.SEED_PRESET`，两侧同源。

* 分辨率：主档 640×480；抽检 320×240、256×256。切换尺寸后允许继续处理

  （缓冲区按尺寸复用，不每帧重新申请）。

## 六、寄存器映射（协议字段 → PL 寄存器）

寄存器偏移**一律从同名 `.hwh` 解析**，代码里不写死地址；hwh 里没有的名字会被跳过并

记录（避免"以为写了其实没写"）。详见 `hardware/board/frame_service_registers.md`。

| 协议字段 | 寄存器 | 说明 |
| --- | --- | --- |
| `width` / `height` | `width` / `height` | 每帧写 |
| `mode` | `mode` | 0=Sobel 1=Prewitt |
| `threshold` | `threshold` | 0=饱和 |
| `morph_mode` | `morph_mode` | 0/1/2 |
| `channels` | `enable_gray` | 1→0，4→1 |
| `flags.bit1` | `enable_gauss` | 1=开 3×3 高斯 |
| （固定） | `out_sel` | 恒为 0 |

## 七、性能口径（不许混写）

* **板端耗时 `pl_ms`**：只含 PL 参数寄存器写入 + DMA 发送 + PL 处理 + DMA 接收 +

  完成轮询（`SobelParamDriver.run_frame` 的返回值）。

* **系统端到端**：PC 采集 + letterbox 缩放 + 网络发送 + 板端 `pl_ms` + 网络回传 +

  Qt 刷新。两个数分开记，不能把 PC 采集时间说成 FPGA 性能。

* 300 帧验收口径：连续 300 帧，记录成功帧数 / 丢帧数 / 超时数 / DMA 错误数；

  **DMA 死锁必须为 0**，不声称"绝对零丢帧"，真实丢帧要记录并定位原因。

  live_stream 实测相机约 15 FPS（见 `docs/evidence/live_stream/camera_capability.md`），300 帧约 20 s。

* live_stream 真板实测（2026-10-11，300 帧、0 失败 0 超时 0 DMA 错误）：

  640×480 median **3.92 ms**、320×240 **1.53 ms**、256×256 **1.52 ms**；

  与 pipeline_validation 同口径基准无回归。原始记录见 `data/evidence/board/live_stream/`。

## 八、板端运行方式

把 `frame_server.py`、`frame_service.py`、`frame_protocol.py`、

`edge_morph_driver.py` 与 `configurable_pipeline.bit/.hwh` 放到同一目录

（板端 Jupyter 工作目录），然后：

```bash

# 真板

sudo python3 frame_server.py --bitfile configurable_pipeline.bit --port 9000 \

    --out . --log board_300frames.log

# 不占板子时先在 PC 上跑同一份代码验证协议（软件 Golden 顶替 PL）

python frame_server.py --mock --port 9000 --out .

```

`--mock` 与真板走**同一条代码路径**（只换 driver），所以 PC 上验过的协议，上板不用改。

服务端会写出 `frame_metrics.csv`（逐帧）与 `board_300frames.log`（汇总与异常）。

## 九、版本

| 项 | 值 |
| --- | --- |
| 协议版本 | v1（`version=1`，magic `AGV1`） |
| 定稿人 | A（今日 A 角色） |
| 单一定义源 | `src/software/app/frame_protocol.py` |
| 相关实现 | `hardware/board/frame_service.py`、`hardware/board/frame_server.py`、`src/software/app/pc_client.py` |

