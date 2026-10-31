# 开发阶段 CPU 占用记录

测试链路：本地回环真实种子帧 → `network_backend.py`（YOLO + 实时三路对比）→ Qt。

未接 PYNQ。

机器：16 个逻辑处理器。

| 进程 | CPU 中位数（占整机） | CPU 峰值（占整机） |
| --- | ---: | ---: |
| Python 网络后端 | 38.63% | 40.06% |
| Qt 主控 | 0.55% | 0.62% |

采样 12.24 秒，原始每秒数据见 `cpu_usage.csv`，汇总见 `cpu_usage_summary.json`。

该结果包含 YOLO 和实时分离分析，主要用于证明 Qt 界面本身不是 CPU 瓶颈；

不能把它换算成 FPGA 板端占用。

