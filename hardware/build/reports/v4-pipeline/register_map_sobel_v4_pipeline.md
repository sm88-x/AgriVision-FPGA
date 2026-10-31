# sobel_v4_pipeline 寄存器表与算法口径

overlay:`hardware/drivers/configurable_pipeline.bit / .hwh`

> 内核实例 `sobel_v4_pipeline_0` @ **0x40000000**,DMA 实例 `axi_dma_0` @ **0x40400000**

> 本文档口径已在 C 仿真(逐像素 ALL PASS)与板端验证通过。

## 1. 寄存器表(s_axilite,每帧写入一次)

| 偏移 | 名称 | 位宽 | 语义 |
|---|---|---|---|
| 0x00 | AP_CTRL | 32 | ap_ctrl_hs:bit0=ap_start(每帧重新拉 1,无 auto-rewind);bit1=ap_done 只读 |
| 0x04/0x08/0x0C | GIER/IP_IER/IP_ISR | 32 | 中断寄存器(本工程中断悬空,不读不写) |
| 0x10 | width | 32 | 帧宽,合法 3..1920 |
| 0x18 | height | 32 | 帧高,合法 3..1080 |
| 0x20 | mode | 32 | 0=Sobel(**默认**,其他值同 Sobel),1=Prewitt |
| 0x28 | threshold | 32 | >0 二值化(梯度 ≥ threshold → 255,否则 0);==0 饱和输出(>255 截断) |
| 0x30 | morph_mode | 32 | 0=Bypass,1=Erode(3×3 min),2=Dilate(3×3 max),其他值按 Bypass |
| 0x38 | enable_gray | 32 | 1=RGB2Gray;0=透传输入低 8bit(灰度输入路径) |
| 0x40 | enable_gauss | 32 | 1=Gaussian 3×3;0=旁路 |
| 0x48 | out_sel | 32 | 0=最终 morph(默认);1=gray;2=gauss;3=edge/threshold;**非法值按 0** |

**PS 侧校验:** width∈[3,1920]、height∈[3,1080]、mode∈{0,1}、morph_mode∈{0,1,2}、enable_gray∈{0,1}、enable_gauss∈{0,1}、out_sel∈{0,1,2,3}。enable_gray=0 时送 RGB 图为非法组合,由 PS 拒绝。

## 2. 流接口协议

- **输入 AXI4-Stream**:32bit/拍 RGBX888,`bit[23:16]=R、bit[15:8]=G、bit[7:0]=B、bit[31:24] 忽略`,keep=0xF,每拍 1 像素,帧 = width×height 拍,**TLAST 仅帧尾一拍**(非 Video 行协议)。enable_gray=0 时低 8bit 直接作为灰度。

- **输出 AXI4-Stream**:8bit/拍,每帧固定输出 width×height 拍,TLAST 帧尾一拍。

- DMA:MM2S 通道 32bit 宽(输入),S2MM 通道 8bit 宽(输出);Memory Map 两侧均 32bit。

- 调用顺序:**写寄存器 → 挂 DMA 收发 → 写 AP_CTRL.ap_start=1 → 轮询双通道 IOC**。

- PS 打包示例:`np.uint32((R<<16)|(G<<8)|B)`,整帧一维数组送 MM2S。

## 3. 算法口径

- **RGB2Gray**:`Y = (77R + 150G + 29B + 128) >> 8`(77+150+29=256,天然饱和,不溢出)

- **Gaussian 3×3**:整数核 `[1 2 1; 2 4 2; 1 2 1]`,**零填充边界**,`out = (sum + 8) >> 4`(round half up;sum ≤ 4080 不溢出)

- **Edge**:Sobel `[-1 0 1;-2 0 2;-1 0 1]` / Prewitt `[-1 0 1;-1 0 1;-1 0 1]`(x 与转置 y),`mag = |gx| + |gy|`;threshold>0 二值化,==0 饱和

- **Morphology**:对阈值化输出 tval 流做 3×3 min/max,**零填充边界**;threshold>0 时 min/max 等价 9 输入 AND/OR,threshold=0 时为灰度 min/max,语义同样成立

## 4. 级联语义(Golden 公式,已验证逐像素一致)

设 D = width+1,全部流尺寸 h×w,窗口均为 **3×3、帧外零填充**,未写位置 = 0:

```

第0步  gray[r,c]                       # 由 RGB 公式或透传得到

第1步  G(r,c)  [Gaussian 级输出流]

        enable_gauss=1: G[r,c] = gauss3x3(gray, 中心=(r-1,c-1)),仅 r,c >= 2 计算

        enable_gauss=0: G[r,c] = gray[r-1,c-1] (c>=1);G[r,0] = gray[r-2,w-1]

第2步  E(r,c)  [Edge 级输出流]

        enable_gauss=1: E[r,c] = edge3x3(G,    中心=(r,c), 零填充),仅 r,c >= 2

        enable_gauss=0: E[r,c] = edge3x3(gray, 中心=(r-1,c-1), 零填充),仅 r,c >= 2

第3步  M(r,c)  [Morph 级输出流]

        M[r,c] = morph3x3(E, 中心=(r,c), 零填充),仅 r,c >= 2

输出   out_sel=0: out[r,c] = M[r+1,c+1]       # r,c ∈ [1,h-2]×[1,w-2],外圈强制 0

       out_sel=1: out[r,c] = gray[r,c]        # 全帧有效,无外圈清零

       out_sel=2: out[r,c] = G[r+1,c+1]       # 外圈强制 0

       out_sel=3: out[r,c] = E[r+1,c+1]       # 外圈强制 0

```

**等价读法**:除 gray 外每级输出流都是「前级流同坐标 3×3 零填充窗口」的结果,唯一例外是 Gaussian 级窗口中心偏移 (-1,-1)(旁路时表现为 gray 流滞后 (1,1) 的线性平移,含最右列 wrap 语义)。三级是**三个独立窗口**,不是 5×5。

## 5. 时序与延迟

- 输出延迟(out_sel):0→3D 拍,1→0 拍,2→D 拍,3→2D 拍(D=width+1)

- 单帧处理总拍数 = width×height + 3D;输出始终为 width×height 拍

- 目标 II=1(以综合报告为准);帧间由 ap_ctrl_hs 重拉 ap_start 启动

## 6. 回归锚点

`enable_gray=0 + enable_gauss=0 + mode=0 + threshold=0 + morph_mode=0 + out_sel=0`、灰度 8bit 输入时,整核输出与旧版 `sobel_v4` **逐像素一致**(含最外圈与右/底边界相邻像素)——旧脚本在灰度路径上行为不回退。

## 7. 板端验证结果(configurable_pipeline,2026-10-10)

- Vivado:BD 名 `integrated_pipeline_system`(避开 Windows 260 字符路径限制),device xc7z020clg400-1

- 时序:clk_fpga_0 @100MHz,**WNS=+1.275ns,TNS=0,无失败端点**;LUT 8.4% / FF 5.2%

- hwh 已核对:8 个数据寄存器齐全、sobel_v4_pipeline_0@0x40000000、axi_dma_0@0x40400000、MM2S=32bit、S2MM=8bit

