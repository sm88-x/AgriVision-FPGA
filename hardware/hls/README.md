# 硬件设计版本

baseline、pipelined、parameterized构成同功能Sobel基线优化链；legacy/edge_morphology和legacy/configurable_pipeline记录处理功能扩展；integrated_pipeline是含ROI统计的推荐实现。

每个目录均包含hls_config.cfg、设计源码和测试平台。统一C仿真入口为 `python tools/build.py csim --variant all`；单独版本通过--variant选择。完整功能、资源与原始结果对应关系见docs/versions.md。
