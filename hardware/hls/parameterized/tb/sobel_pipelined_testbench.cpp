// =============================================================
// Sobel AXI4-Stream V3 testbench(edge_validation 参数化 + 双算法版,C Simulation / Cosim)
//
// 帧清单(每轮):
//   - 256×256 ×3 张真实图 ×4 组合:
//       (mode 0, thr 0)   对拍文件 golden(独立对拍,保持回归)
//       (mode 0, thr 80)  Sobel 二值化
//       (mode 1, thr 0)   Prewitt 饱和
//       (mode 1, thr 80)  Prewitt 二值化
//   - 合成图(确定性伪随机,对拍 tb 内参考):
//       320×240 ×3: (0,0) 饱和回归、(1,0)、(1,120)
//       640×480 ×2: (0,0) 饱和回归、(0,500) 高阈值二值化
//       255×255 ×3: (0,0)、(1,0)、(0,255) 阈值=饱和点边界(奇数宽抓行切换)
//
// 每帧检查:
//   输出拍数 == width*height、TLAST 仅帧尾一次、keep/strb == 1、
//   逐像素 == golden、最外一圈全 0、输入拍全部被消费。
//
// 口径(mode/threshold,与 B 侧一致):
//   mode:0=Sobel,1=Prewitt。
//   threshold:0=饱和输出;>0=二值化,梯度>=threshold 输出255,否则0。
// 注意:本文件用于 C 仿真与 RTL cosim;RTL 级背压需上板验证。
// =============================================================

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include <ap_axi_sdata.h>
#include <hls_stream.h>

typedef ap_axiu<8, 0, 0, 0> axis_pixel_t;

extern void sobel_v3(
    hls::stream<axis_pixel_t>& src,
    hls::stream<axis_pixel_t>& dst,
    int width,
    int height,
    int mode,
    int threshold
);

static const int MAX_W = 640;
static const int MAX_H = 480;
static const int MAX_PX = MAX_W * MAX_H;   // 307200

static const int ROUNDS = 2;
static const int MAX_ERROR_PRINTS = 10;

// 帧缓冲放静态区,避免深栈(大尺寸帧会爆栈)。
static uint8_t g_src[MAX_PX];
static uint8_t g_golden[MAX_PX];

// -------------------------------------------------------------
// 向上查找仓库文件。
// -------------------------------------------------------------
static FILE* open_repo_file(const char* relpath) {
    static const char* prefixes[] = {
        "./",
        "../",
        "../../",
        "../../../",
        "../../../../",
        "../../../../../",
        "../../../../../../",
        "../../../../../../../"
    };

    char path[512];
    for (int i = 0; i < 8; ++i) {
        snprintf(path, sizeof(path), "%s%s", prefixes[i], relpath);
        FILE* f = fopen(path, "rb");
        if (f) {
            printf("  [open] %s\n", path);
            return f;
        }
    }
    return NULL;
}

// -------------------------------------------------------------
// 严格检查文件长度,不静默接受多余数据。
// 返回读取的字节数;失败返回-1。
// -------------------------------------------------------------
static int read_binary(const char* path, uint8_t* data, int capacity) {
    FILE* f = open_repo_file(path);
    if (!f) {
        printf("FAIL: cannot open %s\n", path);
        return -1;
    }
    size_t bytes = fread(data, 1, (size_t)capacity, f);

    // 如果已读满capacity,再试读一个字节以检查超长。
    int extra = fgetc(f);
    int io_error = ferror(f);
    fclose(f);
    if (io_error || extra != EOF) {
        printf("FAIL: read error or oversized file: %s\n", path);
        return -1;
    }
    return (int)bytes;
}

// -------------------------------------------------------------
// 两种 Golden 格式统一为 w×h:
// 旧版 golden[r][c] 映射到新版 golden[r+1][c+1]。
// -------------------------------------------------------------
static int load_golden(const char* name, int w, int h) {
    uint8_t raw[MAX_PX];
    char path[256];
    snprintf(path, sizeof(path), "data/fixtures/golden/%s_edge_hw.bin", name);
    int bytes = read_binary(path, raw, MAX_PX);
    if (bytes < 0) return -1;

    const int pixels = w * h;
    const int ow = w - 2;
    const int oh = h - 2;

    if (bytes == ow * oh) {
        memset(g_golden, 0, pixels);
        for (int r = 0; r < oh; ++r)
            for (int c = 0; c < ow; ++c)
                g_golden[(r + 1) * w + c + 1] = raw[r * ow + c];
        printf("  Golden: %dx%d padded to %dx%d\n", ow, oh, w, h);
    } else if (bytes == pixels) {
        memcpy(g_golden, raw, pixels);
        printf("  Golden: %dx%d\n", w, h);
    } else {
        printf("FAIL: golden size=%d, expected %d or %d\n",
               bytes, ow * oh, pixels);
        return -1;
    }

    for (int r = 0; r < h; ++r) {
        for (int c = 0; c < w; ++c) {
            if ((r == 0 || r == h - 1 || c == 0 || c == w - 1)
                && g_golden[r * w + c] != 0) {
                printf("FAIL: nonzero golden border at (%d,%d)\n", r, c);
                return -1;
            }
        }
    }
    return 0;
}

// -------------------------------------------------------------
// 参考梯度(|Gx|+|Gy| 原值,不做饱和/二值化),
// 与 B 侧 Python 参考同口径。仅对内部像素。
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
// 由输入图现算 golden(按 mode/threshold 口径)。
// -------------------------------------------------------------
static void make_golden_from_img(
    const uint8_t* img,
    int w,
    int h,
    int mode,
    int threshold
) {
    memset(g_golden, 0, w * h);
    for (int r = 1; r < h - 1; ++r) {
        for (int c = 1; c < w - 1; ++c) {
            const uint16_t mag = (mode == 1)
                ? ref_prewitt_mag(img, w, r, c)
                : ref_sobel_mag(img, w, r, c);

            if (threshold > 0) {
                g_golden[r * w + c] =
                    (mag >= (uint16_t)threshold) ? 255 : 0;
            } else {
                g_golden[r * w + c] =
                    (mag > 255) ? 255 : (uint8_t)mag;
            }
        }
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
// 单帧测试:g_src 已填好输入,g_golden 已填好期望。
// 返回0表示PASS,非0表示FAIL。
// -------------------------------------------------------------
static int check_frame(
    const char* label,
    int frame_id,
    int w,
    int h,
    int mode,
    int threshold,
    hls::stream<axis_pixel_t>& src_stream,
    hls::stream<axis_pixel_t>& dst_stream
) {
    const int pixels = w * h;

    printf(
        "\n[Frame %d] %s (%dx%d, mode=%d, thr=%d)\n",
        frame_id, label, w, h, mode, threshold
    );

    // 上一帧不能留下未消费的输入或输出。
    if (!src_stream.empty() || !dst_stream.empty()) {
        printf("FAIL: streams are not empty before frame\n");
        return 1;
    }

    // 将整帧输入打包为 AXI Stream 数据。
    // C Simulation中stream队列可容纳整帧。
    for (int i = 0; i < pixels; ++i) {
        axis_pixel_t packet;

        packet.data = g_src[i];
        packet.keep = 1;
        packet.strb = 1;
        packet.last = (i == pixels - 1) ? 1 : 0;

        src_stream.write(packet);
    }

    // C Simulation中每次调用处理一帧。
    sobel_v3(src_stream, dst_stream, w, h, mode, threshold);

    int printed = 0;
    int pixel_errors = 0;
    int protocol_errors = 0;
    int border_errors = 0;
    int output_count = 0;
    int last_count = 0;
    int max_error = 0;
    unsigned long total_error = 0;

    // 先检查empty再read,避免输出不足时
    // testbench自身执行阻塞读。
    while (!dst_stream.empty()) {
        axis_pixel_t packet = dst_stream.read();

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

        const bool border =
            r == 0 || r == h - 1 ||
            c == 0 || c == w - 1;

        if (border && value != 0) {
            ++border_errors;
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
// 帧清单:
//   真实图 × {mode,thr} 组合 + 合成图多尺寸多组合,连续两轮。
// -------------------------------------------------------------
int main(void) {
    static const char* real_images[] = {
        "airfield_gray_256x256",
        "clock_gray_256x256",
        "plane_gray_256x256"
    };

    // 真实图组合:{mode, threshold}。
    // (0,0) 用独立文件 golden 对拍,其余组合 golden 现算。
    static const int real_combos[][2] = {
        {0, 0},
        {0, 80},
        {1, 0},
        {1, 80}
    };

    // 合成图规格:{宽, 高, 种子, mode, threshold}
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

    hls::stream<axis_pixel_t> src_stream("src_stream");
    hls::stream<axis_pixel_t> dst_stream("dst_stream");

    int frame_id = 0;
    int frames = 0;

    for (int round = 0; round < ROUNDS; ++round) {
        printf(
            "\n========== Round %d/%d ==========\n",
            round + 1,
            ROUNDS
        );

        // 真实图 256×256 × 组合。
        for (int i = 0; i < 3; ++i) {
            char path[256];
            snprintf(path, sizeof(path), "data/fixtures/images/%s.bin", real_images[i]);

            int bytes = read_binary(path, g_src, 256 * 256);
            if (bytes != 256 * 256) {
                printf(
                    "FAILED at frame %d (input %s)\n",
                    frame_id + 1,
                    path
                );
                return 1;
            }

            for (int k = 0; k < 4; ++k) {
                ++frame_id;

                const int mode = real_combos[k][0];
                const int threshold = real_combos[k][1];

                char label[96];
                snprintf(
                    label,
                    sizeof(label),
                    "%s_m%d_t%d",
                    real_images[i],
                    mode,
                    threshold
                );

                // (0,0) 用独立文件 golden 回归,其余现算。
                if (mode == 0 && threshold == 0) {
                    if (load_golden(real_images[i], 256, 256) != 0) {
                        return 1;
                    }
                } else {
                    make_golden_from_img(g_src, 256, 256, mode, threshold);
                }

                if (check_frame(
                        label,
                        frame_id,
                        256, 256,
                        mode, threshold,
                        src_stream,
                        dst_stream
                    ) != 0) {
                    printf("\nFAILED at frame %d\n", frame_id);
                    return 1;
                }
                ++frames;
            }
        }

        // 合成图:多尺寸 × 组合,每轮换种子。
        for (int i = 0; i < 8; ++i) {
            ++frame_id;

            const int w = synth_cases[i][0];
            const int h = synth_cases[i][1];
            const int mode = synth_cases[i][3];
            const int threshold = synth_cases[i][4];
            const uint32_t seed =
                (uint32_t)synth_cases[i][2] * (uint32_t)(round + 1);

            char label[64];
            snprintf(
                label,
                sizeof(label),
                "synth_%dx%d_s%u_m%d_t%d",
                w, h, seed, mode, threshold
            );

            make_synthetic(g_src, w, h, seed);
            make_golden_from_img(g_src, w, h, mode, threshold);

            if (check_frame(
                    label,
                    frame_id,
                    w, h,
                    mode, threshold,
                    src_stream,
                    dst_stream
                ) != 0) {
                printf("\nFAILED at frame %d\n", frame_id);
                return 1;
            }
            ++frames;
        }
    }

    printf(
        "\nALL PASS: %d frames across %d rounds\n",
        frames,
        ROUNDS
    );

    return 0;
}
