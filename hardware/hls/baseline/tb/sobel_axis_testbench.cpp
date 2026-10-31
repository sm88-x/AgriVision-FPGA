// =============================================================
// Sobel AXI4-Stream testbench（仅用于 C Simulation）
//
// 配套顶层：sobel_axis
// 输入文件：data/images/{name}.bin
//           256×256 uint8，65536字节
//
// Golden：data/golden/{name}_edge_hw.bin
//         沿用旧版254×254内部像素结果，64516字节。
//         比较时将其映射到256×256图像的内部区域；
//         最外一圈期望为0，不修改原Golden文件。
//
// 检查：
//   1. 输入全部被消费
//   2. 输出恰好65536个像素
//   3. 像素与Golden完全一致
//   4. 仅最后一个输出包TLAST=1
//   5. 每个输出包TKEEP=1、TSTRB=1
// =============================================================

#include <stdio.h>
#include <stdint.h>

#include <ap_axi_sdata.h>
#include <hls_stream.h>

typedef ap_axiu<8, 0, 0, 0> axis_pixel_t;

// 【修改】声明必须与设计源码完全一致，包括引用符号 &。
// 不要在这里#include设计源码的.cpp文件。
extern void sobel_axis(
    hls::stream<axis_pixel_t>& src,
    hls::stream<axis_pixel_t>& dst
);

static const int W = 256;
static const int H = 256;
static const int N = W * H;

static const int GW = W - 2;
static const int GH = H - 2;
static const int GN = GW * GH;

// 从当前目录向上查找仓库数据文件。
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

    char path[1024];

    const int count =
        (int)(sizeof(prefixes) / sizeof(prefixes[0]));

    for (int i = 0; i < count; ++i) {
        snprintf(path, sizeof(path), "%s%s",
                 prefixes[i], relpath);

        FILE* f = fopen(path, "rb");
        if (f != NULL) {
            printf("  [open] %s\n", path);
            return f;
        }
    }

    return NULL;
}

// 【新增】严格检查文件长度，防止把错误尺寸的数据用于对拍。
static bool read_exact_file(
    const char* relpath,
    uint8_t* buffer,
    size_t expected_bytes
) {
    FILE* f = open_repo_file(relpath);

    if (f == NULL) {
        printf("FAIL: cannot open %s\n", relpath);
        return false;
    }

    const size_t actual =
        fread(buffer, 1, expected_bytes, f);

    if (actual != expected_bytes) {
        printf("FAIL: %s: expected %lu bytes, read %lu\n",
               relpath,
               (unsigned long)expected_bytes,
               (unsigned long)actual);
        fclose(f);
        return false;
    }

    const int extra = fgetc(f);
    const bool read_error = (ferror(f) != 0);
    fclose(f);

    if (read_error) {
        printf("FAIL: read error: %s\n", relpath);
        return false;
    }

    if (extra != EOF) {
        printf("FAIL: %s is larger than %lu bytes\n",
               relpath, (unsigned long)expected_bytes);
        return false;
    }

    return true;
}

static bool check_image(
    const char* name,
    hls::stream<axis_pixel_t>& src,
    hls::stream<axis_pixel_t>& dst
) {
    static uint8_t input[N];
    static uint8_t golden[GN];

    char path[1024];

    printf("\n========== %s ==========\n", name);

    // 同一对流跨帧复用，检查前一帧是否有残留。
    if (!src.empty() || !dst.empty()) {
        printf("FAIL: streams are not empty before frame\n");
        return false;
    }

    snprintf(path, sizeof(path),
             "data/fixtures/images/%s.bin", name);

    if (!read_exact_file(path, input, N)) {
        return false;
    }

    snprintf(path, sizeof(path),
             "data/fixtures/golden/%s_edge_hw.bin", name);

    if (!read_exact_file(path, golden, GN)) {
        return false;
    }

    // 【修改】把输入数组封装为AXIS数据包。
    for (int i = 0; i < N; ++i) {
        axis_pixel_t packet;

        packet.data = input[i];
        packet.keep = 1;
        packet.strb = 1;
        packet.last = (i == N - 1) ? 1 : 0;

        src.write(packet);
    }

    // 【修改】调用流式顶层，不再调用sobel_kernel。
    // C仿真中一次函数调用处理一帧。
    sobel_axis(src, dst);

    // 输入必须全部被消费。
    if (!src.empty()) {
        printf("FAIL: input stream contains unread pixels\n");
        return false;
    }

    int different_pixels = 0;
    int protocol_errors = 0;
    int max_error = 0;
    unsigned long long sum_error = 0;
    int last_count = 0;

    for (int i = 0; i < N; ++i) {
        // 先判断empty，避免测试程序读取不存在的数据。
        if (dst.empty()) {
            printf("FAIL: output too short: "
                   "expected %d pixels, received %d\n", N, i);
            return false;
        }

        axis_pixel_t packet = dst.read();

        const int r = i / W;
        const int c = i % W;

        // 【修改】将旧254×254 Golden映射到256×256内部。
        uint8_t expected = 0;

        if (r > 0 && r < H - 1 &&
            c > 0 && c < W - 1) {
            expected = golden[(r - 1) * GW + (c - 1)];
        }

        const uint8_t actual = (uint8_t)packet.data;

        int error = (int)actual - (int)expected;
        if (error < 0) {
            error = -error;
        }

        sum_error += (unsigned long long)error;

        if (error > max_error) {
            max_error = error;
        }

        if (error != 0) {
            ++different_pixels;

            if (different_pixels <= 5) {
                printf("  PIXEL MISMATCH (%d,%d): "
                       "got=%u expected=%u\n",
                       r, c,
                       (unsigned int)actual,
                       (unsigned int)expected);
            }
        }

        // 【新增】TLAST只能出现在整帧最后一个像素。
        const bool actual_last = (packet.last != 0);
        const bool expected_last = (i == N - 1);

        if (actual_last) {
            ++last_count;
        }

        if (actual_last != expected_last) {
            ++protocol_errors;

            if (protocol_errors <= 5) {
                printf("  TLAST MISMATCH idx=%d: "
                       "got=%d expected=%d\n",
                       i, (int)actual_last, (int)expected_last);
            }
        }

        // 【新增】8-bit数据流，每拍一个有效字节。
        if (packet.keep != 1 || packet.strb != 1) {
            ++protocol_errors;

            if (protocol_errors <= 5) {
                printf("  KEEP/STRB ERROR idx=%d: "
                       "keep=%u strb=%u\n",
                       i,
                       (unsigned int)packet.keep,
                       (unsigned int)packet.strb);
            }
        }
    }

    // 输出不能超过一帧。
    if (!dst.empty()) {
        printf("FAIL: output contains more than %d pixels\n", N);
        return false;
    }

    const double mean_error =
        (double)sum_error / (double)N;

    printf("  total_pixels     = %d\n", N);
    printf("  different_pixels = %d\n", different_pixels);
    printf("  max_error        = %d\n", max_error);
    printf("  mean_error       = %.8f\n", mean_error);
    printf("  protocol_errors  = %d\n", protocol_errors);
    printf("  TLAST_count      = %d\n", last_count);

    const bool pass =
        different_pixels == 0 &&
        protocol_errors == 0 &&
        last_count == 1;

    printf("  %s: %s\n", name, pass ? "PASS" : "FAIL");

    return pass;
}

int main() {
    const char* images[] = {
        "airfield_gray_256x256",
        "clock_gray_256x256",
        "plane_gray_256x256",
        "plant_a_256x256",
        "plant_b_256x256"
    };

    const int image_count =
        (int)(sizeof(images) / sizeof(images[0]));

    // 【新增】跨帧复用流对象，检查连续多帧调用。
    hls::stream<axis_pixel_t> src;
    hls::stream<axis_pixel_t> dst;

    for (int i = 0; i < image_count; ++i) {
        if (!check_image(images[i], src, dst)) {
            printf("\nFAILED at image: %s\n", images[i]);
            return 1;
        }
    }

    printf("\nALL PASS: %d frames, "
           "pixels and AXIS sidebands verified\n",
           image_count);

    return 0;
}