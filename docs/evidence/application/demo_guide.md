# 农业演示与复现

## 文件准备

在板端建立独立的 `agrivision_demo` 目录，将项目 `hardware/drivers/` 中的 Dashboard、驱动、回归脚本及唯一一组配套 bit/hwh 复制进去。原项目路径保留，复制操作不得覆盖旧实验素材。

| 本目录素材 | 回归脚本要求的板端文件名 | 预期用途 |
|---|---|---|
| seed.jpeg | plant_a_640x480.png | 白底种子，Count=42 |
| leaf.jpeg | plant_leaf_640x480.png | 多片叶片，Count=7 |
| microscopy.jpeg | microscopy_640x480.png | 显微边缘与整体区域展示 |

这里的 `.png` 是历史脚本要求的文件名，Pillow 按内容解码 JPEG；仅在独立演示目录内复制命名，不改原图。素材来源为 `data/evidence/board/application_regression/`，不要使用 `data/fixtures/images/` 中另一批同名自然场景图片替代。

## 操作顺序

1. 在上述目录创建 Jupyter Notebook，执行 `%run dashboard.py`。

2. 上传种子图，选择 FPGA、Sobel、threshold=40、min_area=100、闭运算 1 次。

3. 运行后确认 Golden PASS，再讲解原图、边缘图、编号和表型统计。该图计数为 42，平均面积约 883.65 px²。

4. 切换 Prewitt，重新运行。计数仍为 42，面积可能不同，不能用“计数一致”代替所有指标相同。

5. 上传叶片图（多片），说明输出为 7 个叶片区域。显微图展示轮廓，不称为细胞计数。

6. 执行 `%run -i application_regression.py`，复用 Dashboard 初始化的对象。检查输出 CSV 的 12 行记录，其中 FPGA 11 行全部 PASS，5 个种子稳定性用例计数一致。

## 对结果的解释

面积是外轮廓包围面积，包含内部孔洞；长宽来自最小面积旋转矩形；周长为轮廓弧长。指标属于缩放补白后的 640×480 分析坐标，不能直接写成 mm 或 mm²，也不能直接视为原始分辨率下的测量。

水印、阴影、粘连及复杂背景会影响结果。Golden FAIL 时界面停止表型测量，避免将不一致边缘继续用于应用统计。发生 DMA 超时后停止连续调用，恢复硬件后重新初始化。演示吞吐应以完整回归总时间另计，边缘 FPS 不代表界面刷新率。

