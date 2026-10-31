# detection 图像与检测接口

## 运行链路

USB 摄像头由 PC 采集。

PC 保留同尺寸 BGR 彩色帧，并生成定点 Gray8 图送入 PYNQ。

PYNQ PS 将灰度扩展为 uint32，低 8 位承载灰度，经 DMA 交给 PL。

PL 输出 Gray8，经网络回到 PC。

YOLO 暂定使用 PC 保留的 BGR 彩色帧。

FPGA 处理图供显示及后续表型计算使用。

## 帧身份

使用 (session_id, frame_id) 标识一帧。

session_id 每次建立客户端时重新生成。

frame_id 在该 session 内递增。

网络 v1 只传 frame_id，session_id 由 PC 本地管理。

检测结果必须携带发起推理时的 session_id 和 frame_id。

Qt 只给相同身份的图像叠加检测框。

切换会话后丢弃旧会话的检测结果。

## 内存图像

raw_bgr:

- uint8

- H×W×3

- B、G、R 顺序

- C 连续

- 每行 3×W 字节

input_gray 与 pl_gray:

- uint8

- H×W

- C 连续

- 每行 W 字节

两路图来自同一次 resize 后的图像，尺寸一致。

该 raw_bgr 是处理分辨率的彩色原帧，不是传感器原始分辨率图像。

## 灰度公式

Y = (77×R + 150×G + 29×B + 128) >> 8

## 网络

TCP 9000。

40 字节小端头 + 裸像素 payload。

实时请求 channels=1；正常响应 channels=1。

当前实现不发送 channels=3。

不修改 live_stream v1 帧头。

timestamp_ms 是 PC 单调时钟毫秒的低 32 位。

它不是 Unix 时间，也不能跨设备直接相减。

capture_time_ns 是 PC 读到图像时记录的主机墙钟时间，

不是相机曝光时间。

## 算法配置

mode=0

enable_gauss=1

threshold=40

morph_mode=2

pipeline_mode=0

## YOLO 输入与输出

默认输入 raw_bgr。

若模型接口需要 RGB，由 B 在推理入口明确转换一次。

bbox_xyxy 使用 raw_bgr 的 W×H 像素坐标系，原点在左上。

约定范围为 [x1,y1,x2,y2)，坐标可以是浮点数。

B 负责撤销模型内部 letterbox/resize，并裁剪到图像范围。

Qt 负责将该坐标映射到实际显示区域。

## 实时队列

A 的客户端按请求响应锁步处理。

B/C 接入异步推理时，等待队列最多保留一帧，丢弃旧等待帧。

队列丢帧必须计数。

Qt 不把旧帧框画到新帧图像上。

## 性能

PC RTT 与板端 pl_ms 分开报告。

网络 v1 不携带 pl_ms。

板端耗时按该项验证运行目录和 frame_id 从 CSV 对齐。

不得声称 YOLO 在 PL 上运行。

尚未做精度对照实验，不声称 FPGA 提高 YOLO 精度。