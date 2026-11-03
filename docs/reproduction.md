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

qmake或make未在PATH时，使用 `--qmake <工具路径> --make <工具路径>`。运行库不在PATH时，使用 `python tools/run.py --qt-bin <Qt运行库目录> -- ...`。Windows入口为 `src/launchers/run_gui.bat`，可用环境变量AGRIVISION_PYTHON指定解释器。

载入固定表型示例：

```powershell
python tools/run.py -- --load data/evidence/phenotyping/objects.json
```

单张图片的完整软件分析：

```powershell
python src/app/comparison_runtime.py --image data/datasets/seeds/raw/soybean_sparse_001.png --species soybean --out outputs/example/comparison.json --pheno-out outputs/example/objects.json
```

Image/Batch 的“本地回放”使用上述 CPU 入口；选择“板卡实时”时，Qt 改用 `src/app/scene_runtime.py --board <板卡IP>:9001`，逐张发送原图并接收 FPGA 输出和实际配置回执。检测、分离、表型仍在电脑上以原图运行，FPGA 输出独立展示。`comparison_runtime.py` 本身不接受 `--board`，不能把它与板端入口混用。实际检测使用冻结模型参数与 CPU，记录权重、输入哈希及推理设置。

板端服务启动后，命令行验证单图：

```powershell
New-Item -ItemType Directory -Force outputs/example | Out-Null
python src/app/scene_runtime.py --image data/datasets/seeds/raw/soybean_sparse_001.png --species soybean --board 192.168.2.99:9001 --out outputs/example/comparison.json --pheno-out outputs/example/phenotyping.json
```

批量时将 `--image <图片>` 改为 `--dir <目录>`。一批只选一个品种；带标准品种前缀的混合目录只分析所选品种，不给整批套用同一个 GT。启用检测的板端入口要求原始 640×480 分辨率，不静默缩放后沿用标定。板端入口的输出目录须预先建立。

界面运行配置和新增标定写入 `outputs/config`；没有运行配置时读取随附样例。毫米结果还需用户确认机位一致，并逐目标通过尺寸、setup_id 和中心 ROI 检查。载入旧文件不代表重新实测。

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
mkdir -p outputs/board
sudo python3 hardware/board/frame_service.py --selftest 10 --bitfile hardware/firmware/integrated_pipeline_validated.bit
# 单图、批量和网络帧转发使用 HTTP 9001：
sudo python3 hardware/board/scene_config_server.py --bitfile hardware/firmware/integrated_pipeline_validated.bit --port 9001 --log outputs/board/http_receipts.jsonl
```

直接相机实时链路使用 TCP 9000；先停止上面的 HTTP 服务，再启动：

```bash
sudo python3 hardware/board/frame_server.py --bitfile hardware/firmware/integrated_pipeline_validated.bit --port 9000 --out outputs/board --frames 0
```

同一 FPGA/DMA 不允许由两个独立服务进程同时占用；切换 HTTP/TCP 前在原终端 Ctrl+C 停止服务。双电脑相机链路见[双电脑运行手册](dual_pc_runbook.md)。网络地址、端口和尺寸按实际连接配置。重新构建的位流需要重新完成寄存器核对、Golden对拍、ROI统计和连续运行验证，不能直接继承已归档固件的板测结果。连接拒绝或响应超时进入ERROR/OFFLINE并清空旧预览。

## 结果核验

原始性能记录及其统计口径见 `tests/evidence`；70图种子评估中的test为重复评估集，不能解释为全新独立测试集。实例失败和异常测量保留人工复核标记。历史73/78检出差异的另一环境资料不完整，当前固定CPU的五帧测试结果为73粒且重复一致。

## 解压后的最短核验路径

RAR解压到新目录后，从包含LICENSE和src的根目录运行上述命令。先运行tools/verify.py，再载入data/evidence/phenotyping/objects.json查看历史结果；需要真实推理时运行单图命令。新机器推理数值若不同，先比较environment.json、模型权重SHA、输入SHA与推理参数，再判断算法差异。

参数化历史版本同样支持统一构建：python tools/build.py all --variant parameterized。仅校验BD可在HLS导出后运行python tools/build.py vivado --variant parameterized --stage validate。Vivado输出目录必须为空，重复构建通过--out指定新目录。其余保留版本支持C仿真和HLS综合，不宣称全部具备独立Vivado系统脚本。

关闭检测时，单图/批量统一通过scene_runtime.py的--preprocess-only路径，只保存原图、可选FPGA图与回执，不加载模型；板卡来源仍使用HTTP 9001。本地查看不生成FPGA图或表型。
