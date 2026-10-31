# detection 训练配置记录

- 模型: YOLO11n(nano 级轻量模型)

- 权重来源: COCO 预训练(yolo11n.pt,官方 ultralytics assets)

- 数据: ai/dataset(单类 seed,100 图,997 框,划分 60/15/25,随机种子 42)

- 训练参数: imgsz=640, epochs=100, batch=16, device=0, seed=42

- 训练命令: python ai/model/train.py --epochs 100 --imgsz 640 --batch 16 --device 0

## 环境

- python: 3.14.7

- torch: 2.9.1+cu128 (cuda 12.8, available=True, device=NVIDIA GeForce RTX 5060 Laptop GPU)

- ultralytics: 8.4.176

- numpy: 2.5.2

