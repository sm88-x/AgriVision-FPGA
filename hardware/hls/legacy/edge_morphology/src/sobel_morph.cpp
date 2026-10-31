// =============================================================

// Sobel + Morphology AXI4-Stream V4(edge_validation 交付版:sobel_v4)

//

// 输入:width×height,uint8,行优先

// 输出:width×height,uint8,最外一圈为0

// 算法:V3 全部行为(mode/threshold)+ 新增 morph_mode:

//   morph_mode = 0:Bypass —— 与 V3 逐像素一致(回归保障)

//   morph_mode = 1:Erode  —— 3×3 最小值

//   morph_mode = 2:Dilate —— 3×3 最大值

//   其余值按 Bypass 处理(PS 侧校验)。

//

// 形态学语义(与 Golden 约定,见 register_map_edge_validation.md):

//   对阈值化输出 tval 流做 3×3 窗口 min/max;

//   窗口对帧外像素按 0 填充(zero-padding);

//   输出最外一圈强制 0。

//   threshold>0 二值化模式下,min/max 等价 9 输入 AND/OR;

//   threshold=0 饱和模式下为灰度 min/max,语义同样成立。

//

// 【保留】V3 全部协议:帧 = width*height 拍、TLAST 仅帧尾、

//   阻塞读写、三行缓存循环复用、单循环 II=1 目标、

//   ap_ctrl_hs 每帧重拉 ap_start。

// 【修改9】形态学第二窗口级:

//   tval 流(第一级输出,延迟 width+1 拍)串接第二级

//   lbuf2[3][MAX_W] + win2[3][3],与第一级同构,同一套 pragma 写法;

//   9 输入 min/max 平衡树(组合逻辑深度 4);

//   总输出延迟 = 2×(width+1),total_steps = pixels + 2×(width+1);

//   输出行列独立计数(避免除法),最外圈强制 0。

//

// 注意:

// II=1 是综合目标,真实 Achieved II 以 HLS 综合报告为准。

// TLAST 采用整帧结束协议,不是 AXI4-Stream Video 行尾协议。

// =============================================================

#include <stdint.h>

#include <ap_axi_sdata.h>

#include <hls_stream.h>

#ifndef __SYNTHESIS__

#include <cassert>

#endif

typedef ap_axiu<8, 0, 0, 0> axis_pixel_t;

// 尺寸上限(寄存器合法范围:3 <= width <= MAX_W,3 <= height <= MAX_H;

// 范围校验由 PS 侧负责,核内仅在 C 仿真断言)

static const int MAX_W = 1920;

static const int MAX_H = 1080;

// -------------------------------------------------------------

// 输入:保留 V0 的协议检查。

// -------------------------------------------------------------

static uint8_t read_pixel(

    hls::stream<axis_pixel_t>& src,

    int index,

    int pixels

) {

#pragma HLS INLINE

    axis_pixel_t packet = src.read();

#ifndef __SYNTHESIS__

    assert(packet.keep == 1 &&

           "Each input beat must contain one valid byte");

    assert((packet.last != 0) == (index == pixels - 1) &&

           "Input TLAST must occur only at the end of the frame");

#endif

    return (uint8_t)packet.data;

}

// -------------------------------------------------------------

// 输出:每拍一个有效字节,TLAST 仅在整帧末尾置1。

// -------------------------------------------------------------

static void write_pixel(

    hls::stream<axis_pixel_t>& dst,

    uint8_t value,

    bool last

) {

#pragma HLS INLINE

    axis_pixel_t packet;

    packet.data = value;

    packet.keep = 1;

    packet.strb = 1;

    packet.last = last ? 1 : 0;

    dst.write(packet);

}

// -------------------------------------------------------------

// 窗口计算:mode 选 Sobel/Prewitt,threshold 决定输出方式。

// 只读取完全分区的 3×3 窗口。

// -------------------------------------------------------------

static uint8_t edge_from_window(

    const uint8_t window[3][3],

    int mode,

    int threshold

) {

#pragma HLS INLINE

    int16_t gx;

    int16_t gy;

    if (mode == 1) {

        // Prewitt:系数均为 ±1

        gx =

            -(int16_t)window[0][0] + (int16_t)window[0][2]

            - (int16_t)window[1][0] + (int16_t)window[1][2]

            - (int16_t)window[2][0] + (int16_t)window[2][2];

        gy =

            -(int16_t)window[0][0] - (int16_t)window[0][1] - (int16_t)window[0][2]

            + (int16_t)window[2][0] + (int16_t)window[2][1] + (int16_t)window[2][2];

    } else {

        // Sobel(默认,含 mode 其他值)

        gx =

            -(int16_t)window[0][0]

            + (int16_t)window[0][2]

            - 2 * (int16_t)window[1][0]

            + 2 * (int16_t)window[1][2]

            - (int16_t)window[2][0]

            + (int16_t)window[2][2];

        gy =

            -(int16_t)window[0][0]

            - 2 * (int16_t)window[0][1]

            - (int16_t)window[0][2]

            + (int16_t)window[2][0]

            + 2 * (int16_t)window[2][1]

            + (int16_t)window[2][2];

    }

    uint16_t abs_gx =

        (gx < 0) ? (uint16_t)(-gx) : (uint16_t)gx;

    uint16_t abs_gy =

        (gy < 0) ? (uint16_t)(-gy) : (uint16_t)gy;

    uint16_t magnitude = abs_gx + abs_gy;

    if (threshold > 0) {

        // 二值化:梯度 >= threshold -> 255,否则 0。

        return (magnitude >= (uint16_t)threshold)

            ? (uint8_t)255

            : (uint8_t)0;

    }

    // 饱和输出:超 255 截断。

    return (magnitude > 255)

        ? (uint8_t)255

        : (uint8_t)magnitude;

}

// -------------------------------------------------------------

// 形态学窗口计算:3×3 min(Erode)/ max(Dilate)/ 透传中心(Bypass)。

// min/max 用平衡树实现(深度4),避免 8 级比较链拖慢关键路径。

// -------------------------------------------------------------

static uint8_t morph_from_window(

    const uint8_t w[3][3],

    int morph_mode

) {

#pragma HLS INLINE

    if (morph_mode == 1) {

        // Erode:9 像素最小值(二值图等价 9 输入 AND)

        uint8_t u0 = (w[0][0] < w[0][1]) ? w[0][0] : w[0][1];

        uint8_t u1 = (w[0][2] < w[1][0]) ? w[0][2] : w[1][0];

        uint8_t u2 = (w[1][1] < w[1][2]) ? w[1][1] : w[1][2];

        uint8_t u3 = (w[2][0] < w[2][1]) ? w[2][0] : w[2][1];

        uint8_t v0 = (u0 < u1) ? u0 : u1;

        uint8_t v1 = (u2 < u3) ? u2 : u3;

        uint8_t v2 = (v1 < w[2][2]) ? v1 : w[2][2];

        return (v0 < v2) ? v0 : v2;

    }

    if (morph_mode == 2) {

        // Dilate:9 像素最大值(二值图等价 9 输入 OR)

        uint8_t u0 = (w[0][0] > w[0][1]) ? w[0][0] : w[0][1];

        uint8_t u1 = (w[0][2] > w[1][0]) ? w[0][2] : w[1][0];

        uint8_t u2 = (w[1][1] > w[1][2]) ? w[1][1] : w[1][2];

        uint8_t u3 = (w[2][0] > w[2][1]) ? w[2][0] : w[2][1];

        uint8_t v0 = (u0 > u1) ? u0 : u1;

        uint8_t v1 = (u2 > u3) ? u2 : u3;

        uint8_t v2 = (v1 > w[2][2]) ? v1 : w[2][2];

        return (v0 > v2) ? v0 : v2;

    }

    // Bypass:透传窗口中心 = 第一级阈值化输出 tval。

    return w[1][1];

}

// -------------------------------------------------------------

// 每帧一次 ap_start,处理 width×height 一帧。

// -------------------------------------------------------------

void sobel_v4(

    hls::stream<axis_pixel_t>& src,

    hls::stream<axis_pixel_t>& dst,

    int width,

    int height,

    int mode,

    int threshold,

    int morph_mode

) {

#pragma HLS INTERFACE mode=axis port=src

#pragma HLS INTERFACE mode=axis port=dst

#pragma HLS INTERFACE mode=s_axilite port=return bundle=control

#pragma HLS INTERFACE mode=s_axilite port=width bundle=control

#pragma HLS INTERFACE mode=s_axilite port=height bundle=control

#pragma HLS INTERFACE mode=s_axilite port=mode bundle=control

#pragma HLS INTERFACE mode=s_axilite port=threshold bundle=control

#pragma HLS INTERFACE mode=s_axilite port=morph_mode bundle=control

    // 帧参数(运行时)。

    const int pixels = width * height;

    const int output_delay = width + 1;        // 第一级窗口中心滞后

    const int total_delay = 2 * output_delay;  // 两级串联总滞后

    const int total_steps = pixels + total_delay;

#ifndef __SYNTHESIS__

    assert(width >= 3 && height >= 3 &&

           "width/height must be >= 3");

    assert(width <= MAX_W && height <= MAX_H &&

           "width/height exceed MAX_W/MAX_H");

    assert(morph_mode >= 0 && morph_mode <= 2 &&

           "morph_mode must be 0/1/2");

#endif

    // ================= 第一级:与 V3 完全相同 =================

    // 三个物理bank循环保存三行。

    uint8_t lbuf[3][MAX_W];

#pragma HLS ARRAY_PARTITION variable=lbuf complete dim=1

#pragma HLS BIND_STORAGE variable=lbuf type=ram_s2p impl=bram

    // 窗口完全分区为寄存器,支持并行访问。

    uint8_t window[3][3];

#pragma HLS ARRAY_PARTITION variable=window complete dim=0

    // 每帧初始化9个窗口寄存器。

    INIT_WINDOW_ROW:

    for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

        INIT_WINDOW_COL:

        for (int x = 0; x < 3; ++x) {

#pragma HLS UNROLL

            window[y][x] = 0;

        }

    }

    int bank = 0;

    int r = 0;

    int c = 0;

    // ================= 第二级:形态学窗口(同构结构) =================

    uint8_t lbuf2[3][MAX_W];

#pragma HLS ARRAY_PARTITION variable=lbuf2 complete dim=1

#pragma HLS BIND_STORAGE variable=lbuf2 type=ram_s2p impl=bram

    uint8_t win2[3][3];

#pragma HLS ARRAY_PARTITION variable=win2 complete dim=0

    INIT_WIN2_ROW:

    for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

        INIT_WIN2_COL:

        for (int x = 0; x < 3; ++x) {

#pragma HLS UNROLL

            win2[y][x] = 0;

        }

    }

    int bank2 = 0;

    int r2 = 0;

    int c2 = 0;

    // 输出行列独立计数(最外圈判定;避免真除法保 II=1)。

    int out_r = 0;

    int out_c = 0;

    // 单循环:第一级每拍处理输入;第二级滞后 output_delay 拍

    // 消费第一级的 tval 流;输出再滞后 output_delay 拍。

    STREAM_PIXELS:

    for (int n = 0; n < total_steps; ++n) {

#pragma HLS PIPELINE II=1

        // 仅影响综合报告的延迟估计,不影响功能。

#pragma HLS LOOP_TRIPCOUNT min=66050 max=2077442

        // 第一级本次迭代算出的阈值化输出(= tval)。

        uint8_t edge = 0;

        // ---- 第一级(与 V3 逐拍一致)----

        if (n < pixels) {

            uint8_t current = read_pixel(src, n, pixels);

            uint8_t top = 0;

            uint8_t middle = 0;

            // 循环行缓存:bank=0 时 top 在 lbuf[1],middle 在 lbuf[2];

            // bank=1 时 top 在 2,middle 在 0;bank=2 时 top 在 0,middle 在 1。

            // 前两行用 r 条件屏蔽未写入的缓存,无需逐帧清空。

            switch (bank) {

            case 0:

                if (r >= 2) {

                    top = lbuf[1][c];

                }

                if (r >= 1) {

                    middle = lbuf[2][c];

                }

                lbuf[0][c] = current;

                break;

            case 1:

                if (r >= 2) {

                    top = lbuf[2][c];

                }

                if (r >= 1) {

                    middle = lbuf[0][c];

                }

                lbuf[1][c] = current;

                break;

            default:  // bank == 2

                if (r >= 2) {

                    top = lbuf[0][c];

                }

                if (r >= 1) {

                    middle = lbuf[1][c];

                }

                lbuf[2][c] = current;

                break;

            }

            // 窗口左移两列复用 6 像素,右列插入 3 个新像素;

            // 每行第 0 列清空左两列,避免跨行残留。

            WINDOW_SHIFT:

            for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

                if (c == 0) {

                    window[y][0] = 0;

                    window[y][1] = 0;

                } else {

                    window[y][0] = window[y][1];

                    window[y][1] = window[y][2];

                }

            }

            window[0][2] = top;

            window[1][2] = middle;

            window[2][2] = current;

            // 只有 r>=2 且 c>=2 时窗口完整,中心(r-1,c-1)为内部像素。

            if (r >= 2 && c >= 2) {

                edge = edge_from_window(window, mode, threshold);

            }

            if (c == width - 1) {

                bank = (bank == 2) ? 0 : bank + 1;

                c = 0;

                ++r;

            } else {

                ++c;

            }

        }

        // ---- 第二级:消费 tval 流(与第一级同构的行缓存+窗口)----

        // 仅当本拍存在 tval 时动作;flush 阶段(尾部 2×output_delay 拍

        // 中的最后 output_delay 拍)不动作,此时输出全为最外圈,强制 0。

        uint8_t morph_val = 0;

        if (n >= output_delay && n < output_delay + pixels) {

            uint8_t current2 = edge;

            uint8_t top2 = 0;

            uint8_t mid2 = 0;

            switch (bank2) {

            case 0:

                if (r2 >= 2) {

                    top2 = lbuf2[1][c2];

                }

                if (r2 >= 1) {

                    mid2 = lbuf2[2][c2];

                }

                lbuf2[0][c2] = current2;

                break;

            case 1:

                if (r2 >= 2) {

                    top2 = lbuf2[2][c2];

                }

                if (r2 >= 1) {

                    mid2 = lbuf2[0][c2];

                }

                lbuf2[1][c2] = current2;

                break;

            default:  // bank2 == 2

                if (r2 >= 2) {

                    top2 = lbuf2[0][c2];

                }

                if (r2 >= 1) {

                    mid2 = lbuf2[1][c2];

                }

                lbuf2[2][c2] = current2;

                break;

            }

            WINDOW2_SHIFT:

            for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

                if (c2 == 0) {

                    win2[y][0] = 0;

                    win2[y][1] = 0;

                } else {

                    win2[y][0] = win2[y][1];

                    win2[y][1] = win2[y][2];

                }

            }

            win2[0][2] = top2;

            win2[1][2] = mid2;

            win2[2][2] = current2;

            // 窗口完整时中心(r2-1,c2-1)即当前输出像素;

            // 行尾 wrap 迭代(c2==0)与首列(c2==1)对应最外圈,强制 0 兜底。

            if (r2 >= 2 && c2 >= 2) {

                morph_val = morph_from_window(win2, morph_mode);

            }

            if (c2 == width - 1) {

                bank2 = (bank2 == 2) ? 0 : bank2 + 1;

                c2 = 0;

                ++r2;

            } else {

                ++c2;

            }

        }

        // ---- 输出:两级延迟后每拍一个像素,最外圈强制 0 ----

        if (n >= total_delay) {

            const int output_index = n - total_delay;

            const bool is_ring = (out_r == 0) || (out_r == height - 1)

                              || (out_c == 0) || (out_c == width - 1);

            write_pixel(

                dst,

                is_ring ? (uint8_t)0 : morph_val,

                output_index == pixels - 1

            );

            if (out_c == width - 1) {

                out_c = 0;

                ++out_r;

            } else {

                ++out_c;

            }

        }

    }

}

