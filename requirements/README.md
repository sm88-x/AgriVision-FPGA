# 依赖说明

根目录requirements.txt通过-r包含ai.txt，供完整PC功能安装使用；它不能安装Vivado、Qt或PYNQ镜像。

| 文件 | 安装范围 |
|---|---|
| pc.txt | NumPy、OpenCV、SciPy、scikit-image等参考与图像算法 |
| ai.txt | pc依赖加Torch、Ultralytics，支持真实模型推理与完整离线核验 |
| board.txt | 板端额外依赖说明；以PYNQ镜像自带驱动与Python为准 |
| environment.json | 已验证环境的版本快照，用于解释跨环境复现差异 |

命令从工程根目录执行：python -m pip install -r requirements.txt。推荐独立虚拟环境；Qt Widgets/匹配MinGW和Vivado/Vitis 2026.1单独安装。详见[复现指南](../docs/reproduction.md)。
