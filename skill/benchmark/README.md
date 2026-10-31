# benchmark —— Generic 性能统计 Skill

对任意可调用处理函数做统一性能统计:Mean / Median / Min / Max / FPS + CSV 记录。

不绑定任何算法、图片或项目路径。

## 适用范围

- 任意 Python 可调用对象:PS 算法函数、PL E2E 封装函数、混合流程;

- 单次耗时毫秒级及以上(`time.perf_counter` 精度足够);

- PC 与板端均可运行(不依赖 PYNQ)。

不适用:

- 纳秒级微操作(改用 `timeit`);

- 需要固定 buffer 地址、禁止重复执行的场景(每次迭代都会重新调用 fn)。

## 计时范围由调用者定义

benchmark 只负责"重复调用 fn 并计时"。**测的是什么,完全取决于 fn 里写什么**:

- 测 **PS 算法**:fn = 纯算法函数(读图 → 处理 → 返回);

- 测 **PL E2E**:fn = 寄存器写 + DMA send/recv + wait 全流程;

- 不要把 PS 算法时间和 PL E2E 混在同一个 fn 里比较 —— 两者口径不同,混测得出的对比没有意义。

## 调用示例

```python

import sys

sys.path.insert(0, '<repo>/skill/benchmark')

from benchmark import benchmark

# 例 1: 测 PS 算法函数(计时范围 = 算法本身)

def ps_sobel():

    edge = my_ps_sobel(gray_img)        # 调用者自己的 PS 参考实现

benchmark(ps_sobel, name="ps_sobel_640x480", repeat=20,

          csv_path="validation/benchmark_results.csv")

# 例 2: 测 PL E2E(计时范围 = 寄存器 + DMA + wait 全流程,与 board_benchmark.py 同口径)

def pl_e2e():

    dma_ctrl.write(0x04, 0x1000)                 # 清 IOC 完成标志(写 1 清除)

    dma_ctrl.write(0x34, 0x1000)

    ctrl.write(0x10, W); ctrl.write(0x18, H)     # width / height

    ctrl.write(0x20, 0);  ctrl.write(0x28, 0)    # mode / threshold

    dma.recvchannel.transfer(out_buf)            # 先挂接收,再挂发送

    dma.sendchannel.transfer(inp_buf)

    ctrl.write(0x00, 1)                          # ap_start(先挂 DMA 再拉,本板实测顺序)

    while not (dma_ctrl.read(0x04) & 0x1000 and dma_ctrl.read(0x34) & 0x1000):

        pass                                     # 轮询 IOC 位,同 board_benchmark.py

benchmark(pl_e2e, name="pl_e2e_640x480", warmup=2, repeat=20,

          csv_path="validation/benchmark_results.csv")

# 例 3: 只测不带参的函数,取回完整样本

r = benchmark(ps_sobel, name="ps_sobel_640x480")

print(r["samples"])      # 全部 20 个原始样本(ms)

```

## 正常输出

```

[benchmark] pl_e2e_640x480: mean=1.231 ms, median=1.209 ms, min=1.181 ms, max=1.352 ms, fps=827.130 (N=20, warmup=2)

```

CSV 列(追加模式,首次自动写表头):

| name | mean_ms | median_ms | min_ms | max_ms | fps | repeat | warmup | timestamp |
|---|---|---|---|---|---|---|---|---|

本 Skill 的 CSV 是**汇总统计**(每行一组测量:mean/median/min/max/fps)。

两者口径一致:FPS = 1000 / median(ms)、warmup 2 + repeat 20,可直接交叉核对。

## 失败条件

| 失败 | 输出特征 | 含义 |
|---|---|---|
| fn 不可调用 | `ValueError: benchmark: fn 必须是可调用对象` | 参数错误 |
| fn 运行中抛异常 | 异常冒泡、统计中断 | 被测函数本身出错(计时失败即停止,不做部分统计) |
| CSV 不可写 | `OSError` 冒泡 | 路径/权限问题 |

## 注意事项与排查顺序

1. 首轮样本异常高 → 增大 `warmup`(缓存冷启动、DMA 首次分配);

2. 板端偶发尖峰拉高 max → 以 **median** 为报告口径(FPS 也按 median 算);

3. fn 里混入了不该测的代码(打印、读图)→ 重构 fn,把计时范围收紧到要测的那一段;

4. CSV 数字对不上 → 确认两处用同一口径(median_ms 与 fps 的关系 = fps = 1000/median_ms);

5. 板端跑和 PC 跑差异大 → PL E2E 只能在板端测,PC 上只有 PS 算法有意义。

## FPS 口径

`FPS = 1000 / median(ms)`,与本项目板测 CSV(r1~r6)口径一致。

若 fn 是一次完整处理(一帧),FPS 即帧率。

