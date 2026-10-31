# network 电脑1 → 电脑2 帧协议

本文描述 PC 网络摄像头链路，不替代 `hardware/board/protocol.md` 中的 PC ↔ PYNQ 协议。

## 线上格式

全部字段使用大端序：

```text

[4 字节 body_len]

[4 字节魔数 "AGF1"]

[4 字节 frame_id]

[8 字节 capture_ts_ns]

[2 字节 width]

[2 字节 height]

[JPEG 图像]

```

`body_len = 20 + JPEG字节数`。单帧 body 上限8 MiB。

## 收发纪律

- 先循环收到4字节 `body_len`，再循环收满整个 body；

- 校验 `AGF1` 魔数、帧号、时间戳、宽高和 JPEG 长度；

- TCP 长连接可以连续发送多帧；

- 非法长度、魔数或尺寸说明流已失步，接收端应断开当前连接并继续监听；

- 接收端只保留最新帧，旧帧覆盖并计入 dropped，避免无界排队。

协议常量与打包/解析代码位于 `src/software/app/timestamped_frame_protocol.py`。电脑1使用

`capture_sender.py`，电脑2独立接收可使用 `network_frame_receiver.py`，Qt主控使用

`network_backend.py`。

## 帧号与时间

- `frame_id` 由电脑1逐帧递增，用于检测丢帧和乱序；

- `capture_ts_ns` 是电脑1调用 `time.time_ns()` 的采集墙钟；

- `capture_age_ms = 电脑2当前墙钟 - capture_ts_ns`；

- 跨电脑解释 `capture_age_ms` 前必须确认两台系统时钟同步，否则数值包含时钟偏差；

- 接收端另外记录 JPEG 解码、YOLO 推理和最新帧覆盖数量。

## 实测证据

- 真实60秒板端联网日志：`data/evidence/scene_policy/configuration_smoke.log`

- `AGF1` 本地发送/接收逐帧验证：`data/evidence/network/timestamp_validation.csv`

- CPU和Qt后端耗时：`data/evidence/system_validation/dual_pc/cpu_usage.csv`

