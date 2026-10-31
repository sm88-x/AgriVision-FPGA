// =============================================================

// RGB2Gray + Gaussian + Edge + Threshold + Morphology

// AXI4-Stream 全链路可配置流水线(system_validation 交付版:sobel_v4_pipeline)

//

// 输入:width×height,32bit/拍 RGBX888:

//   bit[23:16]=R、bit[15:8]=G、bit[7:0]=B、bit[31:24] 忽略;

//   enable_gray=0 时低 8bit 作为灰度直接透传(灰度输入路径,

//   同时构成 V4 逐像素回归通道)。

// 输出:width×height,uint8;窗口级(out_sel=0/2/3)最外一圈强制 0;

//   out_sel=1(gray)为点对点级,全帧有效。

//

// 流水:gray(组合,延迟 0)→ Gaussian 3×3(延迟 width+1)

//   → Edge+Threshold(延迟 2×(width+1))

//   → Morphology 3×3(延迟 3×(width+1))

//   三级窗口各自独立 lbuf[3][MAX_W] + win[3][3],

//   每级边界独立零填充,总输出延迟 = 3×(width+1),

//   total_steps = pixels + 3×(width+1)。

//

// 寄存器(width/height/mode/threshold/morph_mode 偏移与 V4 一致):

//   0x10 width     0x18 height    0x20 mode(0=Sobel,1=Prewitt)

//   0x28 threshold(>0 二值化;==0 饱和)

//   0x30 morph_mode(0=Bypass,1=Erode,2=Dilate)

//   0x38 enable_gray(1=RGB2Gray;0=透传低 8bit)

//   0x40 enable_gauss(1=Gaussian;0=旁路)

//   0x48 out_sel(0=最终 morph;1=gray;2=gauss;3=edge/threshold(tval);非法按 0)

//

// 算法口径(与 B 侧 Golden 同式,见 register_map_sobel_v4_pipeline.md):

//   Y      = (77R + 150G + 29B + 128) >> 8(77+150+29=256)

//   Gauss  = (sum([1 2 1;2 4 2;1 2 1]·w) + 8) >> 4,零填充,round half up

//   Edge   = |Gx|+|Gy|,thr>0 二值化 / ==0 饱和

//   Morph  = tval 3×3 min/max,零填充

//

// 旁路语义(回归关键):

//   enable_gauss=0 时 Gaussian 级输出"线性滞后 D 拍的 gray 流"

//   (与 V4 第一级输出流的滞后语义一致:每级结果相对其流索引滞后

//   D=width+1,窗口中心=索引-D)。实现:c_g>=1 透传窗口中心

//   win_g[1][1]=gray(r-1,c-1);c_g==0 由行尾寄存器输出

//   gray(r-2,width-1);激活窗口延长到 n<pixels+D(flush 行)。

//   由此下游 Edge 级在右/底边界读到的值与 Golden 逐像素一致

//   (真实最右列/最下行不被丢失)——enable_gray=0+enable_gauss=0

//   时整核输出与 sobel_v4 逐像素相同(含环相邻像素)。

//

// 协议:帧 = width*height 拍、TLAST 仅帧尾、阻塞读写、

//   三行缓存循环复用、单循环 II=1 目标、ap_ctrl_hs 每帧重拉 ap_start。

// 注意:II=1 是综合目标,真实 Achieved II 以 HLS 综合报告为准。

// =============================================================

#include <stdint.h>

#if __has_include(<ap_axi_sdata.h>)

#include <ap_axi_sdata.h>

#else

// Fallback for environments without the Xilinx HLS headers.

// This keeps the AXI-stream interfaces usable in host-side builds

// while preserving the same member names and access patterns used here.

template<int W>

struct ap_uint {

    unsigned int value;

    ap_uint() : value(0) {}

    ap_uint(unsigned int v) : value(v) {}

    ap_uint(const ap_uint& other) : value(other.value) {}

    unsigned int to_uint() const { return value; }

    ap_uint& operator=(unsigned int v) {

        value = v;

        return *this;

    }

    ap_uint& operator=(const ap_uint& other) {

        value = other.value;

        return *this;

    }

    operator unsigned int() const { return value; }

};

template<int D, int U, int TI, int TD>

struct ap_axiu {

    ap_uint<D> data;

    unsigned int keep;

    unsigned int strb;

    unsigned int last;

    ap_axiu() : data(0), keep(0), strb(0), last(0) {}

};

#endif

#if __has_include(<hls_stream.h>)

#include <hls_stream.h>

#else

#include <deque>

namespace hls {

template <typename T>

class stream {

public:

    void write(const T& val) {

        queue_.push_back(val);

    }

    T read() {

        T val = queue_.front();

        queue_.pop_front();

        return val;

    }

    bool empty() const {

        return queue_.empty();

    }

    size_t size() const {

        return queue_.size();

    }

private:

    std::deque<T> queue_;

};

}

#endif

#ifndef __SYNTHESIS__

#include <cassert>

#endif

typedef ap_axiu<32, 0, 0, 0> axis_in_t;

typedef ap_axiu<8, 0, 0, 0> axis_out_t;

// 尺寸上限(寄存器合法范围:3 <= width <= MAX_W,3 <= height <= MAX_H;

// 范围校验由 PS 侧负责,核内仅在 C 仿真断言)

static const int MAX_W = 1920;

static const int MAX_H = 1080;

// -------------------------------------------------------------

// 输入:每拍一个 RGBX888 32bit 字,keep=0xF。

// -------------------------------------------------------------

static uint32_t read_pixel(

    hls::stream<axis_in_t>& src,

    int index,

    int pixels

) {

#pragma HLS INLINE

    axis_in_t packet = src.read();

#ifndef __SYNTHESIS__

    assert(packet.keep == 0xF &&

           "每个输入拍必须携带 4 个有效字节(RGBX888)");

    assert((packet.last != 0) == (index == pixels - 1) &&

           "输入 TLAST 只能出现在帧尾");

#endif

    return packet.data.to_uint();

}

// -------------------------------------------------------------

// 输出:每拍一个有效字节,TLAST 仅在整帧末尾置1。

// -------------------------------------------------------------

static void write_pixel(

    hls::stream<axis_out_t>& dst,

    uint8_t value,

    bool last

) {

#pragma HLS INLINE

    axis_out_t packet;

    packet.data = value;

    packet.keep = 1;

    packet.strb = 1;

    packet.last = last ? 1 : 0;

    dst.write(packet);

}

// -------------------------------------------------------------

// RGB2Gray:Y=(77R+150G+29B+128)>>8。

// 77+150+29=256,最大和 65280+128 < 2^16,不溢出。

// -------------------------------------------------------------

static uint8_t gray_from_rgb(uint32_t rgb) {

#pragma HLS INLINE

    const uint32_t r = (rgb >> 16) & 0xFFu;

    const uint32_t g = (rgb >> 8) & 0xFFu;

    const uint32_t b = rgb & 0xFFu;

    return (uint8_t)((77u * r + 150u * g + 29u * b + 128u) >> 8);

}

// -------------------------------------------------------------

// Gaussian 3×3:[1 2 1;2 4 2;1 2 1]/16。

// 零填充边界;round half up:(sum+8)>>4;sum<=16*255=4080。

// -------------------------------------------------------------

static uint8_t gauss_from_window(const uint8_t w[3][3]) {

#pragma HLS INLINE

    const uint16_t sum =

         1u * w[0][0] + 2u * w[0][1] + 1u * w[0][2]

      + 2u * w[1][0] + 4u * w[1][1] + 2u * w[1][2]

      + 1u * w[2][0] + 2u * w[2][1] + 1u * w[2][2];

    return (uint8_t)((sum + 8u) >> 4);

}

// -------------------------------------------------------------

// 窗口计算:mode 选 Sobel/Prewitt,threshold 决定输出方式。

// 与 sobel_v4 逐位一致(回归要求)。

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

// 与 sobel_v4 逐位一致(回归要求)。

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

    // Bypass:透传窗口中心 = 阈值化输出 tval。

    return w[1][1];

}

// -------------------------------------------------------------

// 每帧一次 ap_start,处理 width×height 一帧。

// -------------------------------------------------------------

void sobel_v4_baseline(

    hls::stream<axis_in_t>& src,

    hls::stream<axis_out_t>& dst,

    int width,

    int height,

    int mode,

    int threshold,

    int morph_mode,

    int enable_gray,

    int enable_gauss,

    int out_sel

) {

#pragma HLS INTERFACE mode=axis port=src

#pragma HLS INTERFACE mode=axis port=dst

#pragma HLS INTERFACE mode=s_axilite port=return bundle=control

#pragma HLS INTERFACE mode=s_axilite port=width bundle=control

#pragma HLS INTERFACE mode=s_axilite port=height bundle=control

#pragma HLS INTERFACE mode=s_axilite port=mode bundle=control

#pragma HLS INTERFACE mode=s_axilite port=threshold bundle=control

#pragma HLS INTERFACE mode=s_axilite port=morph_mode bundle=control

#pragma HLS INTERFACE mode=s_axilite port=enable_gray bundle=control

#pragma HLS INTERFACE mode=s_axilite port=enable_gauss bundle=control

#pragma HLS INTERFACE mode=s_axilite port=out_sel bundle=control

    // 帧参数(运行时)。

    const int pixels = width * height;

    const int D = width + 1;             // 单级窗口中心滞后

    const int total_delay = 3 * D;       // 三级串联总滞后

    const int total_steps = pixels + total_delay;

#ifndef __SYNTHESIS__

    assert(width >= 3 && height >= 3 &&

           "width/height must be >= 3");

    assert(width <= MAX_W && height <= MAX_H &&

           "width/height exceed MAX_W/MAX_H");

    assert(morph_mode >= 0 && morph_mode <= 2 &&

           "morph_mode must be 0/1/2");

    assert((enable_gray == 0 || enable_gray == 1) &&

           "enable_gray must be 0/1");

    assert((enable_gauss == 0 || enable_gauss == 1) &&

           "enable_gauss must be 0/1");

    assert(out_sel >= 0 && out_sel <= 3 &&

           "out_sel must be 0..3");

#endif

    // 输出延迟按 out_sel:0→3D(最终 morph),1→0(gray),

    // 2→D(gauss),3→2D(edge/threshold)。总拍数固定 3D 不变。

    int out_delay = total_delay;

    if (out_sel == 1) {

        out_delay = 0;

    } else if (out_sel == 2) {

        out_delay = D;

    } else if (out_sel == 3) {

        out_delay = 2 * D;

    }

    // gray 为点对点级,输出全帧有效;其余各级为窗口级,外圈强制 0。

    const bool ring_zero = (out_sel != 1);

    // ================= 第一级:Gaussian 窗口(输入 = gray 流) =================

    uint8_t lbuf_g[3][MAX_W];

#pragma HLS ARRAY_PARTITION variable=lbuf_g complete dim=1

#pragma HLS BIND_STORAGE variable=lbuf_g type=ram_s2p impl=bram

    uint8_t win_g[3][3];

#pragma HLS ARRAY_PARTITION variable=win_g complete dim=0

    INIT_WIN_G_ROW:

    for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

        INIT_WIN_G_COL:

        for (int x = 0; x < 3; ++x) {

#pragma HLS UNROLL

            win_g[y][x] = 0;

        }

    }

    int bank_g = 0;

    int r_g = 0;

    int c_g = 0;

    // 旁路(enable_gauss=0)行尾寄存器:旁路输出 = 线性滞后 D 拍的 gray,

    // 位置(r,0)需输出上一行行尾 gray(r-2,w-1)(c_g>=1 由 win_g[1][1]

    // 覆盖)。prev2 每行行首从 prev 移入,prev 每行行尾(仅输入期)刷新。

    uint8_t prev_row_last = 0;

    uint8_t prev2_row_last = 0;

    // ================= 第二级:Edge 窗口(输入 = gauss 流) =================

    uint8_t lbuf_e[3][MAX_W];

#pragma HLS ARRAY_PARTITION variable=lbuf_e complete dim=1

#pragma HLS BIND_STORAGE variable=lbuf_e type=ram_s2p impl=bram

    uint8_t win_e[3][3];

#pragma HLS ARRAY_PARTITION variable=win_e complete dim=0

    INIT_WIN_E_ROW:

    for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

        INIT_WIN_E_COL:

        for (int x = 0; x < 3; ++x) {

#pragma HLS UNROLL

            win_e[y][x] = 0;

        }

    }

    int bank_e = 0;

    int r_e = 0;

    int c_e = 0;

    // ================= 第三级:Morphology 窗口(输入 = tval 流) =================

    uint8_t lbuf_m[3][MAX_W];

#pragma HLS ARRAY_PARTITION variable=lbuf_m complete dim=1

#pragma HLS BIND_STORAGE variable=lbuf_m type=ram_s2p impl=bram

    uint8_t win_m[3][3];

#pragma HLS ARRAY_PARTITION variable=win_m complete dim=0

    INIT_WIN_M_ROW:

    for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

        INIT_WIN_M_COL:

        for (int x = 0; x < 3; ++x) {

#pragma HLS UNROLL

            win_m[y][x] = 0;

        }

    }

    int bank_m = 0;

    int r_m = 0;

    int c_m = 0;

    // 输出行列独立计数(最外圈判定;避免真除法保 II=1)。

    int out_r = 0;

    int out_c = 0;

    // 单循环(V4 同构):各级在同一拍消费上一级的同拍值;

    // 激活窗口依次平移 D 拍(流索引 0 / D / 2D),每级结果相对

    // 其流索引滞后 D(窗口中心 = 索引 - D),输出再按 out_sel

    // 的延迟取对应级值 —— 逐像素与各级 golden 图对齐,无累计平移。

    STREAM_PIXELS:

    for (int n = 0; n < total_steps; ++n) {

#pragma HLS PIPELINE II=1

        // 仅影响综合报告的延迟估计,不影响功能。

#pragma HLS LOOP_TRIPCOUNT min=66307 max=2079363

        // 各级本拍输出(默认 0 = 帧外零填充/环 0)。

        uint8_t gray = 0;

        uint8_t gauss_val = 0;

        uint8_t edge_val = 0;

        uint8_t morph_val = 0;

        // ---- 输入 → gray(点对点,延迟 0)----

        if (n < pixels) {

            const uint32_t word = read_pixel(src, n, pixels);

            gray = (enable_gray != 0)

                ? gray_from_rgb(word)

                : (uint8_t)(word & 0xFFu);

        }

        // ---- 第一级:Gaussian(与 V4 第一级同拍:消费本拍 gray(n))----

        // 旁路时激活延长 D 拍(flush 行):输出 = 线性滞后 D 拍的 gray 流,

        // 与 V4 第一级输出流的滞后语义一致 —— 下游 Edge 级在右/底边界

        // 读到的值与 Golden 逐像素一致(真实最右列/最下行,不再丢失)。

        if (n < pixels ||

            (enable_gauss == 0 && n < pixels + D)) {

            uint8_t top = 0;

            uint8_t mid = 0;

            switch (bank_g) {

            case 0:

                if (r_g >= 2) {

                    top = lbuf_g[1][c_g];

                }

                if (r_g >= 1) {

                    mid = lbuf_g[2][c_g];

                }

                lbuf_g[0][c_g] = gray;

                break;

            case 1:

                if (r_g >= 2) {

                    top = lbuf_g[2][c_g];

                }

                if (r_g >= 1) {

                    mid = lbuf_g[0][c_g];

                }

                lbuf_g[1][c_g] = gray;

                break;

            default:  // bank_g == 2

                if (r_g >= 2) {

                    top = lbuf_g[0][c_g];

                }

                if (r_g >= 1) {

                    mid = lbuf_g[1][c_g];

                }

                lbuf_g[2][c_g] = gray;

                break;

            }

            WINDOW_G_SHIFT:

            for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

                if (c_g == 0) {

                    win_g[y][0] = 0;

                    win_g[y][1] = 0;

                } else {

                    win_g[y][0] = win_g[y][1];

                    win_g[y][1] = win_g[y][2];

                }

            }

            win_g[0][2] = top;

            win_g[1][2] = mid;

            win_g[2][2] = gray;

            if (enable_gauss != 0) {

                if (r_g >= 2 && c_g >= 2) {

                    gauss_val = gauss_from_window(win_g);

                }

            } else {

                // 旁路:输出 gray 流滞后 D 拍的值。

                // c_g >= 1:窗口中心 = gray(r_g-1, c_g-1);

                // c_g == 0:行尾寄存器 = gray(r_g-2, width-1)

                // (帧外/前两行为 0,与线性滞后 D 的零填充一致)。

                if (c_g >= 1) {

                    gauss_val = win_g[1][1];

                } else {

                    gauss_val = prev2_row_last;

                }

            }

            // 旁路行尾寄存器维护:行首从 prev 移入 prev2;

            // 行尾刷新 prev(仅输入期,flush 期保留 gray(h-1,w-1)

            // 供位置 (h+1,0) 输出)。

            if (c_g == 0) {

                prev2_row_last = prev_row_last;

            }

            if (c_g == width - 1 && n < pixels) {

                prev_row_last = gray;

            }

            if (c_g == width - 1) {

                bank_g = (bank_g == 2) ? 0 : bank_g + 1;

                c_g = 0;

                ++r_g;

            } else {

                ++c_g;

            }

        }

        // ---- 第二级:Edge+Threshold(消费同拍 gauss_val,流索引 n-D)----

        if (n >= D && n < D + pixels) {

            uint8_t top = 0;

            uint8_t mid = 0;

            switch (bank_e) {

            case 0:

                if (r_e >= 2) {

                    top = lbuf_e[1][c_e];

                }

                if (r_e >= 1) {

                    mid = lbuf_e[2][c_e];

                }

                lbuf_e[0][c_e] = gauss_val;

                break;

            case 1:

                if (r_e >= 2) {

                    top = lbuf_e[2][c_e];

                }

                if (r_e >= 1) {

                    mid = lbuf_e[0][c_e];

                }

                lbuf_e[1][c_e] = gauss_val;

                break;

            default:  // bank_e == 2

                if (r_e >= 2) {

                    top = lbuf_e[0][c_e];

                }

                if (r_e >= 1) {

                    mid = lbuf_e[1][c_e];

                }

                lbuf_e[2][c_e] = gauss_val;

                break;

            }

            WINDOW_E_SHIFT:

            for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

                if (c_e == 0) {

                    win_e[y][0] = 0;

                    win_e[y][1] = 0;

                } else {

                    win_e[y][0] = win_e[y][1];

                    win_e[y][1] = win_e[y][2];

                }

            }

            win_e[0][2] = top;

            win_e[1][2] = mid;

            win_e[2][2] = gauss_val;

            if (r_e >= 2 && c_e >= 2) {

                edge_val = edge_from_window(win_e, mode, threshold);

            }

            if (c_e == width - 1) {

                bank_e = (bank_e == 2) ? 0 : bank_e + 1;

                c_e = 0;

                ++r_e;

            } else {

                ++c_e;

            }

        }

        // ---- 第三级:Morphology(消费同拍 edge_val,流索引 n-2D)----

        if (n >= 2 * D && n < 2 * D + pixels) {

            uint8_t top = 0;

            uint8_t mid = 0;

            switch (bank_m) {

            case 0:

                if (r_m >= 2) {

                    top = lbuf_m[1][c_m];

                }

                if (r_m >= 1) {

                    mid = lbuf_m[2][c_m];

                }

                lbuf_m[0][c_m] = edge_val;

                break;

            case 1:

                if (r_m >= 2) {

                    top = lbuf_m[2][c_m];

                }

                if (r_m >= 1) {

                    mid = lbuf_m[0][c_m];

                }

                lbuf_m[1][c_m] = edge_val;

                break;

            default:  // bank_m == 2

                if (r_m >= 2) {

                    top = lbuf_m[0][c_m];

                }

                if (r_m >= 1) {

                    mid = lbuf_m[1][c_m];

                }

                lbuf_m[2][c_m] = edge_val;

                break;

            }

            WINDOW_M_SHIFT:

            for (int y = 0; y < 3; ++y) {

#pragma HLS UNROLL

                if (c_m == 0) {

                    win_m[y][0] = 0;

                    win_m[y][1] = 0;

                } else {

                    win_m[y][0] = win_m[y][1];

                    win_m[y][1] = win_m[y][2];

                }

            }

            win_m[0][2] = top;

            win_m[1][2] = mid;

            win_m[2][2] = edge_val;

            if (r_m >= 2 && c_m >= 2) {

                morph_val = morph_from_window(win_m, morph_mode);

            }

            if (c_m == width - 1) {

                bank_m = (bank_m == 2) ? 0 : bank_m + 1;

                c_m = 0;

                ++r_m;

            } else {

                ++c_m;

            }

        }

        // ---- 输出:按 out_sel 选级值,窗口级外圈强制 0 ----

        if (n >= out_delay) {

            const int output_index = n - out_delay;

            if (output_index < pixels) {

                uint8_t out_val;

                switch (out_sel) {

                case 1:

                    out_val = gray;

                    break;

                case 2:

                    out_val = gauss_val;

                    break;

                case 3:

                    out_val = edge_val;

                    break;

                default:

                    out_val = morph_val;

                    break;

                }

                const bool is_ring = ring_zero && (

                    out_r == 0 || out_r == height - 1 ||

                    out_c == 0 || out_c == width - 1);

                write_pixel(

                    dst,

                    is_ring ? (uint8_t)0 : out_val,

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

}

