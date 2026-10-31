#include <ap_axi_sdata.h>

#include <hls_stream.h>

#include <cstdio>

#include <cstdlib>

typedef ap_axiu<32, 0, 0, 0> axis_in_t;

typedef ap_axiu<8, 0, 0, 0> axis_out_t;

void sobel_v4_baseline(

    hls::stream<axis_in_t>&,

    hls::stream<axis_out_t>&,

    int, int, int, int, int, int, int, int);

void sobel_v4_pipeline(

    hls::stream<axis_in_t>&,

    hls::stream<axis_out_t>&,

    int, int, int, int, int, int, int, int,

    int, int, int, int,

    unsigned int&, unsigned int&);

static void check(bool good, const char *message) {

    if (!good) {

        std::fprintf(stderr, "FAIL: %s\n", message);

        std::exit(1);

    }

}

int main() {

    const int sizes[3][2] = {{256,256}, {320,240}, {640,480}};

    int tests = 0;

    for (int s = 0; s < 3; ++s) {

        int w = sizes[s][0], h = sizes[s][1];

        int pixels = w * h;

        for (int cfg = 0; cfg < 12; ++cfg) {

            // 连续测试非零帧和全零帧，检查帧间清零。

            for (int pattern = 0; pattern < 2; ++pattern) {

                hls::stream<axis_in_t> a, b;

                hls::stream<axis_out_t> old_out, new_out;

                unsigned int state = 123u;

                for (int i = 0; i < pixels; ++i) {

                    state = 1664525u * state + 1013904223u;

                    axis_in_t p;

                    p.data = pattern == 0 ? state : 0u;

                    p.keep = 15;

                    p.strb = 15;

                    p.last = i == pixels - 1;

                    a.write(p);

                    b.write(p);

                }

                int mode = cfg % 2;

                int threshold = cfg % 3 == 0 ? 0 : 40;

                int morph = cfg % 3;

                int gray = (cfg / 2) % 2;

                int gauss = (cfg / 3) % 2;

                int sel = cfg % 4;

                // 覆盖全帧、内部 ROI、空 ROI。

                int x0 = cfg % 3 == 0 ? 0 : w / 4;

                int y0 = cfg % 3 == 0 ? 0 : h / 4;

                int x1 = cfg % 3 == 0 ? w : 3 * w / 4;

                int y1 = cfg % 3 == 0 ? h : 3 * h / 4;

                if (cfg % 3 == 2) x1 = x0;

                unsigned int fg = 0xdeadbeefu;

                unsigned int roi = 0xdeadbeefu;

                sobel_v4_baseline(

                    a, old_out, w, h, mode, threshold,

                    morph, gray, gauss, sel);

                sobel_v4_pipeline(

                    b, new_out, w, h, mode, threshold,

                    morph, gray, gauss, sel,

                    x0, y0, x1, y1, fg, roi);

                unsigned int expected_fg = 0;

                unsigned int expected_roi = 0;

                for (int i = 0; i < pixels; ++i) {

                    check(!old_out.empty() && !new_out.empty(),

                          "too few output beats");

                    axis_out_t p = old_out.read();

                    axis_out_t q = new_out.read();

                    check(p.data == q.data, "output pixel changed");

                    check(q.keep == 1 && q.strb == 1,

                          "invalid output byte enables");

                    check((q.last != 0) == (i == pixels - 1),

                          "invalid TLAST");

                    int x = i % w, y = i / w;

                    if (q.data != 0) {

                        ++expected_fg;

                        if (x >= x0 && x < x1 && y >= y0 && y < y1)

                            ++expected_roi;

                    }

                }

                check(a.empty() && b.empty(), "input not consumed");

                check(old_out.empty() && new_out.empty(),

                      "too many output beats");

                check(fg == expected_fg, "foreground count mismatch");

                check(roi == expected_roi, "ROI count mismatch");

                ++tests;

            }

        }

    }

    std::printf("PASS: %d frame tests\n", tests);

    return 0;

}