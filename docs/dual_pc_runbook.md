# 双电脑摄像头与 FPGA 联调

电脑1负责采集、JPEG编码和发送；电脑2运行Qt、模型与表型分析。两台电脑在同一可互通局域网，使用实际地址；需要FPGA时，电脑2还须能访问PYNQ-Z2。以下命令从工程根目录运行。

## 电脑2：接收与分析

```powershell
python tools/run.py -- --source net --detector auto --size 640x480
```

Qt中选择“网络摄像头”，接收端口默认9100。仅电脑处理时不勾选“网络帧经过真实FPGA”；原图、检测、分离及表型在电脑运行，FPGA图像为空，硬件耗时为N/A。

需要FPGA时，先按[复现指南](reproduction.md)在板上启动HTTP 9001服务，再设置板卡IP与HTTP端口，勾选“网络帧经过真实FPGA”。每个处理帧发送原始RGBX像素，校验返回图像、帧号、哈希和寄存器后展示。命令行等价开关：

```powershell
python tools/run.py -- --source net --detector auto --size 640x480 --net-fpga 1 --host 192.168.2.99 --board-http-port 9001
```

## 电脑1：摄像头发送

安装PC依赖后执行，将 `<receiver_ip>` 换成电脑2的局域网IP：

```powershell
python src/app/capture_sender.py --host <receiver_ip> --port 9100 --camera 0 --width 640 --height 480 --fps 10
```

没有摄像头时，可用图片测试传输（不能当作相机实测）：

```powershell
python src/app/timestamped_frame_sender.py --host <receiver_ip> --port 9100 --images data/datasets/seeds/raw/soybean_sparse_001.png --width 640 --height 480 --fps 2 --frames 10
```

两个发送入口都使用AGF1头：4字节长度、20字节帧号/采集时间/尺寸头、JPEG。不要混用早期只有JPEG长度的发送程序。若系统询问网络访问权限，应允许实际使用的局域网接收端口；无需关闭防火墙。

## 核验与异常

1. Qt收帧和处理帧计数递增，对端地址与电脑1一致。
2. 勾选FPGA时核对hardware=true、simulated=false、寄存器验证、DMA与RTT；未勾选时显示NOT_USED。
3. 查看运行目录中的request.json、live_status.json和network_frames.csv。分析图像按帧保存，避免与最新预览错配。
4. 帧槽位采用最新帧覆盖策略；发送过快会丢弃旧帧并计入dropped。板端失败进入ERROR并清空旧硬件回执，不转为本机结果。

capture_age_ms依赖两台电脑时钟同步，不能当作纯网络延迟，也未包含最终界面绘制。DMA时间不等于整链路耗时。历史双电脑记录与本次测试范围见[验证范围](validation.md)。

网络源的network.fps和CSV的fps字段为接收速率；live_fps与CSV的processing_fps为启动处理后的累计完成帧率，包含该处理循环的板端往返与算法工作。AGF1未传感器级测量帧率，camera_fps为空，界面显示N/A。不能用接收速率或DMA耗时倒数代替系统处理帧率。
