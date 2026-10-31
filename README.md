# AgriVision种子智检系统

基于PYNQ-Z2的软硬件协同种子分析系统。自研PL流水线完成灰度、高斯、Sobel/Prewitt、阈值、形态学与前景/ROI统计；软件完成目标检测、粘连分离、单粒表型和Qt展示。

## 阅读顺序

1. [版本与证据索引](docs/versions.md)：六个硬件实现阶段及对应源码、固件、数据与对比结果。
2. [复现指南](docs/reproduction.md)：安装依赖、离线验证、应用构建及板端部署。
3. [验证范围](docs/validation.md)：已验证内容、统计口径和适用限制。
4. [文档导航](docs/README.md)：模块接口、专项操作及原始证据。

本仓库提供代码、依赖、复现说明和实验数据。技术报告、视频、海报及其他参赛提交材料独立提交，不包含在工程或代码包中。

## 推荐版本

推荐使用 `hardware/hls/integrated_pipeline` 与 `src/software/ui`。原板测固件为 `hardware/firmware/integrated_pipeline_validated.bit/.hwh`；重新构建的固件必须重新完成板级验收后再替换。其他版本保留用于分析性能、资源与功能演进。

## 快速复现

验证环境为Python 3.14.3、Qt 6.12.0 / MinGW 13.1、Vivado/Vitis 2026.1。在项目根目录执行，所列路径均为工程相对路径。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements/ai.txt
.venv/Scripts/python tools/verify.py
```

在Qt配套编译环境中执行 `python tools/build.py qt`，随后 `python tools/run.py -- --source mock --detector mock`。mock用于软件演示，不代表真实PL运行。完整硬件构建使用 `python tools/build.py all`；各版本C仿真使用 `python tools/build.py csim --variant all`。

## 工程结构

| 目录 | 内容 |
|---|---|
| src/software | 当前应用、检测、分离、表型、通信适配及保留的界面版本 |
| hardware | 六个HLS版本、板端服务、驱动、固件和Vivado构建脚本 |
| tests | 自动验证、独立参考检查与证据核验 |
| data | 数据集、模型、固定输入、Golden、标定及按功能分类的测试结果 |
| docs | 版本对比、运行说明、接口、硬件证据和模型协作记录 |
| requirements | PC、AI及板端依赖说明 |
| skill | DMA链路检查与性能统计技能包 |
| tools | 构建、启动、验证和打包入口 |

构建和运行时自动生成 `outputs/`，其中内容不属于提交源码。代码包不包含Git元数据、缓存和重复交付物。

## 结果与口径

640×480 Sobel历史对比：PS NumPy参考中位216.3076 ms，参数化PL驱动内端到端3.6066 ms，约59.98倍。该结果不含采集、网络、模型和界面，也不代表相对优化OpenCV的速度。

集成流水线离线实现曾得到LUT 4685、FF 5940、BRAM 6.5 tile、DSP 5、WNS +1.598 ns（100 MHz约束）；源报告及固件身份见版本索引。Golden PASS用于指定算子一致性，不等价于识别或实例分割准确。未标定时采用px与px²。

原创代码采用[MIT许可证](LICENSE)。第三方依赖、权重及图片不自动由代码许可证覆盖，来源登记见[data/provenance.md](data/provenance.md)。
