# B 当天任务交付报告

已完成70图清单校验、三路算法、40图开发和30图重复评估、分品种指标、10张粘连代表图、6个失败案例和C接口样例。未改原图、人工GT、板端或模型权重。

## 三个指标的真实变化

| 集合 | MAE 修改前到当前 | 完全正确率 修改前到当前 | 未解决比例 修改前到当前 |
|---|---:|---:|---:|
| dev | 1.875 → 1.475 | 37.5% → 40.0% | 67.5% → 42.5% |
| test | 2.267 → 1.700 | 23.3% → 33.3% | 73.3% → 56.7% |

MAE按abs(final_count-gt_count)逐图平均；完全正确率按绝对误差为0的图片比例；未解决比例按存在UNRESOLVED实例的图片比例。没有改公式、读GT凑数量或把未解决实例隐藏。status=OK仍可能数错，不能当作准确率。

该组实验改进：用开发集单粒面积分位数、圆形度和凹陷程度消除大而圆整单粒的错误报警；加入种子表面色彩响应的候选标记；在开发集有限验证后选择960输入和按品种冻结的NMS参数，三路同图使用相同检测结果。提高输入尺寸增加了CPU耗时，未宣称增加原图真实细节。前景掩膜仍在640x480原图上计算。

test之前已查看，该组实验明确为重复评估，不能称为新的独立测试。provenance保留上版指标和原始首次测试摘要。后续若要独立泛化结论，需要新增未用于调参的保留集。

## 文件与完成情况

- comparison.csv：全部70图、带split的逐图三路计数/误差/耗时。

- summary.csv：按集合和品种的指标；before_after.csv：与该组实验开始版本对照。

- dev/json、test/json：210份结果；相应triptych目录：70张完整三联图。

- triptych：10张选定粘连代表图，包含成功与失败，不只挑成功图。

- failures/analysis.csv和6张图：真实计数失败及未解决原因，人工实例复核状态如实保留。

- handoff：15张真实输入、45份模式结果、C调用说明和15图后台一致性检查。

- manual_review.json：人工复核确认mungbean_touch_004为平铺接触；不将困难全部归咎于遮挡。

70张照片和GT来自赛题提供的CSV，程序校验不等于代替人工逐粒计数。C的Qt界面15图显示验证、视频和操作卡属于C工作，本报告不将其标记完成。6个失败案例已经保留，但个别实例轮廓是否正确还需人工确认。

## 重跑方法

```powershell

Set-Location '.'

python -B algorithm\seed_separation.py check

python -B algorithm\seed_separation.py freeze --out results\开发阶段\frozen.json --imgsz 960 --iou 0.7 --overwrite

python -B algorithm\seed_separation.py eval --frozen results\开发阶段\frozen.json --split dev --out results\开发阶段\dev --overwrite

python -B algorithm\seed_separation.py eval --frozen results\开发阶段\frozen.json --split test --out results\开发阶段\test --overwrite

```

当前结果已经真实运行完成，无须再跑。按品种的覆盖参数在frozen.json的species_detector_profiles；不能只看detector默认值推断各图实际IoU。每份JSON记录effective_detector_parameters。没有独立cache目录，全图原始框保存在baseline_0的raw_detections中。

## 仍未解决与开发阶段建议

密集团仍有漏分、过分割和UNRESOLVED，尤其约10像素短轴的绿豆。人工复核确认是平铺接触，因此应继续改善真实成像尺寸、漫射照明、实例标记和密集场景训练，而不是将所有漏分解释为遮挡。禁止按已知40/50的GT反推分割数量。

## 人工复核追加

用户已确认redbean_touch_005中：原YOLO有重复框，固定分水岭有过分割，自适应仍有两粒未分开的情况。该图自适应计数40、GT40、绝对误差0，但实例分割失败。已加入第6个失败案例，并在JSON中记录manual_instance_review=FAIL；该图原本已标UNRESOLVED。计数指标不因此改写，也不把它宣传为完全分割成功。

