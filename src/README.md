# 软件模块

| 目录 | 职责 |
|---|---|
| ui | 推荐Qt Widgets界面，qmake工程为agri_morph_gui.pro |
| app | 采集、网络、状态文件和单图/批量分析入口 |
| ai/inference | YOLO权重加载、CPU推理及可复现参数记录 |
| algorithm | 分离基线与adaptive自适应算法；保留失败案例 |
| phenotyping | 单粒几何指标、尺度标定和异常测量，见本目录专用说明 |
| adapters | 检测结果与应用接口适配 |
| fpga | 场景策略与板端调用 |
| grading | 批次统计和规则分级 |
| legacy/qt | 保留的界面演进版本，用于功能与结果对比 |
| launchers | Windows启动入口 |

构建与启动统一见[复现指南](../docs/reproduction.md)。Image/Batch可选择本地回放或HTTP板端预处理，实时网络帧也可选择转发FPGA。检测、分离和表型均在电脑执行；FPGA图像和实测回执独立展示，不标记为PL推理。当前标定控件位于ui/scene_controls.h，运行配置写入outputs/config。
