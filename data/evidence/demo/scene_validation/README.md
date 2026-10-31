# 开发阶段 Qt 功能验证

## 运行环境

- Qt 6.8.0 / MinGW 13.1

- 数据：`data/evidence/demo/scene_examples/scene_example.json` 与真实 开发阶段 单粒结果

- 板卡：未连接；本页验证 UI、标定计算和 px/mm 显示，不冒充板端回执

## 页面与结果

| 截图 | 验证内容 |
| --- | --- |
| `manual_scene.png` | Manual 模式、真实结果载入、4个目标显示 |
| `automatic_scene.png` | Auto 模式、真实结果载入、4个目标显示 |
| `scale_calibration.png` | Calibration 页、机位确认、标定可加载、毫米选项启用 |
| `center_measurement.png` | 中心 ROI 内真实实例的 px/mm 数值切换 |
| `scene_comparison.png` | 载入15张真实 handoff 对比结果；首图 YOLO/固定/自适应均为40，GT=40 |

`data/evidence/demo/scene_control.json` 在 Auto 模式测试后保留为 `mode=auto`、`setup_id=camera_fixed`。

## px/mm 数值核对

`center_object_test.json` 来自真实 `soybean_sparse_003` 结果中的 Seed #007，

其 bbox 完全位于 `camera_calibration.json` 的中心 ROI 内。

界面实测：

```text

length: 20.00 px / 5.75 mm

width:  17.00 px / 4.88 mm

area:   274.5 px² / 22.7 mm²

```

反算比例约为 3.48 px/mm，与 `camera_calibration.json` 的 3.4802298775 px/mm 一致。

## 边界

- Manual/Auto 已能切换；没有连接 PYNQ，因此没有“实际寄存器已生效”的板端回执。

- Calibration 页使用 界面侧固定机位标定做界面验证。

- 当前移动相机场景仍必须按 `data/calibration/scale_calibration/README.md` 做同帧尺子标定。

