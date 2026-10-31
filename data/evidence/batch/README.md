# 开发阶段 三档分级批量结果

本目录由 `src/software/grading/batch_report.py` 使用 `data/config/grading_rules_v1.json` 生成。输入是

`data/evidence/segmentation_comparison/handoff/samples` 中15份真实 `proposed_v1.json`：

- 黄豆：199 个实例

- 绿豆：234 个实例

- 红豆：196 个实例

合计629个实例，A/B/C 分别为183/198/248；其中56个非OK实例标记为不确定。

## 重要限制

仓库中没有找到任务书规定的官方分级阈值，因此 v1 规则是数据驱动的临时规则：

各品种 dev 集 `status=OK` 实例的面积三分位数：

- `A`：面积 ≥ 第67百分位

- `B`：面积 ≥ 第33百分位且小于第67百分位

- `C`：面积小于第33百分位

这些是“大/中/小”三档，不是未经确认的官方质量等级。相机机位、比例或分辨率变化后

必须重新标定。规则文件已标记 `requires_task_owner_review=true`。

## 文件

- `objects_graded.csv`：逐粒面积和 A/B/C 结果

- `summary.csv` / `summary.json`：按品种汇总

- 仓库根目录 `data/evidence/grading/grade_audit_30.csv`：三种种子各10粒审计样本

- `data/evidence/benchmark/batch_performance.csv`：629个实例连续5次批处理耗时

非 `OK` 实例不会被删除：仍给出面积档，同时记录 `uncertain=true`。

