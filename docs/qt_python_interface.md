# Qt 与 Python 接口

当前界面为 `src/ui`，由 `QProcess` 启动Python，状态JSON和图像均以临时文件替换方式发布。所有运行产物位于 `outputs`。

## 入口与路由

| 操作 | 入口 | 板端协议 |
|---|---|---|
| 单图/批量，本地 | src/app/comparison_runtime.py | 不调用板卡 |
| 单图/批量，板卡 | src/app/scene_runtime.py --board host:port | HTTP /frame，默认9001 |
| 实时，网络摄像头 | src/app/network_backend.py --request request.json | AGF1接收默认9100；可选HTTP转发 |
| 实时，板卡/回放 | src/adapters/detection_adapter.py | board使用AGV1 TCP，默认9000 |

关闭检测时，Image/Batch改用scene_runtime.py --preprocess-only，不初始化模型；仍按来源决定是否传入--board。Qt不会把 --board 传给CPU专用的comparison_runtime.py。HTTP与TCP帧服务不能同时占用同一FPGA/DMA。

## request.json

| 字段 | 类型 | 含义 |
|---|---|---|
| mode / source | string | Live/Image/Batch；board/mock/net |
| output_dir | string | 当前运行目录 |
| width / height / frames | integer | 工作尺寸与帧数；frames=0持续运行 |
| host / port | string / integer | board源的TCP地址，默认192.168.2.99:9000 |
| net_port | integer | 电脑1发帧的接收端口，默认9100 |
| net_fpga | boolean | 网络帧是否必须经过真实FPGA |
| board_host / board_http_port | string / integer | HTTP板端地址，默认端口9001 |
| control_mode | string | manual/auto板端配置策略 |
| live_compare / live_pheno | boolean | 复用已加载模型生成分析结果 |
| detect / detector / conf | boolean / string / number | 检测开关、mock/auto、0至1置信度 |
| species | string | soybean/mungbean/redbean |
| input_dir / image_path | string | 输入目录或图片 |

## live_status.json 与回执

状态包括running、state、frame、preview、ai、latency和network。网络链路另外提供fpga、dma、board_receipt、board_error。板卡未参与时board_receipt为空，RTT/PL耗时为空值；失败时state=ERROR、running=false，preview/board_receipt/latency清空。

HTTP回执包括request_id、frame_id、applied_frame、hardware、simulated、applied_config、actual_registers、register_verified、input_sha256、output_sha256和fpga_dma_ms。客户端逐项核验对应请求及实际寄存器值，拒绝模拟、错帧、错图或不完整回执。fpga_dma_ms为驱动内含DMA耗时，transport_rtt_ms为电脑实测往返时间，分别展示。

单图和批量结果将回执放在frame.scene或image.scene中，原图和FPGA输出路径由结果明确声明。实时分析保存同帧图像，避免引用不断覆盖的cam_latest.png。缺测显示N/A，不以0替代实测值。

模型、分离和表型均在电脑执行，输入仍为原始BGR图；FPGA输出作为独立视图和验证依据。Golden一致性、种子数量和实例分割准确性属于不同指标。

协议细节见[AGF1帧协议](frame_protocol.md)和[板端TCP协议](../hardware/board/protocol.md)，实际操作见[复现指南](reproduction.md)。

网络源的network.fps和CSV的fps字段为接收速率；live_fps与CSV的processing_fps为启动处理后的累计完成帧率，包含该处理循环的板端往返与算法工作。AGF1未传感器级测量帧率，camera_fps为空，界面显示N/A。不能用接收速率或DMA耗时倒数代替系统处理帧率。
