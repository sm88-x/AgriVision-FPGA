# 版本与证据索引

各版本用于说明不同的功能与优化阶段。比较同功能性能时使用相同输入、算子配置、预热与统计口径；不能把增加功能后的耗时变化直接解释为优化收益。

| 版本 | HLS源码目录 | 功能与用途 | 主要证据 |
|---|---|---|---|
| baseline | hardware/hls/baseline | 固定尺寸流式Sobel基线 | hardware/build/reports/sobel_v0；tests/evidence/performance_summary.csv |
| pipelined | hardware/hls/pipelined | 行缓冲与流水化，保留基线接口 | hardware/build/reports/sobel_v3；PS/PL性能原始CSV |
| parameterized | hardware/hls/parameterized | 运行时尺寸、Sobel/Prewitt、阈值 | hardware/build/reports/sobel_v3_param；hardware/firmware/parameterized_edge.* |
| edge_morphology | hardware/hls/legacy/edge_morphology | 边缘与形态学组合 | hardware/build/reports/v4_morphology；hardware/firmware/edge_morphology_system.* |
| configurable_pipeline | hardware/hls/legacy/configurable_pipeline | 灰度、高斯、边缘、形态学可配置链 | hardware/build/reports/v4-pipeline；hardware/firmware/configurable_pipeline.* |
| integrated_pipeline | hardware/hls/integrated_pipeline | 完整处理链与前景/ROI统计，推荐实现 | docs/evidence/integrated_pipeline；hardware/firmware/integrated_pipeline_validated.*；docs/evidence/rebuild_20261031 |

## 统一构建接口

每个HLS目录包含 `hls_config.cfg`、设计源码和测试平台。`python tools/build.py csim --variant <版本>` 运行对应C仿真，`hls`运行综合并导出IP。版本名与上表第一列一致。

`integrated_pipeline`与`parameterized`另外提供Vivado重建入口。`python tools/build.py all --variant integrated_pipeline`执行C仿真、HLS综合、BD校验、综合实现和固件导出。其他阶段保留HLS复现与原实现报告，不将缺少完整系统BD的版本描述为一键上板工程。

## 数据与结果

- `data/datasets/seeds`：70张固定种子图、人工计数与划分清单。
- `data/evidence/segmentation_comparison`：基础检测、固定分水岭、自适应分离的逐图结果、汇总、失败案例及冻结参数。
- `data/evidence/board`：不同硬件阶段的原始板测、性能与场景切换数据。
- `data/evidence/system_validation/fpga`：300帧、连续运行及配置差异验证；`dual_pc`是独立的PC双机链路。
- `data/evidence/phenotyping`：单粒测量、30粒人工核查及异常轮廓样例。
- `tests/evidence`：原始数据的只读核验脚本、摘要与自动测试记录。

`historical_frozen.json`和原始来源清单保留原记录指纹；当前源码清单记录打包文件的身份。重命名和路径调整不构成新的实验测量。

## C仿真验收

| 版本 | 帧数 | 结果 |
|---|---:|---|
| baseline | 5 | 通过 |
| pipelined | 6 | 通过 |
| parameterized | 40 | 通过 |
| edge_morphology | 116 | 通过 |
| configurable_pipeline | 67 | 通过 |
| integrated_pipeline | 72 | 通过 |

基线使用现有airfield、clock、plane、plant_a、plant_b五组256×256输入及其原Golden；原测试列表中未保留的文件不作为复现依赖。全部版本共306帧。验证记录见tests/evidence/hls_variants.json。

## 重建与归档固件身份

hardware/firmware下的parameterized_edge与integrated_pipeline_validated是原历史板测配对固件；hardware/firmware/rebuilt下分别保存parameterized_edge和agrivision_v4的本轮重建配对固件。新重建固件均未继承真板验收结论。参数化重建报告与哈希见docs/evidence/rebuild_20261031/parameterized/build_metadata.json；集成重建证据位于其父目录。
