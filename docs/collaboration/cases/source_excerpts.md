# 原始对话摘录与定位

以下为原文节选，省略长代码、附件本地隐私路径和无关内容。完整聊天由团队保留，thread/turn 标识供核查。

## 案例 01 Pillow 接口兼容性

来源 thread `01a109d1-5605-73e3-bad9-754d48c6a8a4`，turn `01a10a72-80f4-7923-9458-caa2889f5ebc`。

**用户原文**

图片读取失败

**模型原文节选**

报错原因明确：

```text

module 'PIL.ImageOps' has no attribute 'exif_transpose'

```

**板端的 Pillow 不提供这个接口，是我前面给出的代码没有兼容你的环境，不是图片损坏，也不是 FPGA 问题。**

你当前的种子和叶片图片可以先跳过 EXIF 方向校正，不必升级板端依赖。

**1．在第五个单元格的 `on_upload()` 中找到：**

```python

image = ImageOps.exif_transpose(source)

```

替换为：

```python

if hasattr(ImageOps, "exif_transpose"):

    image = ImageOps.exif_transpose(source)

else:

    image = source.copy()

```

旧环境下保留图片原始方向；以后上传带 EXIF 旋转标记的手机照片时，需要检查方向是否正确。

**2．按这个顺序重新运行**

为避免重复绑定按钮事件：

1. 运行**第四格**：重新创建界面控件。

2. 运行修改后的**第五格**：定义函数并绑定事件。

3. 运行**第六格**：`display(dashboard)`。

4. 在最新显示的 Dashboard 中重新上传图片。

**不用重新运行第一格，不用重新加载 Overlay 或分配 DMA 缓冲区。**

**3．先验证图片读取，再运行分析**

上传成功后应出现：

- 文件名和分辨率；

- `Original RGB` 原图；

- 可点击的 `Run Analysis`。

你截图中的参数是 `Threshold=75、Min area=100`。若继续测试刚才的种子图，改回：

```text

Threshold = 40

Min area = 1000

Closing = 3×3，一次

```

先用 PS 验证界面，再切 FPGA。我没有修改你的任何文件。

（以上为原文节选，后续内容省略。）

## 案例 02 种子噪声与面积过滤

来源 thread `01a109d1-5605-73e3-bad9-754d48c6a8a4`，turn `01a10a5d-1de9-7361-b5e4-fd01fe214a1c`。

**用户原文**

种子现阈值为40，会将马赛克和阴影也计数。如增加到45，2号种子边框不完整，仍识别马赛克为种子，阴影不会误识别

**模型原文节选**

**这次应先提高面积过滤阈值，不要继续提高 FPGA threshold。** 从截图看，马赛克和阴影已经成为独立的小轮廓，可以用你现有的 `min_area` 去掉。

截图中的面积差距很明显：

| 目标 | Area（pixel²） |
|---|---:|
| 种子 ID 4～7 | 约 13,860～15,734 |
| 马赛克 ID 8 | 437.5 |
| 阴影 ID 9 | 142.5 |

之前水印与种子相连时，面积过滤不能单独去除水印；**现在这两个干扰是独立目标，面积过滤就适用了。**

**1．FPGA threshold 回到 40**

保留较完整的种子边缘。若当前 FPGA 输出是在 `45` 下生成的，先改回：

```python

threshold = 40

```

重新运行 FPGA 单元格，并确认 Golden PASS。

**2．只将分析调用中的 `min_area` 改成 1000**

```python

phenotype_result, annotated_rgb, clean_edges = analyze_phenotype(

    original_rgb,

    fpga_result,

    binary_output=True,

    min_area=1000.0,       # 原来是 100

    close_ksize=3,

    close_iterations=1,

    border_margin=1,

)

```

这是你前面独立 Notebook 的函数接口。**如果使用最新版 Dashboard，直接把界面里的 `Min area` 改为 `1000`，Threshold 改为 `40`，再点击 Run Analysis**，不用改函数。

`1000 pixel²` 是针对当前 **640×480 种子图**的起始值，不要直接作为所有图片的通用参数。先确认 ID 1～3 的面积也都大于 1000。

**3．检查结果，而不是只看 Count**

预期 ID 8、9 被过滤，七粒种子保留，但需要实际运行确认：

- Count 是否为 7。

- 七粒种子是否各有一个编号。

- 2 号种子的绿色外轮廓是否沿着真实边缘，没有明显向内部凹入。

- 面积和周长是否来自完整外轮廓。

**面积过滤只负责去掉独立小目标，不会修复 2 号种子的轮廓。** 如果回到 40 后它仍有局部缺口，再单独处理；暂时保持闭运算参数不变。

我没有修改你的任何文件。

（以上为原文节选，后续内容省略。）

## 案例 03 区分计数正确与轮廓精度

来源 thread `01a109d1-5605-73e3-bad9-754d48c6a8a4`，turn `01a10a60-4968-7231-aa27-7a55dee23b8b`。

**用户原文**

8和9已被过滤，但是阈值为40时，2号轮廓仍不完整，4号轮廓有小突起

**模型原文节选**

**现在七粒种子都保留了，计数这一步已改善；剩下是局部轮廓误差。** 截图中，2 号顶部更像是轮廓向内凹入，4 号顶部有小凸起，暂时不必继续改 FPGA 阈值。

建议保持 **`threshold=40、min_area=1000`**，先试验：**将每个有效外轮廓填成区域，再用小范围开、闭运算修整区域边界。** 不要直接对细边缘做开运算，否则可能删除目标边缘。

下面只做对比预览，不覆盖当前统计结果，也不修改你的文件。新建单元格运行：

（以上为原文节选，后续内容省略。）

## 案例 04 DMA 无输出打印的诊断

来源 thread `01a100b7-6bed-7091-9ba8-3dbd9fde611f`，turn `01a10133-a90c-7073-b891-c968553e2279`。

**用户原文**

这边都没打印任何东西

**模型原文节选**

你的代码里，**输入/输出缓冲区都是65536字节，先接收再发送的顺序也正确**，这两个疑点可以排除。

“后面没有任何打印”很可能是因为程序仍停在前面的 `wait()`：完成提示写在两个 `wait()` **之后**，而无限等待不会自动触发 `except Exception`。后面的比较 cell 也无法执行。

但从这份代码本身，还不能确定卡在发送还是接收。先按下面步骤定位。

**1．中断正在运行的 cell，然后读取现场状态，不要先重新加载 Overlay。**

新建 cell 执行：

```python

print("开始读取 DMA 状态", flush=True)

print("IMAGE_FILES =", IMAGE_FILES, flush=True)

print("OL_READY =", OL_READY, flush=True)

for label, offset in [

    ("MM2S_DMACR",  0x00),

    ("MM2S_DMASR",  0x04),

    ("MM2S_LENGTH", 0x28),

    ("S2MM_DMACR",  0x30),

    ("S2MM_DMASR",  0x34),

    ("S2MM_LENGTH", 0x58),

]:

    print(f"{label} = 0x{dma.mmio.read(offset):08X}", flush=True)

```

如果连“开始读取 DMA 状态”都没有出现，说明 Notebook 内核还没有开始执行这个 cell，不能据此判断 FPGA 状态。

另外，你最初贴出的 `IMAGE_FILES = []` 如果没有修改，循环会执行零次，搬移 cell 就不会打印任何东西；此时单独运行比较 cell 应打印“暂无结果”。

**2．你的 Overlay 加载代码存在一个需要核实的版本问题。**

```python

if BIT_PATH.exists() and not HWH_PATH.exists():

    ...

```

这个逻辑**只在同名 hwh 不存在时复制**。如果你后来更新了 `.bit`，但旧的 `sobel_axis_wrapper.hwh` 还在，它就会继续使用旧 hwh。

因此，“Overlay 加载成功”只能说明加载过程完成，**不能证明 bit/hwh 来自同一次构建**。请确认板上这两个文件是一套：

```text

sobel_axis_wrapper.bit

sobel_axis_wrapper.hwh

```

不要仅凭重命名或复制就认定配套。

**3．下次在 DMA 已恢复正常后重测，给每一步加打印。**

替换原来的 `try` 内传输部分：

```python

print(f"[{name}] 输入={inp.nbytes} B，输出={out.nbytes} B",

      flush=True)

print("① 启动接收", flush=True)

dma.recvchannel.transfer(out)

print("② 启动发送", flush=True)

dma.sendchannel.transfer(inp)

print("③ 等待发送完成", flush=True)

dma.sendchannel.wait()

print("④ 发送已完成，等待接收完成", flush=True)

dma.recvchannel.wait()

print("⑤ 接收已完成", flush=True)

```

- 停在③：尚未证实 MM2S 完成。

- 停在④：确实是 S2MM 未完成，再结合状态寄存器区分 DMA 错误与等待数据/TLAST。

- 出现⑤：DMA 已完成，应转查后续 Python 代码。

还有一处建议后续修正：展示 cell 使用 `out = {...}` 覆盖了 DMA 输出缓冲区变量。它不会导致第一次接收卡住，但会妨碍挂起后的检查，建议改成 `result_record = {...}`。

**现在最关键的是先取得现场的 `MM2S_DMASR`、`S2MM_DMASR`，以及最后出现的步骤编号。目前不应直接修改 HLS 的 TLAST 逻辑。**

（以上为原文节选，后续内容省略。）

## 案例 05 64 字节尾部缺失与根因收敛

来源 thread `01a100b7-6bed-7091-9ba8-3dbd9fde611f`，turn `01a10151-eacf-74d1-ae4b-2a6cea72a427`。

**用户原文**

B 侧预填标记法诊断结果:输出数据停在第 65472 拍,最后 64 拍(最后一行第 192~255 列)从未发出,TLAST 也从未到达(S2MM 状态 0x0 无错误);输入 65536 拍全部被核接收;倒数第二行数据完整正确,说明不是乱序/错位。结论:帧尾流水未排空,最后 64 拍数据 + TLAST 一起丢了。64 这个数请对照内部流水级数/FIFO 深度排查:C/RTL 联合仿真里看输出流 valid 何时拉低、最后写出的像素坐标,以及输出循环是否跑满 65536 次、帧尾 drain 逻辑是否少 64 拍。

**模型原文节选**

**这进一步支持“帧尾没有完成”，但还不能确定核恰好少发64拍。** 预填标记法确认的是：**DDR最后64字节没有被覆盖**。核输出握手次数仍需波形或计数器确认。

我也需要修正之前的判断：`rewind` 导致尾部停顿目前只是候选原因，尚未被验证。

### 64字节首先对应DMA突发大小

你截图中的配置为：

```text

S2MM Memory Map Data Width = 32 bit = 4字节

S2MM Max Burst Size        = 16

最大突发数据量             = 4 × 16 = 64字节

```

AMD 将该参数定义为 **S2MM内存侧突发映射粒度**。[官方说明](https://docs.amd.com/r/en-US/pg021_axi_dma/Field-Descriptions)

所以有一种可能：核只缺最后少量数据或TLAST，却使DMA最后一个突发没有完成写回，最终在DDR中表现为整整64字节未更新。**这只是与配置吻合的解释，不能据此认定DMA一定扣留了一个突发。**

已有HLS报告中的循环迭代延迟是6 cycles，**没有“内部流水深度为64”的证据**。

### 源码中的逻辑排尾没有少64次

当前源码：

```cpp

OUTPUT_DELAY = 257;

TOTAL_STEPS  = 65536 + 257;

for (int n = 0; n < TOTAL_STEPS; ++n) {

    ...

    if (n >= OUTPUT_DELAY) {

        write_pixel(dst, edge, n - OUTPUT_DELAY == 65535);

    }

}

```

按C语义计算：

| 项目 | 范围或数量 |
|---|---|
| 输入读取 | `n=0…65535`，共65536次 |
| 输出写入 | `n=257…65792`，共65536次 |
| 只输出、不读输入 | `n=65536…65792`，共257次 |
| 携带TLAST的输出 | `n=65792`，输出索引65535 |

因此，**不要把 `TOTAL_STEPS` 再加64**。这样可能增加输出数据或改变帧边界，并没有解决潜在的硬件停顿。

### 下一步直接数核输出握手

在核 `dst` 与 DMA `S_AXIS_S2MM` 的连接处增加调试计数器，并由ILA观察。计数器至少18 bit，每次测试前复位：

```verilog

// 仅为调试逻辑示例，不加入HLS算法。

// 在该AXIS接口的时钟域内运行。

always @(posedge aclk) begin

    if (!aresetn) begin

        out_count       <= 18'd0;

        last_seen       <= 1'b0;

        last_beat_count <= 18'd0;

    end else if (dst_tvalid && dst_tready) begin

        out_count <= out_count + 18'd1;

        if (dst_tlast) begin

            last_seen       <= 1'b1;

            last_beat_count <= out_count + 18'd1;

        end

    end

end

```

同时抓取：

```text

out_count、last_seen、last_beat_count

dst_TVALID、dst_TREADY、dst_TLAST、dst_TKEEP

```

结果可直接定位：

| 观察结果 | 判断 |
|---|---|
| `out_count=65472`，持续 `TREADY=1、TVALID=0` | 核确实在该位置停止提供数据 |
| `65472 < out_count < 65536` | DDR缺64字节不等于核缺64拍 |
| `out_count=65536`，`last_seen=0` | 输出数量完整，但帧标记异常 |
| `out_count=65536`，`last_beat_count=65536` | 核数据和TLAST均已交付，转查DMA写通道 |

（以上为原文节选，后续内容省略。）

## 案例 06 根据实际进度修正任务计划

来源 thread `6aae0437-314c-83ee-9eac-ec36cd78aee0`，turn `418878c3-2954-4afd-86b4-8db4d7973782`。

**用户原文**

这个文档里的任务已完成

（原对话含附件，当前摘录不包含附件正文。）

**模型原文节选**

明白。那你们现在已经完成的不是早期那个“单纯 V0→V3”的 Day 1，而是更进一步的**参数化最终候选版本**：`width/height/mode/threshold`、Sobel/Prewitt、RGB/Gray、三档尺寸、Golden、Benchmark 和测试素材这些都已经纳入并完成。

这意味着接下来**不要再按我上一条生成的 Day 2 文档做**，否则会大量重复。你们上传的任务书自己也明确写了：从下一天开始，主线应该转入**性能证据、Dashboard、应用、Skill、开源与答辩**，不再反复折腾基础功能。

因此下一天我建议直接升级为：

> **新 Day 2：农业应用闭环 + Dashboard 完成 + 性能证据链**

其中最重要的变化，就是把我们刚讨论的“为什么不直接看照片”彻底解决掉：把 Plant Demo 从单纯显示边缘升级为 **种子/叶片 → FPGA处理 → 轮廓提取 → Count / Area / Length / Width / Perimeter → 原图标注 → Dashboard**。

同时另一人并行把 V0/V3、PS/PL、多尺寸、Sobel/Prewitt 的 Benchmark 数据整理成正式图表。这样一天结束，作品应该第一次达到**“评委看一眼就知道它有什么用，而且能看到FPGA为什么有用”**的状态。

下一份任务文档我建议仍然做成**两人分工版**，但不会重复该项实验已经完成的任何东西。

（以上为原文节选，后续内容省略。）

