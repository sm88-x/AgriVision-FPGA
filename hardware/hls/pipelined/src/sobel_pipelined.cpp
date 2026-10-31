// =============================================================

// Sobel AXI4-Stream V3

//

// 输入：256×256，uint8，行优先

// 输出：256×256，uint8，最外一圈为0

// 算法：G = |Gx| + |Gy|，饱和到255

//

// 【保留】AXI4-Stream接口和ap_ctrl_none。

// 【保留】固定帧协议，每帧输入/输出65536个像素。

// 【保留】C仿真检查输入TKEEP和TLAST。

// 【保留】阻塞读写，支持AXI Stream背压。

//

// 【修改1】三行缓存循环复用，取消整行SHIFT。

// 【修改2】增加真正的3×3滑动窗口。

// 【修改3】输入、窗口更新、Sobel、输出合并到一个循环。

// 【修改4】主循环申请PIPELINE II=1。

// 【修改5】显式处理warm-up、坐标对齐和帧尾flush。

//

// 注意：

// II=1是综合目标，真实Achieved II以HLS综合报告为准。

// 输入断流时等待；不提供超时或坏帧自动恢复。

// TLAST采用整帧结束协议，不是AXI4-Stream Video行尾协议。

// =============================================================

#include <stdint.h>

#include <ap_axi_sdata.h>

#include <hls_stream.h>

#ifndef __SYNTHESIS__

#include <cassert>

#endif

typedef ap_axiu<8, 0, 0, 0> axis_pixel_t;

static const int W = 256;

static const int H = 256;

static const int PIXELS = W * H;

// 【修改5】输出像素比当前输入位置延迟一行加一列。

// 输入位置(r,c)对应窗口中心(r-1,c-1)。

static const int OUTPUT_DELAY = W + 1;

// 最后OUTPUT_DELAY次迭代只输出边界0，不再读取输入。

static const int TOTAL_STEPS = PIXELS + OUTPUT_DELAY;

// -------------------------------------------------------------

// 输入：保留V0的协议检查。

// -------------------------------------------------------------

static uint8_t read_pixel(

    hls::stream<axis_pixel_t>& src,

    int index

) {

#pragma HLS INLINE

    axis_pixel_t packet = src.read();

#ifndef __SYNTHESIS__

    assert(packet.keep == 1 &&

           "Each input beat must contain one valid byte");

    assert((packet.last != 0) == (index == PIXELS - 1) &&

           "Input TLAST must occur only at the end of the frame");

#endif

    return (uint8_t)packet.data;

}

// -------------------------------------------------------------

// 输出：每拍一个有效字节，TLAST仅在整帧末尾置1。

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

// 【修改2】Sobel只读取完全分区的3×3窗口。

// 不再直接从行缓存同时读取多个列位置。

// -------------------------------------------------------------

static uint8_t sobel_from_window(

    const uint8_t window[3][3]

) {

#pragma HLS INLINE

    int16_t gx =

        -(int16_t)window[0][0]

        + (int16_t)window[0][2]

        - 2 * (int16_t)window[1][0]

        + 2 * (int16_t)window[1][2]

        - (int16_t)window[2][0]

        + (int16_t)window[2][2];

    int16_t gy =

        -(int16_t)window[0][0]

        - 2 * (int16_t)window[0][1]

        - (int16_t)window[0][2]

        + (int16_t)window[2][0]

        + 2 * (int16_t)window[2][1]

        + (int16_t)window[2][2];

    uint16_t abs_gx =

        (gx < 0) ? (uint16_t)(-gx) : (uint16_t)gx;

    uint16_t abs_gy =

        (gy < 0) ? (uint16_t)(-gy) : (uint16_t)gy;

    uint16_t magnitude = abs_gx + abs_gy;

    return (magnitude > 255)

        ? (uint8_t)255

        : (uint8_t)magnitude;

}

// -------------------------------------------------------------

// 

// 

// -------------------------------------------------------------

void sobel_v3(

    hls::stream<axis_pixel_t>& src,

    hls::stream<axis_pixel_t>& dst

) {

#pragma HLS INTERFACE mode=axis port=src

#pragma HLS INTERFACE mode=axis port=dst

#pragma HLS INTERFACE mode=ap_ctrl_none port=return

    // 【修改1】三个物理bank循环保存三行。

    // bank表示当前输入行写入哪个bank。

    // 每进入新的一行，只切换bank，不复制整行数据。

    uint8_t lbuf[3][W];

#pragma HLS ARRAY_PARTITION variable=lbuf complete dim=1

#pragma HLS BIND_STORAGE variable=lbuf type=ram_s2p impl=bram

    // 【修改2】窗口完全分区为寄存器，支持并行访问。

    uint8_t window[3][3];

#pragma HLS ARRAY_PARTITION variable=window complete dim=0

    // 【方案一】把单帧处理包进 while(1) 真死循环，

    // 让 ap_ctrl_none 的自动回绕作用在 while(1) 上，

    // 内层 STREAM_PIXELS 变成普通流水循环，退出时自然排空流水，

    // 避免 delay=0 的 auto-rewind 把帧尾输出踩掉。

    FRAME:

    while (1) {

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

        // 当前输入行的写入bank，按0→1→2→0循环（每帧重新从0开始）。

        int bank = 0;

        // 【修改3】用单个循环替代PREFILL、LOAD、

        // COMPUTE、SHIFT以及单独的边界输出循环。

        STREAM_PIXELS:

        for (int n = 0; n < TOTAL_STEPS; ++n) {

#pragma HLS PIPELINE II=1

            // 本次迭代可输出的内部像素。

            // 边界和flush阶段保持0。

            uint8_t edge = 0;

            if (n < PIXELS) {

                // W固定为256，除法/取模可化为位选择。

                const int r = n / W;

                const int c = n % W;

                uint8_t current = read_pixel(src, n);

                uint8_t top = 0;

                uint8_t middle = 0;

                // 【修改1】循环行缓存：

                // 当前行写一个bank，前两行各读一个bank。

                //

                // bank=0：top在1，middle在2，current写0

                // bank=1：top在2，middle在0，current写1

                // bank=2：top在0，middle在1，current写2

                //

                // 前两行用r条件屏蔽尚未写入的缓存，

                // 因此不需要逐帧清空整个lbuf。

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

                // 【修改2】窗口向左移动，右侧插入新的一列。

                // 已有6个像素复用，只引入3个新像素。

                //

                // 每行第0列清除左侧两列，避免跨行残留。

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

                // 窗口更新后：

                // window[0]对应输入第r-2行；

                // window[1]对应输入第r-1行；

                // window[2]对应输入第r行；

                // 三列对应c-2、c-1、c。

                //

                // 【修改5】只有r>=2且c>=2时窗口完整。

                // 此时中心(r-1,c-1)一定是内部像素。

                if (r >= 2 && c >= 2) {

                    edge = sobel_from_window(window);

                }

                // 【修改1】行结束只更新bank编号。

                if (c == W - 1) {

                    bank = (bank == 2) ? 0 : bank + 1;

                }

            }

            // 【修改5】前257次迭代只读输入，不产生输出。

            // 后续每次迭代输出一个像素。

            //

            // 输出索引 = n - 257

            // 最后一次输出索引 = 65535

            if (n >= OUTPUT_DELAY) {

                const int output_index = n - OUTPUT_DELAY;

                write_pixel(

                    dst,

                    edge,

                    output_index == PIXELS - 1

                );

            }

        }

#ifndef __SYNTHESIS__

        // 仅 C 仿真：每次调用处理一帧后返回，便于测试台逐帧对拍；

        // 综合时此句被移除，while(1) 成为真正的死循环。

        break;

#endif

    }

}