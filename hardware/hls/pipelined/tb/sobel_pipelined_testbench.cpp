// =============================================================
// Sobel AXI4-Stream V3 testbench（C Simulation）
//
// 输入：
//   data/images/{name}.bin
//   必须为256×256个uint8像素。
//
// Golden Reference：
//   data/golden/{name}_edge_hw.bin
//
// 注意：
// 本文件用于C Simulation。
// C仿真中的hls::stream不能验证RTL周期级背压行为。
// ap_ctrl_none自主运行、随机TREADY停顿等需RTL验证。
// =============================================================

#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include <ap_axi_sdata.h>
#include <hls_stream.h>

typedef ap_axiu<8, 0, 0, 0> axis_pixel_t;

// 【修改1】与V3顶层函数名称、参数保持一致。
extern void sobel_v3(
    hls::stream<axis_pixel_t>& src,
    hls::stream<axis_pixel_t>& dst
);

static const int W = 256;
static const int H = 256;
static const int PIXELS = W * H;

static const int OW = 254;
static const int OH = 254;
static const int OLD_PIXELS = OW * OH;

static const int ROUNDS = 2;
static const int MAX_ERROR_PRINTS = 10;

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
// 【修改】严格检查文件长度，不静默接受多余数据。
// 返回读取的字节数；失败返回-1。
// -------------------------------------------------------------
static int read_binary(const char* path, uint8_t* data, int capacity) {
    FILE* f = open_repo_file(path);
    if (!f) {
        printf("FAIL: cannot open %s\n", path);
        return -1;
    }
    size_t bytes = fread(data, 1, (size_t)capacity, f);

    // 如果已读满capacity，再试读一个字节以检查超长。
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
// 【修改3】将两种Golden格式统一为256×256。
// 旧版golden[r][c]映射到新版golden[r+1][c+1]。
// -------------------------------------------------------------
static int load_golden(const char* name, uint8_t* golden) {
    uint8_t raw[PIXELS];
    char path[256];
    snprintf(path, sizeof(path), "data/fixtures/golden/%s_edge_hw.bin", name);
    int bytes = read_binary(path, raw, PIXELS);
    if (bytes < 0) return -1;
    if (bytes == OW * OH) {
        memset(golden, 0, PIXELS);
        for (int r = 0; r < OH; ++r)
            for (int c = 0; c < OW; ++c)
                golden[(r + 1) * W + c + 1] = raw[r * OW + c];
        printf("  Golden: 254x254 padded to 256x256\n");
    } else if (bytes == PIXELS) {
        memcpy(golden, raw, PIXELS);
        printf("  Golden: 256x256\n");
    } else {
        printf("FAIL: golden size=%d, expected %d or %d\n",
               bytes, OW * OH, PIXELS);
        return -1;
    }
    for (int r = 0; r < H; ++r) {
        for (int c = 0; c < W; ++c) {
            if ((r == 0 || r == H - 1 || c == 0 || c == W - 1)
                && golden[r * W + c] != 0) {
                printf("FAIL: nonzero golden border at (%d,%d)\n", r, c);
                return -1;
            }
        }
    }
    return 0;
}

// -------------------------------------------------------------
// 单帧测试。
// 输入、输出stream由main创建，所有帧重复使用。
// 返回0表示PASS，非0表示FAIL。
// -------------------------------------------------------------
static int check_image(
    const char* name,
    int frame_id,
    hls::stream<axis_pixel_t>& src_stream,
    hls::stream<axis_pixel_t>& dst_stream
) {
    uint8_t src[PIXELS];
    uint8_t golden[PIXELS];
    char path[256];

    printf("\n[Frame %d] %s\n", frame_id, name);

    // 上一帧不能留下未消费的输入或输出。
    if (!src_stream.empty() || !dst_stream.empty()) {
        printf("FAIL: streams are not empty before frame\n");
        return 1;
    }

    snprintf(
        path,
        sizeof(path),
        "data/fixtures/images/%s.bin",
        name
    );

    int bytes = read_binary(path, src, PIXELS);

    if (bytes != PIXELS) {
        if (bytes >= 0) {
            printf(
                "FAIL: invalid input size %d; expected %d\n",
                bytes,
                PIXELS
            );
        }
        return 1;
    }

    if (load_golden(name, golden) != 0) {
        return 1;
    }

    // 【修改1】将整帧输入打包为AXI Stream数据。
    // C Simulation中stream队列可容纳整帧。
    for (int i = 0; i < PIXELS; ++i) {
        axis_pixel_t packet;

        packet.data = src[i];
        packet.keep = 1;
        packet.strb = 1;
        packet.last = (i == PIXELS - 1) ? 1 : 0;

        src_stream.write(packet);
    }

    // C Simulation中每次调用处理一帧。
    sobel_v3(src_stream, dst_stream);

    int printed = 0;
    int pixel_errors = 0;
    int protocol_errors = 0;
    int border_errors = 0;
    int output_count = 0;
    int last_count = 0;
    int max_error = 0;
    unsigned long total_error = 0;

    // 【修改4】先检查empty再read，避免输出不足时
    // testbench自身执行阻塞读。
    while (!dst_stream.empty()) {
        axis_pixel_t packet = dst_stream.read();

        const int index = output_count++;
        const unsigned value = packet.data.to_uint();
        const bool last = packet.last != 0;
        const bool expected_last = index == PIXELS - 1;

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

        // 多余输出仍然读出并计数，但不访问golden越界位置。
        if (index >= PIXELS) {
            continue;
        }

        const int r = index / W;
        const int c = index % W;
        const unsigned expected = golden[index];

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
            r == 0 || r == H - 1 ||
            c == 0 || c == W - 1;

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

    if (output_count != PIXELS) {
        printf(
            "  FAIL: output count=%d, expected=%d\n",
            output_count,
            PIXELS
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
    if (output_count == PIXELS) {
        printf(
            "  Max error=%d, mean error=%.6f\n",
            max_error,
            (double)total_error / (double)PIXELS
        );
    }

    const bool passed =
        unread_input == 0 &&
        output_count == PIXELS &&
        last_count == 1 &&
        pixel_errors == 0 &&
        protocol_errors == 0 &&
        border_errors == 0;

    printf(
        "  %s: %s\n",
        name,
        passed ? "PASS" : "FAIL"
    );

    return passed ? 0 : 1;
}

// -------------------------------------------------------------
// 【修改5】三张不同图像连续运行两轮。
// 不在帧间创建新的stream，也不插入复位。
// -------------------------------------------------------------
int main(void) {
    static const char* images[] = {
        "airfield_gray_256x256",
        "clock_gray_256x256",
        "plane_gray_256x256"
    };

    const int image_count =
        (int)(sizeof(images) / sizeof(images[0]));

    hls::stream<axis_pixel_t> src_stream("src_stream");
    hls::stream<axis_pixel_t> dst_stream("dst_stream");

    int frame_id = 0;

    for (int round = 0; round < ROUNDS; ++round) {
        printf(
            "\n========== Round %d/%d ==========\n",
            round + 1,
            ROUNDS
        );

        for (int i = 0; i < image_count; ++i) {
            ++frame_id;

            if (check_image(
                    images[i],
                    frame_id,
                    src_stream,
                    dst_stream
                ) != 0) {
                printf(
                    "\nFAILED at frame %d\n",
                    frame_id
                );
                return 1;
            }
        }
    }

    printf(
        "\nALL PASS: %d frames, "
        "%d pixels per frame\n",
        frame_id,
        PIXELS
    );

    return 0;
}