# 板端启动与恢复

以下命令从完整工程根目录运行。板卡为PYNQ-Z2；默认示例地址192.168.2.99，实际以板卡和电脑网段为准。当前推荐固件为hardware/firmware/integrated_pipeline_validated.bit及同名.hwh；它们的历史板测身份见[版本索引](../versions.md)。

## 冷启动自测

```bash
mkdir -p outputs/board
sudo python3 hardware/board/frame_service.py --selftest 10 --bitfile hardware/firmware/integrated_pipeline_validated.bit
```

先确认镜像自带pynq、numpy及同名HWH可用。自测会比较指定算子的PL输出与软件参考；通过不代表目标检测或表型精度已经通过验收。

## 按模式选择服务

单图、批量或网络帧转发FPGA使用HTTP：

```bash
sudo python3 hardware/board/scene_config_server.py --bitfile hardware/firmware/integrated_pipeline_validated.bit --port 9001 --log outputs/board/http_receipts.jsonl
```

直接相机实时帧链路使用TCP：

```bash
sudo python3 hardware/board/frame_server.py --bitfile hardware/firmware/integrated_pipeline_validated.bit --port 9000 --out outputs/board --frames 0
```

**两个服务不可同时占用同一个FPGA/DMA。** 切换时在原终端Ctrl+C停止，等待服务释放，再启动另一种。不要依次启动两个进程后让它们同时访问寄存器。

## 电脑侧检查

TCP服务启动后：

```powershell
python src/app/pc_client.py probe --host 192.168.2.99 --port 9000
python src/app/pc_client.py golden-check --host 192.168.2.99 --port 9000 data/fixtures/agricultural_images/seed_01_dense_640x480.png
```

HTTP路径通过Qt单图/批量，或[复现指南](../reproduction.md)的scene_runtime命令检查。核对hardware=true、simulated=false、register_verified=true、实际生效帧号、输入输出SHA及寄存器记录。mock服务仅用于协议测试，客户端不会接受它作为真板处理。

## 故障恢复

1. 连接超时：检查电源、网线、电脑网卡网段及端口，Jupyter能打开不代表9000/9001处理服务已启动。
2. 缺少寄存器或HWH：停止服务，确认bit/hwh同名成对且来自同一次实现；不要用其他版本HWH套用。
3. DMA超时：停止所有访问FPGA的进程，再重新启动一个服务；若仍失败，重启板卡并先运行自测。日志保留超时和错误，不将该帧计入成功样本。
4. GUI显示ERROR后，旧预览和硬件回执应失效。修复连接后重新开始，不能把上一次DMA时间当作本次测量。

重新构建的位流必须重新通过Golden对拍、寄存器检查、ROI统计和连续运行，不能直接继承归档位流的板测结论。历史回归与30分钟记录保留在data/evidence中；本次验证范围单独见[验证范围](../validation.md)。
