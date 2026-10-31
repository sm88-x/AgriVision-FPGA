// =============================================================

// Sobel AXI4-Stream V3(edge_validation 参数化版)

//

// 输入:width×height,uint8,行优先

// 输出:width×height,uint8,最外一圈为0

// 算法:G = |Gx| + |Gy|,饱和到255(mode/threshold 接口已留,A3/A4 实现)

//

// 【保留】AXI4-Stream 接口。

// 【保留】帧协议:每帧 width*height 拍,TLAST 仅在帧尾。

// 【保留】C 仿真检查输入 TKEEP 和 TLAST。

// 【保留】阻塞读写,支持 AXI Stream 背压。

// 【保留】三行缓存循环复用 + 3×3 滑动窗口 + 单循环 + II=1。

// 【保留】帧尾 flush:多跑 width+1 次只出边界0,最后一拍才带 TLAST。

//

// 【修改6】控制协议:ap_ctrl_none + while(1) 自由跑 -> s_axilite 每帧 ap_start。

//   原因:HLS 标量参数在 ap_start 时锁存;while(1) 内核永不返回 ap_done,

//   无法重启,寄存器写了不生效。ap_ctrl_hs 无 auto-rewind,不存在踩帧尾问题。

//   B 侧每帧流程:写寄存器 -> ap_start -> DMA 收发 -> 等 TLAST。

// 【修改7】width/height 运行时参数(AXI-Lite),上限 MAX_W=1920、MAX_H=1080。

//   n/width、n%width 改为行列计数器递增,避免真除法,保持 II=1。

//   lbuf 按 MAX_W 开(仍 3×BRAM18,上限提高不增面积)。

// 【修改8】mode/threshold:

//   mode:0=Sobel,1=Prewitt,其余值按 Sobel 处理(PS 侧校验)。

//   threshold:0=饱和输出(禁用阈值);>0=二值化,梯度>=threshold 输出255,否则0。

//

// 注意:

// II=1 是综合目标,真实 Achieved II 以 HLS 综合报告为准。

// 输入断流时等待;不提供超时或坏帧自动恢复。

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

// 每帧一次 ap_start,处理 width×height 一帧。

// -------------------------------------------------------------

void sobel_v3(

    hls::stream<axis_pixel_t>& src,

    hls::stream<axis_pixel_t>& dst,

    int width,

    int height,

    int mode,

    int threshold

) {

#pragma HLS INTERFACE mode=axis port=src

#pragma HLS INTERFACE mode=axis port=dst

#pragma HLS INTERFACE mode=s_axilite port=return bundle=control

#pragma HLS INTERFACE mode=s_axilite port=width bundle=control

#pragma HLS INTERFACE mode=s_axilite port=height bundle=control

#pragma HLS INTERFACE mode=s_axilite port=mode bundle=control

#pragma HLS INTERFACE mode=s_axilite port=threshold bundle=control

    // 帧参数(运行时)。

    const int pixels = width * height;

    const int output_delay = width + 1;

    const int total_steps = pixels + output_delay;

#ifndef __SYNTHESIS__

    assert(width >= 3 && height >= 3 &&

           "width/height must be >= 3");

    assert(width <= MAX_W && height <= MAX_H &&

           "width/height exceed MAX_W/MAX_H");

#endif

    // 三个物理bank循环保存三行。

    // bank表示当前输入行写入哪个bank。

    // 每进入新的一行,只切换bank,不复制整行数据。

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

    // 当前输入行的写入bank,按0→1→2→0循环(每帧重新从0开始)。

    int bank = 0;

    // 行列计数器替代 n/width、n%width,避免真除法。

    int r = 0;

    int c = 0;

    // 用单个循环替代PREFILL、LOAD、

    // COMPUTE、SHIFT以及单独的边界输出循环。

    // 多跑 output_delay 次排空帧尾,只输出边界0,最后一拍带 TLAST。

    STREAM_PIXELS:

    for (int n = 0; n < total_steps; ++n) {

#pragma HLS PIPELINE II=1

        // 仅影响综合报告的延迟估计,不影响功能

        // (256×256 帧 ~ 1920×1080 帧的迭代数范围)。

#pragma HLS LOOP_TRIPCOUNT min=65793 max=2073601

        // 本次迭代可输出的内部像素。

        // 边界和flush阶段保持0。

        uint8_t edge = 0;

        if (n < pixels) {

            uint8_t current = read_pixel(src, n, pixels);

            uint8_t top = 0;

            uint8_t middle = 0;

            // 循环行缓存:

            // 当前行写一个bank,前两行各读一个bank。

            //

            // bank=0:top在1,middle在2,current写0

            // bank=1:top在2,middle在0,current写1

            // bank=2:top在0,middle在1,current写2

            //

            // 前两行用r条件屏蔽尚未写入的缓存,

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

            // 窗口向左移动,右侧插入新的一列。

            // 已有6个像素复用,只引入3个新像素。

            //

            // 每行第0列清除左侧两列,避免跨行残留。

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

            // 窗口更新后:

            // window[0]对应输入第r-2行;

            // window[1]对应输入第r-1行;

            // window[2]对应输入第r行;

            // 三列对应c-2、c-1、c。

            //

            // 只有r>=2且c>=2时窗口完整。

            // 此时中心(r-1,c-1)一定是内部像素。

            if (r >= 2 && c >= 2) {

                edge = edge_from_window(window, mode, threshold);

            }

            // 行结束更新bank与行列计数器。

            if (c == width - 1) {

                bank = (bank == 2) ? 0 : bank + 1;

                c = 0;

                ++r;

            } else {

                ++c;

            }

        }

        // 前 output_delay 次迭代只读输入,不产生输出。

        // 后续每次迭代输出一个像素。

        //

        // 输出索引 = n - output_delay

        // 最后一次输出索引 = pixels - 1

        if (n >= output_delay) {

            const int output_index = n - output_delay;

            write_pixel(

                dst,

                edge,

                output_index == pixels - 1

            );

        }

    }

}

