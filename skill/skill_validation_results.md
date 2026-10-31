# Generic Skill(dma_test / benchmark)板端验证记录

## 1 dma_test 验证:T1~T5 全部符合预期

```

===== T1 常量图(期望输出全 0) =====

[dma_test] DMA: DMA @ 0x40400000

[dma_test] send 4096 x uint8 -> recv 4096 x uint8

[dma_test] compare: 0 mismatch(es), max diff = 0

[dma_test] 前 8 个输出值: [0, 0, 0, 0, 0, 0, 0, 0]

[dma_test] PASS

[T1] 64x64 常量图 Sobel 饱和 → 输出全 0 | 预期=PASS 实际=PASS → OK

===== T2a 渐变图对拍 edge_reference (sobel) =====

[T2a] 64x64 渐变图 sobel 对拍 edge_reference | 预期=PASS 实际=PASS → OK

===== T2b 渐变图对拍 edge_reference (prewitt) =====

[T2b] 64x64 渐变图 prewitt 对拍 edge_reference | 预期=PASS 实际=PASS → OK

===== T3 假 DMA 名 =====

[dma_test] FAIL: DMA未找到: 'axi_dma_404' 不在 overlay 中或不是 DMA 驱动。

[T3] 假 DMA 名 → FAIL 且提示「DMA未找到」 | 预期=FAIL+DMA未找到 实际=FAIL → OK

===== T4 reference 长度错配 =====

[dma_test] FAIL: 长度不匹配: reference 100 拍 != out_length 64 拍, 无法比较。

[T4] reference 100 拍 vs out_length 64 → FAIL 且提示「长度不匹配」 | 预期=FAIL+长度不匹配 实际=FAIL → OK

===== T5 名称字符串形式 =====

[dma_test] DMA: axi_dma_0 @ 0x40400000

[dma_test] compare: 0 mismatch(es), max diff = 0

[T5] 名称字符串 + overlay 解析 → 链路 PASS | 预期=PASS 实际=PASS → OK

全部用例符合预期

```

## 2 benchmark 验证:两组 PASS + 量级对照 OK

```

[benchmark] ps_sobel_256x256: mean=40.687 ms, median=39.921 ms,

            min=39.560 ms, max=47.010 ms, fps=25.050 (N=20, warmup=2)

正确性对拍: PASS 不同像素 = 0

[benchmark] pl_e2e_256x256_v3p: mean=1.282 ms, median=1.240 ms,

            min=1.177 ms, max=1.548 ms, fps=806.484 (N=20, warmup=2)

  PS median=39.921 ms 在 [20, 80]: OK

  PL median=1.240 ms 在 [0.5, 5.0]: OK

全部符合预期

```

## 3 与正式板测 r1~r6 量级对照

| 组 | 本次实测 median | r1~r6 基准(256x256 Sobel) | 偏差 |
|---|---|---|---|
| PS 软件(edge_reference) | 39.921 ms | 39.565 ms | +0.9% |
| PL E2E(phenotyping 参数化核) | 1.240 ms | 1.209 ms | +2.6% |

