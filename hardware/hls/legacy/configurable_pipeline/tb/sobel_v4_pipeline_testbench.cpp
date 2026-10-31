// =============================================================

// sobel_v4_pipeline testbench(C Simulation / Cosim)

//

// 帧清单(C 仿真 1 轮;RTL cosim 缩减为代表性子集):

//   A. V4 回归(enable_gray=0, enable_gauss=0,灰阶输入低 8bit):

//      8 帧合成图 × V4 (mode,thr) 组合,morph_mode=0 ——

//      输出必须与 sobel_v4 逐像素一致。

//   B. V4 回归形态学:morph 测试图 × 256/640 宽 × {Erode,Dilate}

//      + 随机灰阶 thr=0(灰度 min/max 语义),共 26 帧。

//   C. RGB 全链(enable_gray=1, enable_gauss=1):

//      纯色(200,100,50)、三基色、棋盘格、边界亮点、

//      噪声、随机 × 256/320/640 三档尺寸。

//   D. 组合矩阵:enable_gray × enable_gauss × morph_mode,

//      灰阶编码输入在两种 enable_gray 下输出必须相同。

//   E. out_sel 导出:同一帧 × {0,1,2,3} 逐像素对拍各级 golden;

//      旁路下 out_sel=2 导出 = 原始灰阶(环 0)。

//

// golden 链(显式循环,与 HLS 同语义,见

// register_map_sobel_v4_pipeline.md):

//   gray  = enable_gray ? (77R+150G+29B+128)>>8 : 低 8bit

//   stage1= enable_gauss ? gauss3×3(gray,环0) : gray

//           (旁路时下游 Edge 看到原始 gray 流,与 V4 输入流

//           逐像素一致 —— 回归关键)

//   tval  = Sobel/Prewitt(stage1,零填充),thr>0 二值化,环0

//   morph = 3×3 min/max(tval),环0

//   输出  = out_sel:0=morph(环0)、1=gray(全帧有效)、

//           2=stage1(环0)、3=tval(环0)

//

// 每帧检查:

//   输出拍数 == width*height、TLAST 仅帧尾一次、

//   输入 keep==0xF 消费完整帧、逐像素 == golden、

//   窗口级输出最外圈全 0(out_sel=1 无此要求)。

// =============================================================

#include <stdio.h>

#include <stdint.h>

#include <string.h>

#include <stdlib.h>

#include <ap_axi_sdata.h>

#include <hls_stream.h>

typedef ap_axiu<32, 0, 0, 0> axis_in_t;

typedef ap_axiu<8, 0, 0, 0> axis_out_t;

extern void sobel_v4_pipeline(

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

);

static const int MAX_W = 640;

static const int MAX_H = 480;

static const int MAX_PX = MAX_W * MAX_H;   // 307200

static const int MAX_ERROR_PRINTS = 10;

// RTL cosim 缩减判别(同 V4 tb):xsim 在 Windows 是 32 位进程,

// 全量帧会内存耗尽;功能全覆盖由 C 仿真保证,cosim 只做代表对拍。

static bool in_rtl_cosim(void) {

#ifdef __RTL_SIMULATION__

    return true;

#else

    return getenv("XILINX_SIMULATOR") != NULL;

#endif

}

// 帧缓冲放静态区,避免深栈(大尺寸帧会爆栈)。

static uint32_t g_rgb[MAX_PX];    // RGBX888 输入字

static uint8_t g_gray[MAX_PX];    // gray 图

static uint8_t g_gauss[MAX_PX];   // gauss 图(环0;旁路=gray 环0)

static uint8_t g_tval[MAX_PX];    // edge/threshold 图(环0)

static uint8_t g_golden[MAX_PX];  // 期望输出

// -------------------------------------------------------------

// 打包:bit[23:16]=R、bit[15:8]=G、bit[7:0]=B。

// -------------------------------------------------------------

static uint32_t pack_rgb(uint8_t r, uint8_t g, uint8_t b) {

    return ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;

}

// -------------------------------------------------------------

// RGB2Gray 参考:Y=(77R+150G+29B+128)>>8。

// -------------------------------------------------------------

static uint8_t ref_y(uint32_t rgb) {

    return (uint8_t)(

        (77u * ((rgb >> 16) & 0xFFu)

        + 150u * ((rgb >> 8) & 0xFFu)

        + 29u * (rgb & 0xFFu)

        + 128u) >> 8

    );

}

// -------------------------------------------------------------

// 参考梯度(|Gx|+|Gy| 原值,不做饱和/二值化),与 V4 同口径。

// 仅对内部像素调用。

// -------------------------------------------------------------

static uint16_t ref_sobel_mag(const uint8_t* img, int w, int r, int c) {

    int16_t gx =

        -(int16_t)img[(r-1)*w + (c-1)] + (int16_t)img[(r-1)*w + (c+1)]

        - 2*(int16_t)img[r*w + (c-1)] + 2*(int16_t)img[r*w + (c+1)]

        - (int16_t)img[(r+1)*w + (c-1)] + (int16_t)img[(r+1)*w + (c+1)];

    int16_t gy =

        -(int16_t)img[(r-1)*w + (c-1)] - 2*(int16_t)img[(r-1)*w + c] - (int16_t)img[(r-1)*w + (c+1)]

        + (int16_t)img[(r+1)*w + (c-1)] + 2*(int16_t)img[(r+1)*w + c] + (int16_t)img[(r+1)*w + (c+1)];

    uint16_t ax = (gx < 0) ? (uint16_t)(-gx) : (uint16_t)gx;

    uint16_t ay = (gy < 0) ? (uint16_t)(-gy) : (uint16_t)gy;

    return ax + ay;

}

static uint16_t ref_prewitt_mag(const uint8_t* img, int w, int r, int c) {

    int16_t gx =

        -(int16_t)img[(r-1)*w + (c-1)] + (int16_t)img[(r-1)*w + (c+1)]

        - (int16_t)img[r*w + (c-1)] + (int16_t)img[r*w + (c+1)]

        - (int16_t)img[(r+1)*w + (c-1)] + (int16_t)img[(r+1)*w + (c+1)];

    int16_t gy =

        -(int16_t)img[(r-1)*w + (c-1)] - (int16_t)img[(r-1)*w + c] - (int16_t)img[(r-1)*w + (c+1)]

        + (int16_t)img[(r+1)*w + (c-1)] + (int16_t)img[(r+1)*w + c] + (int16_t)img[(r+1)*w + (c+1)];

    uint16_t ax = (gx < 0) ? (uint16_t)(-gx) : (uint16_t)gx;

    uint16_t ay = (gy < 0) ? (uint16_t)(-gy) : (uint16_t)gy;

    return ax + ay;

}

// -------------------------------------------------------------

// 由输入图现算 gray 图(g_gray)。

// -------------------------------------------------------------

static void make_gray_img(int w, int h, int enable_gray) {

    const int pixels = w * h;

    for (int i = 0; i < pixels; ++i) {

        g_gray[i] = (enable_gray != 0)

            ? ref_y(g_rgb[i])

            : (uint8_t)(g_rgb[i] & 0xFFu);

    }

}

// -------------------------------------------------------------

// gauss 图(g_gauss):3×3 [1 2 1;2 4 2;1 2 1]/16,round half up,

// 内部像素窗口必然整窗在帧内,最外圈强制 0。

// enable_gauss=0(旁路):g_gauss = gray(仅导出 golden 用,环0)。

// 链 golden 用 stage1 = enable_gauss ? g_gauss : g_gray。

// -------------------------------------------------------------

static void make_gauss_img(int w, int h, int enable_gauss) {

    memset(g_gauss, 0, (size_t)w * h);

    if (enable_gauss == 0) {

        for (int r = 1; r < h - 1; ++r) {

            for (int c = 1; c < w - 1; ++c) {

                g_gauss[r * w + c] = g_gray[r * w + c];

            }

        }

        return;

    }

    for (int r = 1; r < h - 1; ++r) {

        for (int c = 1; c < w - 1; ++c) {

            uint16_t sum = 0;

            for (int dr = -1; dr <= 1; ++dr) {

                for (int dc = -1; dc <= 1; ++dc) {

                    const int wgt = (dr == 0 ? 2 : 1) * (dc == 0 ? 2 : 1);

                    sum += (uint16_t)(

                        (uint16_t)g_gray[(r + dr) * w + (c + dc)] * wgt);

                }

            }

            g_gauss[r * w + c] = (uint8_t)((sum + 8u) >> 4);

        }

    }

}

// -------------------------------------------------------------

// 由 stage1 图现算 tval(g_tval,按 mode/threshold 口径,环0)。

// -------------------------------------------------------------

static void make_tval_img(

    const uint8_t* img,

    int w,

    int h,

    int mode,

    int threshold

) {

    memset(g_tval, 0, (size_t)w * h);

    for (int r = 1; r < h - 1; ++r) {

        for (int c = 1; c < w - 1; ++c) {

            const uint16_t mag = (mode == 1)

                ? ref_prewitt_mag(img, w, r, c)

                : ref_sobel_mag(img, w, r, c);

            if (threshold > 0) {

                g_tval[r * w + c] =

                    (mag >= (uint16_t)threshold) ? 255 : 0;

            } else {

                g_tval[r * w + c] =

                    (mag > 255) ? 255 : (uint8_t)mag;

            }

        }

    }

}

// -------------------------------------------------------------

// 对 g_tval 叠加 3×3 形态学 → g_golden:

//   morph_mode 1 = Erode(9 输入 min),2 = Dilate(9 输入 max),

//   0 = Bypass(不动)。环强制 0。

// -------------------------------------------------------------

static void apply_morph(int w, int h, int morph_mode) {

    memcpy(g_golden, g_tval, (size_t)w * h);

    if (morph_mode == 0) {

        return;

    }

    for (int r = 0; r < h; ++r) {

        for (int c = 0; c < w; ++c) {

            if (r == 0 || r == h - 1 || c == 0 || c == w - 1) {

                g_golden[r * w + c] = 0;

                continue;

            }

            uint8_t acc = g_tval[(r - 1) * w + (c - 1)];

            for (int dr = -1; dr <= 1; ++dr) {

                for (int dc = -1; dc <= 1; ++dc) {

                    const uint8_t v = g_tval[(r + dr) * w + (c + dc)];

                    if (morph_mode == 1) {

                        if (v < acc) acc = v;

                    } else {

                        if (v > acc) acc = v;

                    }

                }

            }

            g_golden[r * w + c] = acc;

        }

    }

}

// -------------------------------------------------------------

// 期望输出(g_golden):按 out_sel 选级。

//   0=morph(默认,已由 apply_morph 写入)

//   1=gray(全帧有效)

//   2=gauss 图(环0;旁路时 = 原始 gray 环0)

//   3=tval(环0)

// -------------------------------------------------------------

static void compute_expected(

    int w,

    int h,

    int mode,

    int threshold,

    int morph_mode,

    int enable_gray,

    int enable_gauss,

    int out_sel

) {

    const int pixels = w * h;

    make_gray_img(w, h, enable_gray);

    make_gauss_img(w, h, enable_gauss);

    // 链输入:旁路时下游看到原始 gray 流(与 V4 输入流一致)。

    const uint8_t* stage1 = (enable_gauss != 0) ? g_gauss : g_gray;

    make_tval_img(stage1, w, h, mode, threshold);

    apply_morph(w, h, morph_mode);

    switch (out_sel) {

    case 1:

        memcpy(g_golden, g_gray, pixels);

        break;

    case 2:

        memcpy(g_golden, g_gauss, pixels);

        break;

    case 3:

        memcpy(g_golden, g_tval, pixels);

        break;

    default:

        break;  // 0 = morph,apply_morph 已写入

    }

}

// -------------------------------------------------------------

// 确定性伪随机图(LCG,固定种子),跨平台可复现。

// -------------------------------------------------------------

static void make_synthetic(uint8_t* img, int w, int h, uint32_t seed) {

    uint32_t state = seed;

    for (int i = 0; i < w * h; ++i) {

        state = state * 1103515245u + 12345u;

        img[i] = (uint8_t)((state >> 16) & 0xFF);

    }

}

// -------------------------------------------------------------

// 形态学测试图(与 V4 tb 同款,写 g_gray):

//   0 全0          1 全255

//   2 中心单点     3 中心3×3方块(中心 = (h/2, w/2))

//   4 贴边目标(左上角8×8块,压最外圈)

//   5 随机二值图(LCG,0/255)

//   6 随机灰度图(LCG,0..255)

// -------------------------------------------------------------

static void make_pattern(uint8_t* img, int w, int h, int pattern, uint32_t seed) {

    memset(img, 0, (size_t)w * h);

    const int cr = h / 2;

    const int cc = w / 2;

    uint32_t state = seed;

    switch (pattern) {

    case 1:  // 全255

        memset(img, 255, (size_t)w * h);

        break;

    case 2:  // 中心单点

        img[cr * w + cc] = 255;

        break;

    case 3:  // 中心3×3方块

        for (int dr = -1; dr <= 1; ++dr)

            for (int dc = -1; dc <= 1; ++dc)

                img[(cr + dr) * w + (cc + dc)] = 255;

        break;

    case 4:  // 贴边目标:左上角8×8块

        for (int r = 0; r < 8; ++r)

            for (int c = 0; c < 8; ++c)

                img[r * w + c] = 255;

        break;

    case 5:  // 随机二值图

        for (int i = 0; i < w * h; ++i) {

            state = state * 1103515245u + 12345u;

            img[i] = ((state >> 16) & 1) ? (uint8_t)255 : (uint8_t)0;

        }

        break;

    case 6:  // 随机灰度图

        for (int i = 0; i < w * h; ++i) {

            state = state * 1103515245u + 12345u;

            img[i] = (uint8_t)((state >> 16) & 0xFF);

        }

        break;

    default:  // 0 = 全0

        break;

    }

}

// -------------------------------------------------------------

// 灰阶图打包为 RGBX 输入字:R=G=B=g(低 8bit = g)。

// Y(g,g,g) = g,故灰阶编码输入在 enable_gray=0/1 两条路径下

// gray 图相同(V4 回归与双路径一致性测试的基础)。

// -------------------------------------------------------------

static void pack_gray_to_rgb(int w, int h) {

    const int pixels = w * h;

    for (int i = 0; i < pixels; ++i) {

        const uint32_t g = g_gray[i];

        g_rgb[i] = (g << 16) | (g << 8) | g;

    }

}

// -------------------------------------------------------------

// 输入图生成(RGB 路径)。

// -------------------------------------------------------------

static void make_rgb_solid(int w, int h, uint8_t r, uint8_t g, uint8_t b) {

    const uint32_t word = pack_rgb(r, g, b);

    for (int i = 0; i < w * h; ++i) {

        g_rgb[i] = word;

    }

}

static void make_rgb_checker(int w, int h, int block) {

    for (int r = 0; r < h; ++r) {

        for (int c = 0; c < w; ++c) {

            g_rgb[r * w + c] = (((r / block) + (c / block)) & 1)

                ? pack_rgb(255, 255, 255)

                : pack_rgb(0, 0, 0);

        }

    }

}

static void make_rgb_noise(int w, int h, uint32_t seed) {

    uint32_t state = seed;

    for (int i = 0; i < w * h; ++i) {

        state = state * 1103515245u + 12345u;

        const uint8_t r = (uint8_t)((state >> 16) & 0xFF);

        state = state * 1103515245u + 12345u;

        const uint8_t g = (uint8_t)((state >> 16) & 0xFF);

        state = state * 1103515245u + 12345u;

        const uint8_t b = (uint8_t)((state >> 16) & 0xFF);

        g_rgb[i] = pack_rgb(r, g, b);

    }

}

// -------------------------------------------------------------

// 单帧测试:g_rgb 已填输入,g_golden 已填期望。

// 返回0表示PASS,非0表示FAIL。

// -------------------------------------------------------------

static int check_frame(

    const char* label,

    int frame_id,

    int w,

    int h,

    int mode,

    int threshold,

    int morph_mode,

    int enable_gray,

    int enable_gauss,

    int out_sel,

    hls::stream<axis_in_t>& src_stream,

    hls::stream<axis_out_t>& dst_stream

) {

    const int pixels = w * h;

    printf(

        "\n[Frame %d] %s (%dx%d, mode=%d, thr=%d, morph=%d, "

        "gray=%d, gauss=%d, out_sel=%d)\n",

        frame_id, label, w, h, mode, threshold, morph_mode,

        enable_gray, enable_gauss, out_sel

    );

    // 上一帧不能留下未消费的输入或输出。

    if (!src_stream.empty() || !dst_stream.empty()) {

        printf("FAIL: streams are not empty before frame\n");

        return 1;

    }

    // 将整帧输入打包为 AXI Stream 数据。

    for (int i = 0; i < pixels; ++i) {

        axis_in_t packet;

        packet.data = g_rgb[i];

        packet.keep = 0xF;   // 4 字节全有效(RGBX888)

        packet.strb = 0xF;

        packet.last = (i == pixels - 1) ? 1 : 0;

        src_stream.write(packet);

    }

    // C Simulation中每次调用处理一帧。

    sobel_v4_pipeline(

        src_stream, dst_stream,

        w, h,

        mode, threshold, morph_mode,

        enable_gray, enable_gauss, out_sel

    );

    int printed = 0;

    int pixel_errors = 0;

    int protocol_errors = 0;

    int border_errors = 0;

    int output_count = 0;

    int last_count = 0;

    int max_error = 0;

    unsigned long total_error = 0;

    // out_sel=1(gray)为点对点级,全帧有效,不做环 0 检查。

    const bool check_border = (out_sel != 1);

    // 先检查empty再read,避免输出不足时 testbench 自身阻塞读。

    while (!dst_stream.empty()) {

        axis_out_t packet = dst_stream.read();

        const int index = output_count++;

        const unsigned value = packet.data.to_uint();

        const bool last = packet.last != 0;

        const bool expected_last = index == pixels - 1;

        if (last) {

            ++last_count;

        }

        if (packet.keep != 1 ||

            packet.strb != 1 ||

            last != expected_last) {

            ++protocol_errors;

            if (printed < MAX_ERROR_PRINTS) {

                printf(

                    "  PROTOCOL idx=%d: "

                    "keep=%u strb=%u last=%u "

                    "(expected 1,1,%u)\n",

                    index,

                    packet.keep.to_uint(),

                    packet.strb.to_uint(),

                    (unsigned)last,

                    (unsigned)expected_last

                );

                ++printed;

            }

        }

        // 多余输出仍然读出并计数,但不访问golden越界位置。

        if (index >= pixels) {

            continue;

        }

        const int r = index / w;

        const int c = index % w;

        const unsigned expected = g_golden[index];

        int difference = (int)value - (int)expected;

        if (difference < 0) {

            difference = -difference;

        }

        total_error += (unsigned long)difference;

        if (difference > max_error) {

            max_error = difference;

        }

        if (value != expected) {

            ++pixel_errors;

            if (printed < MAX_ERROR_PRINTS) {

                printf(

                    "  MISMATCH idx=%d (r=%d,c=%d): "

                    "got=%u golden=%u\n",

                    index,

                    r,

                    c,

                    value,

                    expected

                );

                ++printed;

            }

        }

        if (check_border) {

            const bool border =

                r == 0 || r == h - 1 ||

                c == 0 || c == w - 1;

            if (border && value != 0) {

                ++border_errors;

            }

        }

    }

    // 检查内核是否读取完整帧。

    int unread_input = 0;

    while (!src_stream.empty()) {

        (void)src_stream.read();

        ++unread_input;

    }

    if (unread_input != 0) {

        printf(

            "  FAIL: %d input beats were not consumed\n",

            unread_input

        );

    }

    if (output_count != pixels) {

        printf(

            "  FAIL: output count=%d, expected=%d\n",

            output_count,

            pixels

        );

    }

    if (last_count != 1) {

        printf(

            "  FAIL: output TLAST count=%d, expected=1\n",

            last_count

        );

    }

    printf(

        "  Output=%d, pixel_errors=%d, "

        "protocol_errors=%d, border_errors=%d\n",

        output_count,

        pixel_errors,

        protocol_errors,

        border_errors

    );

    // 输出缺失时不把部分数据的误差均值当作完整帧指标。

    if (output_count == pixels) {

        printf(

            "  Max error=%d, mean error=%.6f\n",

            max_error,

            (double)total_error / (double)pixels

        );

    }

    const bool passed =

        unread_input == 0 &&

        output_count == pixels &&

        last_count == 1 &&

        pixel_errors == 0 &&

        protocol_errors == 0 &&

        border_errors == 0;

    printf(

        "  %s: %s\n",

        label,

        passed ? "PASS" : "FAIL"

    );

    return passed ? 0 : 1;

}

// -------------------------------------------------------------

// 生成输入 + 期望 + 跑帧。

// -------------------------------------------------------------

static int run_frame(

    const char* label,

    int frame_id,

    int w,

    int h,

    int mode,

    int threshold,

    int morph_mode,

    int enable_gray,

    int enable_gauss,

    int out_sel,

    hls::stream<axis_in_t>& src_stream,

    hls::stream<axis_out_t>& dst_stream

) {

    compute_expected(

        w, h, mode, threshold, morph_mode,

        enable_gray, enable_gauss, out_sel

    );

    return check_frame(

        label, frame_id,

        w, h,

        mode, threshold, morph_mode,

        enable_gray, enable_gauss, out_sel,

        src_stream, dst_stream

    );

}

// -------------------------------------------------------------

// 帧清单见文件头。C 仿真 1 轮全量;RTL cosim 缩减子集。

// -------------------------------------------------------------

int main(void) {

    // RGB2Gray 公式口径自检(锁死 77/150/29 + round half up;

    // 与 register_map 规格一致,防参考实现漂移)。

    if (ref_y(pack_rgb(200, 100, 50)) != 124 ||

        ref_y(pack_rgb(255, 0, 0)) != 77 ||

        ref_y(pack_rgb(0, 255, 0)) != 149 ||

        ref_y(pack_rgb(0, 0, 255)) != 29) {

        printf(

            "FAIL: RGB2Gray 参考公式与规格不符:"

            " got %u/%u/%u/%u, expected 124/77/149/29\n",

            ref_y(pack_rgb(200, 100, 50)),

            ref_y(pack_rgb(255, 0, 0)),

            ref_y(pack_rgb(0, 255, 0)),

            ref_y(pack_rgb(0, 0, 255))

        );

        return 1;

    }

    hls::stream<axis_in_t> src_stream("src_stream");

    hls::stream<axis_out_t> dst_stream("dst_stream");

    int frame_id = 0;

    int frames = 0;

    const bool cosim = in_rtl_cosim();

    // ---- A. V4 回归:enable_gray=0 + enable_gauss=0 + morph=0 ----

    // 灰阶编码输入低 8bit 直通,输出必须与 sobel_v4 逐像素一致。

    static const int synth_cases[][5] = {

        {320, 240, 1001, 0, 0},

        {320, 240, 1001, 1, 0},

        {320, 240, 1001, 1, 120},

        {640, 480, 2002, 0, 0},

        {640, 480, 2002, 0, 500},

        {255, 255, 3003, 0, 0},

        {255, 255, 3003, 1, 0},

        {255, 255, 3003, 0, 255}

    };

    // cosim:只留 3 帧(320×240 / 640×480 / 255×255 各一,(0,0))

    static const int synth_keep[] = {0, 3, 5};

    const int synth_count = cosim ? 3 : 8;

    for (int si = 0; si < synth_count; ++si) {

        const int i = cosim ? synth_keep[si] : si;

        ++frame_id;

        const int w = synth_cases[i][0];

        const int h = synth_cases[i][1];

        const int mode = synth_cases[i][3];

        const int threshold = synth_cases[i][4];

        make_synthetic(g_gray, w, h, (uint32_t)synth_cases[i][2]);

        pack_gray_to_rgb(w, h);

        char label[64];

        snprintf(

            label, sizeof(label),

            "v4regress_synth_%dx%d_m%d_t%d",

            w, h, mode, threshold

        );

        if (run_frame(

                label, frame_id,

                w, h, mode, threshold,

                0,      // morph_mode

                0, 0,   // enable_gray=0, enable_gauss=0

                0,      // out_sel=0

                src_stream, dst_stream

            ) != 0) {

            printf("\nFAILED at frame %d\n", frame_id);

            return 1;

        }

        ++frames;

    }

    // ---- B. V4 回归形态学:6 图 × {256,640} 宽 × {Erode,Dilate} ----

    static const char* morph_patterns[] = {

        "all0", "all255", "center_dot", "center_3x3", "border_blob", "rand_bin"

    };

    static const int morph_sizes[][2] = {

        {256, 256},

        {640, 480}

    };

    for (int p = 0; p < 6; ++p) {

        for (int s = 0; s < 2; ++s) {

            // cosim:只跑 256×256 的中心单点/贴边目标。

            if (cosim && !(s == 0 && (p == 2 || p == 4))) {

                continue;

            }

            const int w = morph_sizes[s][0];

            const int h = morph_sizes[s][1];

            for (int m = 1; m <= 2; ++m) {

                ++frame_id;

                make_pattern(g_gray, w, h, p, 4000u + (uint32_t)p);

                pack_gray_to_rgb(w, h);

                char label[96];

                snprintf(

                    label, sizeof(label),

                    "v4regress_morph_%s_%dx%d_m%d",

                    morph_patterns[p], w, h, m

                );

                if (run_frame(

                        label, frame_id,

                        w, h, 0, 80, m,

                        0, 0, 0,

                        src_stream, dst_stream

                    ) != 0) {

                    printf("\nFAILED at frame %d\n", frame_id);

                    return 1;

                }

                ++frames;

            }

        }

    }

    // 随机灰度 × {Erode, Dilate}:thr=0(灰度 min/max 语义)。

    for (int m = 1; m <= 2; ++m) {

        ++frame_id;

        make_pattern(g_gray, 320, 240, 6, 5000u + (uint32_t)m);

        pack_gray_to_rgb(320, 240);

        char label[64];

        snprintf(

            label, sizeof(label),

            "v4regress_morph_randgray_320x240_m%d", m

        );

        if (run_frame(

                label, frame_id,

                320, 240, 0, 0, m,

                0, 0, 0,

                src_stream, dst_stream

            ) != 0) {

            printf("\nFAILED at frame %d\n", frame_id);

            return 1;

        }

        ++frames;

    }

    // ---- C. RGB 全链:enable_gray=1 + enable_gauss=1 ----

    // C1:纯色 (200,100,50) → gray 124 → gauss 124 → sobel 0。

    {

        ++frame_id;

        make_rgb_solid(256, 256, 200, 100, 50);

        if (run_frame(

                "rgb_solid_200_100_50_thr0", frame_id,

                256, 256, 0, 0, 0,

                1, 1, 0,

                src_stream, dst_stream

            ) != 0) {

            printf("\nFAILED at frame %d\n", frame_id);

            return 1;

        }

        ++frames;

    }

    // C2:纯色二值化(全 0 边,thr=80 输出全 0)。

    if (!cosim) {

        ++frame_id;

        make_rgb_solid(64, 64, 200, 100, 50);

        if (run_frame(

                "rgb_solid_200_100_50_thr80", frame_id,

                64, 64, 0, 80, 0,

                1, 1, 0,

                src_stream, dst_stream

            ) != 0) {

            printf("\nFAILED at frame %d\n", frame_id);

            return 1;

        }

        ++frames;

    }

    // C3:三基色纯色,out_sel=1 导出 gray 全帧 = 常数(无环0)。

    {

        static const uint8_t rgb3[3][3] = {

            {255, 0, 0},

            {0, 255, 0},

            {0, 0, 255}

        };

        static const char* names[3] = {"red", "green", "blue"};

        for (int k = 0; k < 3; ++k) {

            ++frame_id;

            make_rgb_solid(64, 64, rgb3[k][0], rgb3[k][1], rgb3[k][2]);

            char label[64];

            snprintf(label, sizeof(label), "rgb_primary_%s_out1", names[k]);

            if (run_frame(

                    label, frame_id,

                    64, 64, 0, 0, 0,

                    1, 1, 1,

                    src_stream, dst_stream

                ) != 0) {

                printf("\nFAILED at frame %d\n", frame_id);

                return 1;

            }

            ++frames;

        }

    }

    // C4:棋盘格(16px 块)× thr=80,全链。

    if (!cosim) {

        ++frame_id;

        make_rgb_checker(256, 256, 16);

        if (run_frame(

                "rgb_checker_256_thr80", frame_id,

                256, 256, 0, 80, 0,

                1, 1, 0,

                src_stream, dst_stream

            ) != 0) {

            printf("\nFAILED at frame %d\n", frame_id);

            return 1;

        }

        ++frames;

    }

    // C5:边界亮点 —— 贴边 8×8 白块(压最外圈,测各级边界零填充)。

    {

        ++frame_id;

        memset(g_gray, 0, (size_t)128 * 128);

        for (int r = 0; r < 8; ++r)

            for (int c = 0; c < 8; ++c)

                g_gray[r * 128 + c] = 255;

        pack_gray_to_rgb(128, 128);

        if (run_frame(

                "rgb_border_blob_128_thr80", frame_id,

                128, 128, 0, 80, 0,

                1, 1, 0,

                src_stream, dst_stream

            ) != 0) {

            printf("\nFAILED at frame %d\n", frame_id);

            return 1;

        }

        ++frames;

    }

    // C6:中心单点(1 像素亮点穿过三级窗口)。

    {

        ++frame_id;

        memset(g_gray, 0, (size_t)128 * 128);

        g_gray[64 * 128 + 64] = 255;

        pack_gray_to_rgb(128, 128);

        if (run_frame(

                "rgb_center_dot_128_thr0", frame_id,

                128, 128, 0, 0, 0,

                1, 1, 0,

                src_stream, dst_stream

            ) != 0) {

            printf("\nFAILED at frame %d\n", frame_id);

            return 1;

        }

        ++frames;

    }

    // C7:RGB 噪声,thr=0 饱和 + 形态学。

    {

        static const int morph_m = cosim ? 1 : 2;

        for (int m = 1; m <= morph_m; ++m) {

            ++frame_id;

            make_rgb_noise(320, 240, 7000u + (uint32_t)m);

            char label[64];

            snprintf(label, sizeof(label), "rgb_noise_320x240_m%d", m);

            if (run_frame(

                    label, frame_id,

                    320, 240, 0, 0, m,

                    1, 1, 0,

                    src_stream, dst_stream

                ) != 0) {

                printf("\nFAILED at frame %d\n", frame_id);

                return 1;

            }

            ++frames;

        }

    }

    // C8:随机 RGB × 三档尺寸,thr=80。

    {

        static const int sizes[][2] = {

            {256, 256},

            {320, 240},

            {640, 480}

        };

        // cosim:只跑 320×240。

        for (int s = 0; s < 3; ++s) {

            if (cosim && s != 1) {

                continue;

            }

            ++frame_id;

            const int w = sizes[s][0];

            const int h = sizes[s][1];

            make_rgb_noise(w, h, 8000u + (uint32_t)s);

            char label[64];

            snprintf(label, sizeof(label), "rgb_rand_%dx%d_thr80", w, h);

            if (run_frame(

                    label, frame_id,

                    w, h, 0, 80, 0,

                    1, 1, 0,

                    src_stream, dst_stream

                ) != 0) {

                printf("\nFAILED at frame %d\n", frame_id);

                return 1;

            }

            ++frames;

        }

    }

    // ---- D. 组合矩阵:enable_gray × enable_gauss × morph_mode ----

    // 同一张随机图;enable_gray=0 行用灰阶编码(低 8bit = g),

    // enable_gray=1 行用真 RGB;灰阶编码输入在两条路径下

    // gray 图相同 → (0,·,·) 与 (1,·,·) 行输出必须逐像素一致。

    {

        static const int combos[][3] = {

            {1, 1, 0}, {1, 1, 1}, {1, 1, 2},

            {1, 0, 0}, {1, 0, 1}, {1, 0, 2},

            {0, 1, 0}, {0, 1, 1}, {0, 1, 2},

            {0, 0, 0}, {0, 0, 1}, {0, 0, 2}

        };

        // cosim:只跑 (1,1,0)、(1,0,1)、(0,1,2)、(0,0,0)。

        static const int combo_keep[] = {0, 4, 8, 9};

        const int combo_count = cosim ? 4 : 12;

        make_pattern(g_gray, 320, 240, 6, 9000u);

        pack_gray_to_rgb(320, 240);

        static uint32_t gray_encoded[MAX_PX];

        memcpy(gray_encoded, g_rgb, sizeof(uint32_t) * 320 * 240);

        make_rgb_noise(320, 240, 9001u);

        static uint32_t rgb_words[MAX_PX];

        memcpy(rgb_words, g_rgb, sizeof(uint32_t) * 320 * 240);

        for (int ci = 0; ci < combo_count; ++ci) {

            const int k = cosim ? combo_keep[ci] : ci;

            ++frame_id;

            const int eg = combos[k][0];

            const int egs = combos[k][1];

            const int mm = combos[k][2];

            // enable_gray=1 用真 RGB,enable_gray=0 用灰阶编码。

            memcpy(

                g_rgb,

                (eg != 0) ? rgb_words : gray_encoded,

                sizeof(uint32_t) * 320 * 240

            );

            char label[64];

            snprintf(

                label, sizeof(label),

                "combo_g%d_s%d_m%d_320x240", eg, egs, mm

            );

            if (run_frame(

                    label, frame_id,

                    320, 240, 0, 80, mm,

                    eg, egs, 0,

                    src_stream, dst_stream

                ) != 0) {

                printf("\nFAILED at frame %d\n", frame_id);

                return 1;

            }

            ++frames;

        }

    }

    // ---- E. out_sel 导出:{0,1,2,3} × {gauss 开/旁路} 全组合 ----

    // 覆盖输出 mux 与各级延迟;enable_gray 前 4 帧真 RGB,

    // 后 4 帧灰阶编码(旁路导出 = 原始 gray 环0)。

    {

        static const int export_sel[] = {0, 1, 2, 3};

        static const int export_gauss[] = {1, 1, 0, 0};

        // cosim:只跑全链 out_sel=2 与旁路 out_sel=1。

        static const int export_keep[] = {2, 5};

        const int export_count = cosim ? 2 : 8;

        make_rgb_noise(256, 256, 9500u);

        static uint32_t exp_rgb[MAX_PX];

        memcpy(exp_rgb, g_rgb, sizeof(uint32_t) * 256 * 256);

        make_pattern(g_gray, 256, 256, 6, 9501u);

        pack_gray_to_rgb(256, 256);

        static uint32_t exp_gray[MAX_PX];

        memcpy(exp_gray, g_rgb, sizeof(uint32_t) * 256 * 256);

        for (int ei = 0; ei < export_count; ++ei) {

            const int k = cosim ? export_keep[ei] : ei;

            ++frame_id;

            const int os = export_sel[k % 4];

            const int egs = export_gauss[k % 4];

            const int eg = (k < 4) ? 1 : 0;   // 前4帧全链真RGB,后4帧旁路灰阶

            memcpy(

                g_rgb,

                (eg != 0) ? exp_rgb : exp_gray,

                sizeof(uint32_t) * 256 * 256

            );

            char label[64];

            snprintf(

                label, sizeof(label),

                "export_g%d_s%d_out%d_256", eg, egs, os

            );

            if (run_frame(

                    label, frame_id,

                    256, 256, 0, 80, 1,

                    eg, egs, os,

                    src_stream, dst_stream

                ) != 0) {

                printf("\nFAILED at frame %d\n", frame_id);

                return 1;

            }

            ++frames;

        }

    }

    printf(

        "\nALL PASS: %d frames\n",

        frames

    );

    return 0;

}

