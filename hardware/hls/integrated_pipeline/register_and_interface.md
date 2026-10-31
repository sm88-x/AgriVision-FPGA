# phenotyping V4 Candidate 1 接口

## 图像链路

沿用 detection：

PC 摄像头采集 → 同尺寸彩色原帧保留 →

PC 定点灰度 → TCP → PYNQ PS →

AXI DMA → PL V4 → AXI DMA →

PS TCP 回传 → PC。

YOLO 使用彩色原帧；表型算法使用同帧 PL 图及检测框。

YOLO 不在 PL 上运行。

## 图像格式

网络输入：Gray8，H×W，uint8，行步长 W 字节。

PL 输入：uint32，每像素 4 字节，低 8 位为灰度。

PL/网络输出：Gray8，H×W，uint8。

没有改变像素顺序、尺寸、输出拍数和 TLAST 规则。

## 新增 PL 统计

foreground_count：

实际输出图非零像素数。

roi_count：

指定半开矩形 [x0,y0,x1,y1) 内实际输出图非零像素数。

统计针对 out_sel 选择并经过最终边界处理的输出。

生产链路使用 out_sel=0。

每次 ap_start 对应一帧，局部累加器从零开始。

仅在本帧 ap_done 后读取统计输出。

下一帧开始前，PS 先保存本帧统计。

计数为 uint32，支持当前最大图像尺寸。

## ROI

范围：0<=x0<=x1<=width，0<=y0<=y1<=height。

空 ROI 返回0。

越界 ROI 由软件拒绝。

B 先撤销 YOLO 的 letterbox/resize，再提供图像坐标。

浮点框转换建议：左上 floor，右下 ceil，然后裁剪到图像范围。

ROI 验收处理完整图像，不先裁 ROI 再运行卷积。

本版每帧支持一个统计 ROI。

多个 ROI 验收通过同一完整图像重复处理完成。

## 网络兼容

仍使用 detection v1 协议，帧头未增加统计字段。

现有客户端收到的仍然是 PL 图像。

Live 模式默认 ROI=整帧。

统计在板端 roi_statistics.jsonl 中按该项验证运行目录和 frame_id 保存。

不能宣称旧客户端已经收到统计寄存器值。

如以后需要在 Qt 实时显示 PL 计数，应另行设计并共同确认元数据接口。

## 单粒表型

PL Pixel Count 不等于轮廓面积。

Area、Length、Width、Perimeter、Aspect Ratio、Circularity

由 B 的表型模块定义和计算。

当前边缘/形态学图可能不是填充种子掩膜。

若需要闭合、填充或轮廓筛选，明确标记这些步骤在 PC 上执行。

## 帧对齐

PC 使用 (session_id,frame_id) 对齐原图、检测和PL图。

板端日志使用独立运行目录区分会话。

同一单板运行期间只允许一个主控客户端进行验收。