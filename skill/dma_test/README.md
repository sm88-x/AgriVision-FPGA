# dma_test —— Generic DMA 链路检查 Skill

验证任意兼容 Overlay 的 DMA 基本发送/接收链路:buffer 分配 → send/recv → wait → 数据返回 → 比对 → PASS/FAIL。

不绑定任何算法、图片或项目路径。

## 适用范围

- 任意带 AXI DMA 的 PYNQ Overlay,且 DMA 以 `sendchannel`/`recvchannel` 驱动呈现;

- 内核从 MM2S 读流、向 S2MM 写流(硬件加速核、环回、已知变换均可);

- 数据宽度 8/16/32 bit(`dtype` 参数,默认 uint8);

- 逐帧 TLAST 的流(不要求行级 TLAST 协议)。

不适用:

- 无 DMA 的纯寄存器 IP(直接用 `register_map`/MMIO 读写即可);

- 输出拍数与输入拍数不同且无法预知的场景(此时把实际期望值传给 `out_length`,并用 `reference` 验证内容)。

## 环境要求

- PYNQ 2.x(需要 `from pynq import allocate`)或 3.x;numpy;

- 板端实测环境:PYNQ-Z2。

## 设计说明

- **完成判定** = 轮询 DMA 状态寄存器 IOC 位(MM2S_DMASR@0x04 / S2MM_DMASR@0x34,bit12),带超时、不依赖中断;取不到 DMA 基址时回退到 PYNQ `channel.wait()`。

- **顺序** = 先挂 recvchannel 再挂 sendchannel(接收侧提前就位);内核启动放在 `start` 回调里执行,保证「先挂 DMA 再 ap_start」(本板实测反序不稳定)。

- **清标志** = 每帧开始前写 1 清除上一帧 IOC,避免把上一帧的完成误判成本帧。

## 调用示例

把 `dma_test.py` 复制到板端(或 clone 仓库)后:

```python

from pynq import Overlay, MMIO

import numpy as np

import sys

sys.path.insert(0, '<repo>/skill/dma_test')

from dma_test import dma_test

ol = Overlay('parameterized_edge.bit')

dma = ol.axi_dma_0                     # DMA 驱动对象(实例名以 hwh 为准)

ctrl = MMIO(ol.ip_dict['sobel_v3_0']['phys_addr'], 0x1000)

# start 回调: 在 DMA 挂好之后、等待之前执行, 保证"先挂 DMA 再 ap_start"的稳定顺序

def start_kernel():

    ctrl.write(0x10, 64)    # width

    ctrl.write(0x18, 64)    # height

    ctrl.write(0x20, 0)     # mode = 0 (Sobel)

    ctrl.write(0x28, 0)     # threshold = 0 (饱和输出)

    ctrl.write(0x00, 1)     # AP_CTRL: ap_start(ap_ctrl_hs 每帧都要重新拉一次)

src = np.zeros(64 * 64, dtype=np.uint8)          # 64x64 常量图

ref = np.zeros(64 * 64, dtype=np.uint8)          # 常量图的 Sobel 输出应为全 0

r = dma_test(dma, src, out_length=64 * 64, reference=ref, start=start_kernel)

print(r['status'], r['mismatches'], r['max_diff'])

```

通用形态:

```python

# 方式一: 直接传 DMA 对象(无寄存器内核/环回 IP,不需要 start)

r = dma_test(dma, data=[1, 2, 3, 4], out_length=4)

# 方式二: 传名称字符串(经 overlay.ip_dict 解析)

r = dma_test('axi_dma_0', data=[1, 2, 3, 4], out_length=4, overlay=ol)

# 方式三: 无 reference, 只验证链路通(不验证内容)

r = dma_test(dma, np.arange(64, dtype=np.uint8), out_length=64)

print(r['checksum'], r['output'][:8])

```

## 正常输出

```

[dma_test] DMA: axi_dma_0 @ 0x40400000

[dma_test] send 4096 x uint8 -> recv 4096 x uint8

[dma_test] compare: 0 mismatch(es), max diff = 0

[dma_test] 前 8 个输出值: [0, 0, 0, 0, 0, 0, 0, 0]

[dma_test] PASS

```

## 失败条件

| 失败 | 输出特征 | 含义 |
|---|---|---|
| DMA未找到 | `FAIL: DMA未找到: ...` | 名称不在 ip_dict / 对象无 sendchannel / dma 为 None |
| 长度不匹配 | `FAIL: 长度不匹配: ...` | 输入拍数 > out_length;或 reference 长度 != out_length |
| wait 卡住(发送端) | `FAIL: wait 卡住: 发送端(MM2S)...` + 发送端排查顺序 | 内核未消费输入(ap_start 没拉/寄存器没写) |
| wait 卡住(接收端) | `FAIL: wait 卡住: 接收端(S2MM)...` + 接收端排查顺序 | 内核未产出足够数据 → TLAST/输出长度异常 |
| start 回调失败 | `FAIL: start 回调执行失败: ...` | 回调里写寄存器/MMIO 出错 |
| 内容不一致 | `FAIL` + mismatch 数与 max diff | 链路通但数据错(算法/口径问题) |

## 排查顺序

1. 读 FAIL 信息,确定失败类别;

2. DMA未找到 → `print(ol.ip_dict.keys())` 核对设备名;

3. 长度不匹配 → 核对 `out_length = width × height`(字节数);

4. 发送端卡住 → 检查 start 回调是否拉了 ap_start、顺序是否为「先挂 DMA 再 ap_start」、寄存器是否合法;

5. 接收端卡住 → 检查输入/输出拍数、TLAST 帧尾、ap_done;

6. 卡住恢复 → `ol.reset()` 重载 overlay 后再排查,避免残留状态干扰;

7. 内容不一致 → 先用小尺寸(如 8×8)对拍,排除长度问题后再查算法。

