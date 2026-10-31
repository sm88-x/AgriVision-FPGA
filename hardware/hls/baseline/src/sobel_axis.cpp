// =============================================================

// Sobel AXI4-Stream 基线版

//

// 输入：256×256，uint8，行优先，每拍一个像素

// 输出：256×256，uint8，最外一圈为0

// 算法：G = |Gx| + |Gy|，饱和到255

//

// 【修改1】ap_ctrl_none：无需PS写ap_start。

// 【修改2】C仿真检查输入TKEEP、TLAST。

// 【保留】固定帧协议，每帧读取/输出65536个像素。

// 【保留】pipeline off，先建立正确性基线。

//

// 硬件输入约定：

//   TDATA宽度8位；每拍TKEEP=1。

//   仅第65536拍TLAST=1。

//   输入中断时等待，不提供超时/坏帧自动恢复。

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

// 【新增】统一输入读取与C仿真协议检查。

static uint8_t read_pixel(

    hls::stream<axis_pixel_t>& src,

    int index

) {

#pragma HLS INLINE

    // 阻塞读取：没有输入数据时等待。

    axis_pixel_t packet = src.read();

#ifndef __SYNTHESIS__

    // 只用于C仿真，不综合为硬件错误处理。

    assert(packet.keep == 1 &&

           "Each input beat must contain one valid byte");

    assert((packet.last != 0) == (index == PIXELS - 1) &&

           "Input TLAST must occur only at the end of the frame");

#endif

    return (uint8_t)packet.data;

}

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

    // 下游未就绪时等待，数据与TLAST一起参与握手。

    dst.write(packet);

}

void sobel_axis(

    hls::stream<axis_pixel_t>& src,

    hls::stream<axis_pixel_t>& dst

) {

#pragma HLS INTERFACE mode=axis port=src

#pragma HLS INTERFACE mode=axis port=dst

    // 【修改】替代原来的s_axilite port=return。

    // 生成的硬件无需ap_start，复位释放后自主执行。

    // 一帧结束后继续处理下一帧；缺数据时停在read()。

#pragma HLS INTERFACE mode=ap_ctrl_none port=return

    uint8_t lbuf[3][W];

#pragma HLS ARRAY_PARTITION variable=lbuf complete dim=1

    // 不使用static：每次帧处理从索引0开始。

    int input_index = 0;

    // 按行优先顺序读取输入第0、1行。

    PREFILL:

    for (int row = 0; row < 2; ++row) {

#pragma HLS PIPELINE off

        PREFILL_COL:

        for (int c = 0; c < W; ++c) {

#pragma HLS PIPELINE off

            lbuf[row][c] = read_pixel(src, input_index);

            ++input_index;

        }

    }

    // 输出第0行，上边界为0。

    TOP_BORDER:

    for (int c = 0; c < W; ++c) {

#pragma HLS PIPELINE off

        write_pixel(dst, 0, false);

    }

    // 输出内部第1～254行。

    ROWS:

    for (int r = 1; r < H - 1; ++r) {

#pragma HLS PIPELINE off

        // 读取输入第r+1行。

        // 三行缓存对应输入r-1、r、r+1行。

        LOAD:

        for (int c = 0; c < W; ++c) {

#pragma HLS PIPELINE off

            lbuf[2][c] = read_pixel(src, input_index);

            ++input_index;

        }

        // 左边界。

        write_pixel(dst, 0, false);

        COMPUTE:

        for (int c = 1; c < W - 1; ++c) {

#pragma HLS PIPELINE off

            // 使用有符号中间量，避免uint8减法溢出。

            int16_t gx =

                -(int16_t)lbuf[0][c - 1]

                + (int16_t)lbuf[0][c + 1]

                - 2 * (int16_t)lbuf[1][c - 1]

                + 2 * (int16_t)lbuf[1][c + 1]

                - (int16_t)lbuf[2][c - 1]

                + (int16_t)lbuf[2][c + 1];

            int16_t gy =

                -(int16_t)lbuf[0][c - 1]

                - 2 * (int16_t)lbuf[0][c]

                - (int16_t)lbuf[0][c + 1]

                + (int16_t)lbuf[2][c - 1]

                + 2 * (int16_t)lbuf[2][c]

                + (int16_t)lbuf[2][c + 1];

            uint16_t abs_gx =

                (gx < 0) ? (uint16_t)(-gx) : (uint16_t)gx;

            uint16_t abs_gy =

                (gy < 0) ? (uint16_t)(-gy) : (uint16_t)gy;

            uint16_t magnitude = abs_gx + abs_gy;

            uint8_t edge =

                (magnitude > 255) ? (uint8_t)255

                                  : (uint8_t)magnitude;

            write_pixel(dst, edge, false);

        }

        // 右边界。

        write_pixel(dst, 0, false);

        SHIFT:

        for (int c = 0; c < W; ++c) {

#pragma HLS PIPELINE off

            lbuf[0][c] = lbuf[1][c];

            lbuf[1][c] = lbuf[2][c];

        }

    }

    // 输出第255行，下边界为0。

    // 最后一个输出像素携带TLAST。

    BOTTOM_BORDER:

    for (int c = 0; c < W; ++c) {

#pragma HLS PIPELINE off

        write_pixel(dst, 0, c == W - 1);

    }

}