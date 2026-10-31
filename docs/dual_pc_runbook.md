# network 双电脑现场运行手册

## 角色

| 电脑 | 职责 |
| --- | --- |
| 电脑1 `10.68.164.113` | 只采集摄像头并发送 JPEG 帧 |
| 电脑2 `10.68.164.209` | 监听 9100、运行 YOLO 和 Qt 主控 |

两条电脑需连接同一局域网，并关闭 VPN/代理。地址在重新连接热点后可能变化，现场以

`ipconfig` 和 `Test-NetConnection` 的实际结果为准。

## 电脑2

先启动 Qt，数据源选“网络摄像头”，端口 `9100`，再点击“连接网络”：

```powershell

qt\agri_morph_gui_7\build_network\release\agrivision_gui.exe

```

首次使用需允许 TCP 9100 入站。管理员 PowerShell 中可添加：

```powershell

New-NetFirewallRule -DisplayName "AgriVision network TCP 9100" `

  -Direction Inbound -Protocol TCP -LocalPort 9100 -Action Allow -Profile Any

```

确认端口监听：

```powershell

Test-NetConnection <电脑2的IP> -Port 9100

```

## 电脑1

安装 Python 3 和 OpenCV 后，从仓库根目录运行：

```powershell

python app\python\capture_sender.py `

  --host <电脑2的IP> --port 9100 --camera 0 `

  --width 640 --height 480 --fps 10

```

正常日志：

```text

已连接 <电脑2IP>:9100

已发送 N 帧 | ... fps | ... KB/帧

```

当前发送端使用 B 侧统一帧头：

```text

4字节body长度 + AGF1头（帧号、纳秒时间戳、宽高）+ JPEG

```

接收端只接受 `AGF1`。现场必须同时使用远端最新

`src/software/app/capture_sender.py` 和接收端代码；便携包为

`network_pc1_sender_agf1.zip`，不能混用旧发送包。

## 现场判定

- Qt 网络状态显示 `LIVE`；

- 对端地址等于电脑1的 IP；

- 收帧数和处理数持续增加；

- `reconnects = 0`，`last_error` 为空；

- 预览和检测框持续刷新。

`Test-NetConnection` 会产生一次“连接后立即断开、0 帧”记录，这是端口探测，不是异常。

## 已知边界

该网络链路是 PC-only：`JPEG → YOLO → Qt`，不经过 PYNQ FPGA。

协议暂无采集时间戳，所以只能测收到帧之后的解码与检测耗时。

