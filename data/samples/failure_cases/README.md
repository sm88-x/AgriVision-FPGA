# 开发阶段 失败样例

`network_zero_frame.json` 记录真实双机联调中出现的一次“TCP 已连接但 0 帧”现象。

原因是第二台电脑使用 `Test-NetConnection` 做端口探测，工具连接后会立即关闭，并非发送端

摄像头或网络故障。正式运行 `capture_sender.py` 后，同一条链路恢复正常送帧。

该样例用于区分：

- TCP 端口可达；

- 发送端确实开始送 JPEG；

- 接收端确实完成解码和检测。

