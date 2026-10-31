# 集成流水线历史板测

本目录保留归档固件integrated_pipeline_validated.bit的历史真板结果，非新重建位流的板级验收。固件SHA-256为c7e8d00464a5082ef45b7d701d990487e6c1c2e8e254ea9b7f66c7f60eec6c12。PYNQ-Z2器件xc7z020clg400-1，目标100 MHz，核sobel_v4_pipeline_0，DMA axi_dma_0。

| 测试 | 帧数/状态 | PL驱动内中位/ms | PC往返中位/ms | 证据 |
|---|---|---:|---:|---|
| 冷启动固定640×480 | 300/300成功 | 4.0571 | 165.2 | frames_300.csv及summary |
| 连续运行约30.5分钟 | 8000/8000成功 | 4.0697 | 164.904 | stability_30min.csv及summary |

8000帧count_match均为True，记录的超时/DMA/协议错误及网络丢包为0。PL驱动内时间包含配置、DMA收发、完成轮询；不代表网络、采集、模型和显示的系统耗时。原记录给出的链路约4.4 fps，相机约29.7 fps，瓶颈仍在PL以外。

Golden口径：原实验说明为抽查10帧像素对拍零差异。CSV中pc_golden虽逐行记录PASS，但没有足够逐帧参考计算证据，故不扩展为8000次独立像素Golden。ROI/前景计数则有8000帧软件参考并排记录。服务日志中的板端日历年份未校准，仅用单调耗时及帧计数统计，不作为采集日期依据。

同机位场景切换：seed_preset白像素150,666（49.0%），sobel_thr80_erode白像素8,038（2.6%），图像见scene_configs。参数改变影响输出与耗时，不改变已实现的静态资源数量。

性能跨版本汇总见benchmark_v0_v3_v4.csv；对应原始输入与测试口径见tests/evidence及data/evidence/board。资源/时序来源见resources_timing.md。原始CSV和服务日志保持可追溯，重建固件验证状态见docs/validation.md。
