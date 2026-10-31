# Qt ↔ Python 接口清单（network C）

本文记录 **network Qt 主控**（`src/software/ui/`）与 Python 后端之间的**实际**接口，

依据是 detection/5/6 已在用的机制，network 只做最小扩展，不另发明一套协议。

## 一、进程模型

Qt 用 `QProcess` 拉起 Python 后端，一次“开始运行”对应一个后端进程：

```

Qt  ──写 request.json──▶  Python 后端（常驻，直到停止）

Qt  ──每 200ms 读──▶      live_status.json

Qt  ──写 stop.request + terminate()──▶  后端退出

```

默认后端（network）：`src/software/app/network_backend.py`

- `source = net`：自己监听 PC1 推来的 JPEG 帧（network 新增）；

- 其他 `source`：**原样转交** `src/software/adapters/detection_adapter.py`，detection/5/6 的

  `mock` / `hardware/board` 两条路径一行未改。

## 二、Qt → Python：`request.json`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mode` | str | `Live` / `Image` / `Batch`（后端会转小写） |
| `source` | str | `hardware/board` / `mock` / **`net`**（network 新增） |
| `output_dir` | str | 本次运行目录（状态、预览、日志都写这里） |
| `width` / `height` | int | 送进处理链路的尺寸（默认 640×480） |
| `frames` | int | 0 = 持续运行（后端换成大数，靠 `stop.request` 停） |
| `host` / `port` | str/int | `hardware/board` 源的板卡地址（默认 192.168.2.99:9000） |
| **`net_port`** | int | **network 新增**：`net` 源的监听端口（默认 9100） |
| **`preset`** | str | **network 新增**：FPGA 处理模式（`seed_preset` / `sobel_thr80_erode` / `sobel_thr80_dilate` / `prewitt_thr80_erode` / `prewitt_thr0_dilate`），仅 `source=board` 生效 |
| `detect` | bool | 是否开启检测 |
| `conf` | float | 置信度（**0~1**；界面滑块是 0~100，发请求时除以 100） |
| `detector` | str | `mock`（连通域假检测）/ `auto`（加载 `src/software/ai/inference/infer_seed.py` 真模型） |
| `input_dir` / `image_path` | str | `mock`/`Image`/`Batch` 的输入 |
| `mock_fps` | float | 本地回放帧率 |

> 注意：界面命令行 `--conf` 收的是 **0~1 的小数**（默认 0.25）。

> 传 `--conf 25` 会被滑块按 ×100 换算成 2500，被夹到 100，等于阈值 1.0 —— 真模型会一个框都不给。

## 三、Python → Qt：`live_status.json`

Qt 每 200ms 轮询一次，字段沿用 detection 适配器，network 增加 `network` 节点：

```json

{

  "running": true,

  "state": "LIVE | LISTENING | WAITING_FRAME | DISCONNECTED | STOPPED | FINISHED",

  "mode": "Live",

  "source": "net",

  "frame": 42,

  "camera_fps": 7.5,

  "live_fps": 7.5,

  "latency": {"rtt_ms": "", "pl_ms": "", "net_ms": 12.3, "full_ms": 12.3},

  "preview": {"cam": ".../cam_latest.png",

              "board": ".../board_latest.png",

              "annot": ".../annot_latest.png"},

  "ai": {"enabled": true, "model_state": "READY", "seed_count": 14,

         "infer_ms": 31.9, "input": "bgr", "source": "network-frame",

         "detections": [{"id": 0, "bbox_xyxy": [242,56,305,133], "confidence": 0.91,

                         "class_name": "seed"}]},

  "network": {"listen_port": 9100, "connected": true, "peer": "192.168.1.11:52123",

              "protocol": "JPEG length-prefixed", "frame_bytes": 19451,

              "frames_received": 42, "processed": 40, "dropped": 2,

              "fps": 7.5, "reconnects": 0,

              "last_error": "", "elapsed_s": 6.1}

}

```

板卡源额外回执（来自 `src/software/app/live_backend.py`，Qt 直接显示）：

```json

{"fpga": "ONLINE", "dma": "READY", "preset": "sobel_thr80_erode"}

```

这就是“FPGA 模式切换的真实回执”：Qt 下拉里选什么，板端实际跑的就是什么，状态行显示的是

**后端回报的 preset**，不是按钮文字。

预览图（`preview/*.png`）是原子写：Qt 读到半张图会保留上一帧，不会闪空。

## 四、电脑1 → 电脑2：帧协议（network 新增）

```

4 字节大端长度 N（1..12 MiB） + N 字节 JPEG

```

- 接收端用循环 `recv` 收满，**不假设一次 recv 拿到整帧**（处理分包/粘包）；

- 断线后接收端继续监听、状态置 `DISCONNECTED`，重连后 `reconnects +1`；

- 发送端 `src/software/app/timestamped_frame_sender.py` 断线自动重连，`--frames 0` 持续发送；

- 没有时间戳字段（协议最小版），所以 **采集→显示的总延迟无法拆分**，

  `latency.net_ms` 只表示“收到帧→解码→检测→写状态”这段。

## 五、Qt 端 network 新增的控件

| 控件 | 行为 | 状态来源 |
| --- | --- | --- |
| 数据源 = 网络摄像头 | 请求里 `source=net`、带 `net_port` | — |
| 网络端口 | 监听端口（默认 9100） | 写进 `request.json` |
| 🔗 连接网络 | 起后端、开监听端口 | 状态栏显示 `LISTENING/已连接` |
| ✂ 断开网络 | 写 `stop.request` 并结束后端 | 状态回 `STOPPED` |
| 「网络：…」状态行 | 端口、对端 IP、收帧数、fps、重连次数、最近错误 | `live_status.json.network` |
| FPGA 模式下拉 | 5 档 preset，仅板卡源可选 | 写进 `request.json.preset` |
| 「FPGA：…」状态行 | ONLINE/OFFLINE、DMA READY/ERROR、实际 preset | `live_status.json.fpga/dma/preset` |

## 五之二、最新帧优先与预热（network 修）

* 接收线程把帧写进“最新帧槽位”，旧帧直接被覆盖丢弃，并计入 `network.dropped`；

  实测：PC1 以 30 fps 发 60 帧、检测约 30 ms/帧 → 处理 54 帧、丢 6 帧，

  延迟不会随运行时间增长（任务书“禁止无界排队”）。

* 后端启动时先 `make_server()` 开监听端口，再加载模型并**预热一次推理**

  （真模型首帧约 2.2 s，torch 初始化 + 首图优化）；不预热的话演示开头几十帧会全被丢掉。

“开始运行/停止”与“连接/断开”在 network 实现里都作用于同一个后端进程：

连接 = 起服务并开始处理；断开 = 停服务。没有单独的“只监听不处理”模式（如实说明）。

## 六、network Qt 工程位置

| 项 | 路径 |
| --- | --- |
| 源码 | `src/software/ui/main.cpp`、`agri_morph_gui.pro`（TARGET `agrivision_gui`） |
| 构建 | `qmake ../agri_morph_gui.pro -spec win32-g++ && mingw32-make -f Makefile.Release -j4` |
| 产物 | `src/software/ui/build_network/release/agrivision_gui.exe`（**不入库**，`.gitignore` 已排除 `build*/`） |

> 任务书里写的 `src/software/legacy/qt/network_build/` 指的就是这个构建产物；为保持仓库 1~6 的编号习惯，

> 源码目录命名为 `agri_morph_gui_7`。

