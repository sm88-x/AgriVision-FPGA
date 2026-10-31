# 双电脑联调交付索引

| 项目 | 文件 | 结果 |
| --- | --- | --- |
| 双机稳定性 | `../../data/evidence/network/network_stability.csv`、`../../data/evidence/network/network_frames.csv` | 310.7 秒，收帧3062、处理3017、丢45、重连0，平均9.86 FPS |
| 动态计数 | `dynamic_count_10rounds.csv` | 18→17→…→8，10轮操作者确认正确；逐轮机器延迟因中途重启未对齐 |
| CPU占用 | `cpu_usage.csv`、`cpu_usage_summary.json` | 16逻辑处理器；后端中位38.63%，Qt中位0.55% |
| 时间戳协议 | `../../data/evidence/network/timestamp_validation.csv/json` | AGF1本地12帧，采集帧龄中位5.216 ms |
| Qt截图 | `../../data/evidence/demo/network_interface/qt_dual_pc_live.png` | 真实双机运行画面 |
| 运行手册 | `../../docs/dual_pc_runbook.md` | 防火墙、IP、发送端、接收端和排错 |
| 帧协议 | `../../docs/frame_protocol.md` | v2时间戳格式及旧版兼容 |

动态计数是人工观察确认，自动逐轮延迟没有补造。网络链路是 PC-only，

不经过 PYNQ。

