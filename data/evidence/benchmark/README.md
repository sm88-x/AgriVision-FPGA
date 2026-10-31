# 开发阶段 批量分级性能

`batch_performance.csv` 记录对629个已分割实例连续运行5次分级与报告生成的耗时。

该指标只包含 Python 读取现有 JSON、面积分级、CSV/JSON 汇总和审计表写入，

不包含相机采集、YOLO 推理或粘连分离。

输入和规则：

- 输入：`data/evidence/segmentation_comparison/handoff` 中黄豆、绿豆、红豆各一张真实结果

- 规则：`data/config/grading_rules_v1.json`

- 命令：`python src/software/grading/batch_report.py --input <dir> --out-dir <dir>`

