# detection LLM 协作记录（C 侧：Qt 界面与工程）

角色分工里没有"谁来用大模型"这一项——记录的是**大模型建议 + 人工判断 + 实测结论**这条链，

以及哪些建议被否掉、为什么。

## 一、当天要做到什么（任务书 C 节）

在 live_stream 已有的 Qt Live 界面上加：AI Detection 开关、置信度阈值、bbox/序号/置信度叠加、

Seed Count 与目标列表（点条目高亮）、MODEL 状态与推理延迟卡片；下午接 B 的 `detect_seeds`，

Image ≥20 张、Live 连续帧、Start/Stop 恢复；最后出截图、视频、README 与证据索引。

## 二、建议 → 判断 → 采纳

| # | 大模型建议 | 人工判断 | 结果 |
| --- | --- | --- | --- |
| 1 | 沿用 live_stream 的架构（QProcess 拉起 Python 适配器 + 200ms 轮询 `live_status.json`），不重写界面框架 | 同意：SMX 的 gui_3 已经在用，重写会两套并行 | **采纳**，新建 `src/software/legacy/qt/detection_viewer`，只做增量 |
| 2 | 先让后端产出**确定性 mock 检测**（阈值+连通域），把界面先跑通，再接真模型 | 同意：C1 上午本来就要求"先用模拟数据完成界面" | **采纳**，`MockDetector` |
| 3 | 本机没有 Qt，用 `aqtinstall` 装 Qt 6.12 | 判断：**6.12.0 官方仓库缺元数据（`Updates.xml` 404）**，实际装 6.8.0 | **部分采纳**：装 6.8.0 + MinGW 13.1，版本差异写进文档 |
| 4 | 给 GUI 加 `--capture <png>` 离屏截图开关，用脚本批量产出界面证据图 | 同意：比人工截屏可复现，也能在无显示器环境跑 | **采纳**，并用它产出 6 张状态图 |
| 5 | 检测框在 Qt 侧按"原图像素 → 控件像素"缩放比绘制 | 同意：把映射留在画的地方，缩放后也不会错位 | **采纳**，`paint()` 里统一换算 |
| 6 | 用整链路 FPS 当"FPGA 性能"展示 | **否掉**：任务书第七节明确要求板端与系统两个口径分开 | 改为相机 FPS / Live FPS / AI 推理延迟分列 |
| 7 | 在 Qt 里直接读模型做推理 | **否掉**：C2 明确要求调用 B 的 `detect_seeds`，不在界面里重实现 YOLO | 只保留 HTTP/进程调用入口 |
| 8 | 为接入 YOLO 顺手改一版 HLS 内核 | **否掉**：A 侧要求不为接 YOLO 动内核，硬件保持稳定版本 | 未改动任何 RTL/bit |

## 三、实测结论（全部可复现）

**界面（6 个状态，离屏渲染真实截图）**

| 状态 | 检测框 | 说明 |
| --- | --- | --- |
| Live + 检测开 + conf 0.25 | 7 | 640×480 主档 |
| Live + conf 0.60 | 5 | 阈值升高后框减少 → 控件确实作用于检测 |
| Live + 检测关 | 0 | 只跑 FPGA 链路，可对比 |
| Live + conf 0.05 | 7 | 低阈值（漏检/误检观察用） |
| Live + 320×240 | 6 | 换分辨率后框仍贴合（缩放映射正确） |
| Image 模式单图 | 9 | Image 入口可用 |
| `--detector auto` 通路（加载外部模块） | 6 | 界面显示 `MODEL STUB`，通路验证用，非精度证据 |

**Image 模式批量**：54 张（真实相机帧 10 + 板端 PL 灰度 10 + 仓库测试图 12 + B 测试集预测图 22 张）

逐张检测，检测框 0–52（平均 5.6），单张耗时平均 **2.91 ms**、最大 12.88 ms。

明细 `docs/evidence/detection_interface/image_mode_summary.csv`，拼图 `image_mode_montage.png`。

**FPGA 侧（沿用 10-11 真板数据）**：300 帧连续处理 300/300 成功、超时 0、DMA 错误 0；

板端 `pl_ms` 中位 640×480 3.92 ms / 320×240 1.53 ms / 256×256 1.52 ms。

原始记录 `data/evidence/board/live_stream/`。

**真链路 Live（10-13 晚，板子借到后实跑）**：`--source board` 走

摄像头 → 以太网 → PYNQ PL V4 → 回传 → Qt，板端记录 **300 帧 300/300 成功、超时 0、

DMA 错误 0、协议错误 0**，`pl_ms` mean 3.9392 / median 3.9278 / min 3.8932 / max 4.1848 ms；

界面状态行显示 `CAMERA ONLINE | FPGA ONLINE | DMA READY`。

证据：`docs/evidence/detection_interface/live_board_300frames.log`、`live_board_frames.csv`。

第一轮截图（`ui_08`）检测框为 0，原因是**摄像头画面全黑**（房间无光/镜头未对亮处）；

后来在真实种子摆白底的场景下重拍并替换：**第 1224 帧、14 个检测框、相机 30.0 fps /

端到端 8.6 fps**（`PrintWindow` 直接抓运行中的窗口，不含桌面其它内容）。

不是链路或检测逻辑问题——同一次运行里检测钩子正常执行（`annot_latest.png` 有生成）、板端 0 错误。

**真链路对接时修掉的四个问题**（都写进 `docs/evidence/detection_interface/README.md` 第 3.3 节）：

① `src/software/app/pc_client.py` 旧草稿遮蔽 SMX 链路版本导致 `read_gray_image` 缺失；

② 界面传 `Live`、B 的后端只认小写；③ `frames=0` 语义不同（持续 vs 0 帧）；

④ 真链路与 mock 的状态字段名不同（`frame_id/fps.*/camera+fpga+dma` vs `frame/camera_fps`）。

## 四、必须说明的限制（不粉饰）

1. **真模型最后跑通了**（过程有些反复，如实记录）：

   - 早先官方源卡在 168 MB、阿里云源 ~300 KB/s 停滞，**装不上**；于是先用**同签名 stub**

     验证了"加载外部模块 → 调用 `detect_seeds` → 框与状态进界面"的接口通路

     （`ui_07_detector_auto.png`，界面显示 `MODEL STUB`，当时声明过这不是精度证据）。

   - **换了校园网之后**实测官方源 3.5 MB/s；用 curl 断点续传把 torch 轮子拉下来装好

     （`torch 2.9.1+cpu` + `torchvision 0.24.1` + `ultralytics 8.4.177`），

     **B 的 `best.pt` 在 10 张真实相机帧上跑通**：检测数 5–38（平均 13.8），

     置信度 0.83–0.95，CPU 约 **28 ms/帧**；界面 `MODEL READY`（`ui_09_model_real.png`）。

   - 中途踩到 pip 解析失控（它想顺带升级 torch 到 2.14.1 并尝试源码编译 matplotlib，

     白下了几百 MB）。改用 `--only-binary=:all:` + 把 torchvision 锁到与 torch 2.9.1 配套的

     0.24.1 才止住——**这条经验值得留给以后装环境的同学**。

   - ⚠️ 另一个坑：**板端灰度帧（`pl_gray_*`）不能喂 YOLO**（同图 29 框 vs 178 假框），

     YOLO 的输入必须是原始彩色帧。

2. **演示视频不是屏幕录制**：本机不允许 GUI 录屏，视频由脚本逐帧渲染合成；其中界面帧是 Qt

   `grab()` 的真实输出，检测帧是真实相机帧跑检测的结果。

3. **Live 真链路已补跑并留下截图**（10-13 晚）：板端 300 帧 0 错误（见上）；

   `ui_08_live_board_real.png` 是真实种子场景下的界面截图（14 个框）。

   另有两个操作坑记录在 `docs/evidence/detection_interface/README.md` 第 3.3 节：

   残留 Python 适配器进程会一直占着摄像头（后续相机只能读到全黑帧，实测 `mean=0.00`）；

   界面默认 1560×940，在 1280×800 的笔记本上要加 `QT_SCALE_FACTOR=0.6` 才能整屏显示。

