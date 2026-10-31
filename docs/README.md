# 文档导航

## 建议阅读顺序

| 文档 | 用途 |
|---|---|
| [复现指南](reproduction.md) | 安装依赖、启动软件、重建硬件、部署板端 |
| [版本索引](versions.md) | 六个设计阶段与源码、固件和对比证据对应关系 |
| [验证范围](validation.md) | 测试结论、指标口径及未完成验证 |

## 接口与原始证据

- [Qt与Python接口](qt_python_interface.md)、[帧协议概览](frame_protocol.md)、[完整板端协议](../hardware/board/protocol.md)。
- [寄存器权威定义](../hardware/hls/integrated_pipeline/register_map_final.md)、[启动与恢复](hardware/startup_and_recovery.md)。
- [证据目录](evidence/README.md)、[自动核验索引](../tests/evidence/evidence_index.md)、[模型协作记录](collaboration/README.md)。
- [双PC独立链路](dual_pc_runbook.md)与[演示操作](demonstration_guide.md)是专项说明，不替代复现指南。

主流程只在复现指南维护；各模块说明记录其独有接口和适用限制。原始实验记录按目录保存，引用历史结论时以对应固件和输入身份为准。

技术报告、视频等参赛材料在工程之外独立提交，本目录保留运行、复现和接口所需文档。
