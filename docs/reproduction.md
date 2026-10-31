# 复现指南

## 环境与输入

PC固定环境为Python 3.14.3，依赖版本见 `requirements/environment.json`。在独立Python环境安装 `requirements/ai.txt`；仅运行参考算法可安装 `requirements/pc.txt`。板端使用PYNQ镜像自身Python环境，不直接套用PC锁定版本。

工具通过PATH或CLI参数定位，工程不依赖固定磁盘位置。以下命令从项目根目录执行。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements/ai.txt
.venv/Scripts/python tools/verify.py
```

验证顺序为语法与文档链接、证据指纹、参考不变量、形态学校验、板端服务mock、数据集和软件运行回归。输出写入 `outputs/tests`。

## 应用构建与启动

安装Qt Widgets及匹配的MinGW，并进入配套编译环境（验证环境为Qt 6.12.0 / MinGW 13.1）。

```powershell
python tools/build.py qt
python tools/run.py -- --source mock --detector mock
```

qmake或make未在PATH时，使用 `--qmake <工具路径> --make <工具路径>`。运行库不在PATH时，使用 `python tools/run.py --qt-bin <Qt运行库目录> -- ...`。Windows入口为 `src/software/launchers/run_gui.bat`，可用环境变量AGRIVISION_PYTHON指定解释器。

载入固定表型示例：

```powershell
python tools/run.py -- --load data/evidence/phenotyping/objects.json
```

单张图片的完整软件分析：

```powershell
python src/software/app/comparison_runtime.py --image data/datasets/seeds/raw/soybean_sparse_001.png --species soybean --out outputs/example/comparison.json --pheno-out outputs/example/objects.json
```

Image/Batch执行本机软件分析，来源选本地；实时模式可连接板卡进行像素预处理，当前帧的检测/分离/表型仍在CPU执行。单图/批量分析入口不接受--board，界面会提示支持范围。载入结果用于复核已有文件；mock回放与真实板卡处理分别标识。真实检测默认CPU，记录权重、输入哈希和conf/iou/imgsz/device，不因环境差异静默改变口径。

## 硬件复现

在Vivado/Vitis 2026.1环境中执行：

Windows下请将工程放在磁盘浅层的英文目录。Vivado生成的IP层级较深，过长路径可能超过其260字节限制；应缩短工程根目录或选择更短的输出目录。所有命令仍从工程根目录执行，--out的相对路径也按工程根目录解析。

```powershell
python tools/build.py csim --variant all
python tools/build.py all --variant integrated_pipeline
```

第一条依次验证六个保留版本，第二条重建推荐实现。工具不在PATH时使用 `--vitis-run`、`--vpp`、`--vivado`指定工具位置。源码与版本关系见[版本索引](versions.md)。

默认输出为 `outputs/hls_integrated_pipeline` 和 `outputs/vivado_integrated_pipeline`。已有Vivado工程不会被覆盖，重跑使用 `--out outputs/vivado_integrated_pipeline_run02`。输出包含bit、hwh、xsa与实现报告；bit/hwh必须成对使用。

## 板端部署

将完整工程结构复制到PYNQ-Z2，先使用已归档板测固件进行自测：

```bash
sudo python3 hardware/board/frame_service.py --selftest 10 --bitfile hardware/firmware/integrated_pipeline_validated.bit
sudo python3 hardware/board/frame_server.py --bitfile hardware/firmware/integrated_pipeline_validated.bit --port 9000 --out outputs/board --frames 0
```

网络地址、端口和尺寸按实际连接配置。重新构建的位流需要重新完成寄存器核对、Golden对拍、ROI统计和连续运行验证，不能直接继承已归档固件的板测结果。连接拒绝或响应超时进入ERROR/OFFLINE并清空旧预览。

## 结果核验

原始性能记录及其统计口径见 `tests/evidence`；70图种子评估中的test为重复评估集，不能解释为全新独立测试集。实例失败和异常测量保留人工复核标记。历史73/78检出差异的另一环境资料不完整，当前固定CPU的五帧测试结果为73粒且重复一致。

## 解压后的最短核验路径

RAR解压到新目录后，从包含LICENSE和src/software的根目录运行上述命令。先运行tools/verify.py，再载入data/evidence/phenotyping/objects.json查看历史结果；需要真实推理时运行单图命令。新机器推理数值若不同，先比较environment.json、模型权重SHA、输入SHA与推理参数，再判断算法差异。

参数化历史版本同样支持统一构建：python tools/build.py all --variant parameterized。仅校验BD可在HLS导出后运行python tools/build.py vivado --variant parameterized --stage validate。Vivado输出目录必须为空，重复构建通过--out指定新目录。其余保留版本支持C仿真和HLS综合，不宣称全部具备独立Vivado系统脚本。
