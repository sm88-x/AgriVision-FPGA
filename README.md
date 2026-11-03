# AgriVision种子智检系统

基于PYNQ-Z2的软硬件协同种子分析系统。自研PL流水线完成灰度、高斯、Sobel/Prewitt、阈值、形态学与前景/ROI统计；软件完成目标检测、粘连分离、单粒表型和Qt展示。

**创新关键词：软硬件协同 · 单粒溯源复核 · 可复用验证技能**

- **软硬件协同**：自研可配置 FPGA 像素流水线承担重复的邻域运算，检测、分离与表型保留在软件端。
- **单粒溯源复核**：整盘计数、粘连复核与单粒形态关联到同一输入，结果可回查原图逐粒核验。
- **可复用验证技能**：固定参考、版本对照与失败样本沉淀为可复用的 DMA 验证与性能测试技能包。

## 阅读顺序

1. [版本与证据索引](docs/versions.md)：六个硬件实现阶段及对应源码、固件、数据与对比结果。
2. [复现指南](docs/reproduction.md)：安装依赖、离线验证、应用构建及板端部署。
3. [验证范围](docs/validation.md)：已验证内容、统计口径和适用限制。
4. [文档导航](docs/README.md)：模块接口、专项操作及原始证据。

本仓库提供代码、依赖、复现说明和实验数据。技术报告、视频、海报及其他参赛提交材料独立提交，不包含在工程或代码包中。

## 推荐版本

推荐使用 `hardware/hls/integrated_pipeline` 与 `src/ui`。原板测固件为 `hardware/firmware/integrated_pipeline_validated.bit` 与同名 `.hwh`；重新构建的固件必须重新完成板级验收后再替换。其他版本保留用于分析性能、资源与功能演进。

## 快速复现

验证环境为Python 3.14.3、Qt 6.12.0 / MinGW 13.1、Vivado/Vitis 2026.1。在项目根目录执行，所列路径均为工程相对路径。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements/ai.txt
.venv/Scripts/python tools/verify.py
```

在Qt配套编译环境中执行 `python tools/build.py qt`，随后 `python tools/run.py -- --source mock --detector mock`。mock用于软件演示，不代表真实PL运行。完整硬件构建使用 `python tools/build.py all`；各版本C仿真使用 `python tools/build.py csim --variant all`。

## 处理模式

| 模式与来源 | FPGA 路径 | 电脑端处理 |
|---|---|---|
| 单图 / 批量，本地回放 | 不参与 | 检测、分离和表型 |
| 单图 / 批量，板卡实时 | HTTP 9001：逐图预处理并返回回执 | 检测、分离和表型仍使用原图 |
| 实时采集，板卡实时 | TCP 9000：相机帧预处理 | 检测和界面 |
| 实时采集，网络摄像头 | 勾选“网络帧经过真实FPGA”后走 HTTP 9001 | 接收 AGF1 帧、检测与分析 |

板端结果必须通过请求标识、帧号、输入输出 SHA-256、配置与寄存器读回校验，才能显示为真板结果。板端失败会报错，不以本机结果代替。DMA 耗时包含驱动和数据搬运，与网络往返、CPU 算法耗时分别显示。

## 工程结构

| 目录 | 内容 |
|---|---|
| src | 当前应用、检测、分离、表型、通信适配及保留的界面版本 |
| hardware | 六个HLS版本、板端服务、驱动、固件和Vivado构建脚本 |
| tests | 自动验证、独立参考检查与证据核验 |
| data | 数据集、模型、固定输入、Golden、标定及按功能分类的测试结果 |
| docs | 版本对比、运行说明、接口、硬件证据和模型协作记录 |
| requirements | PC、AI及板端依赖说明 |
| skill | DMA链路检查与性能统计技能包 |
| tools | 构建、启动和验证入口 |

构建和运行时自动生成 `outputs/`，其中内容不属于提交源码。代码包不包含Git元数据、缓存和重复交付物。

## AMD 推荐目录对应

本工程按功能分组，与指南推荐结构对应如下：src → src；sim → tests/simulation 与 tests/unit；build → hardware/build；board → hardware/board、hardware/drivers、hardware/firmware；data → data；skill → skill；report中的技术证据与协作记录 → docs、tests/evidence，正式技术报告在工程外独立提交。

## 结果与口径

本次真板 HTTP 联调完成19次请求，其中手动10帧、自动3帧Golden均零差异；单图和两图批量分别得到40、40/40粒，另完成3帧网络图片转发。输入、回执、寄存器和输出图见[本次板测记录](tests/evidence/physical_board_validation.json)。这不替代新重建位流或摄像头长时间验收。

640×480 Sobel历史对比：PS NumPy参考中位216.3076 ms，参数化PL驱动内端到端3.6066 ms，约59.98倍。该结果不含采集、网络、模型和界面，也不代表相对优化OpenCV的速度。

集成流水线离线实现曾得到LUT 4685、FF 5940、BRAM 6.5 tile、DSP 5、WNS +1.598 ns（100 MHz约束）；源报告及固件身份见版本索引。Golden PASS用于指定算子一致性，不等价于识别或实例分割准确。未标定时采用px与px²。

原创代码采用[MIT许可证](LICENSE)。第三方依赖、权重及图片不自动由代码许可证覆盖，来源登记见[data/provenance.md](data/provenance.md)。
