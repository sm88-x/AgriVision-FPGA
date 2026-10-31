
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

# coding: utf-8

# In[1]:

get_ipython().magic('run ./agripheno.ipynb')

import html
import traceback
import ipywidgets as widgets
from IPython.display import display, HTML

# 本单元格只初始化一次。
engine = AgriEngine()

bits = list(Path.cwd().glob("*.bit"))
if len(bits) != 1:
    raise RuntimeError("当前目录应有一套明确的 bit/hwh")

if not bits[0].with_suffix(".hwh").is_file():
    raise RuntimeError("缺少同名 hwh")

engine.connect(bits[0])
print("已加载:", bits[0].name)

# In[2]:

display(HTML("""
<style>
.agri-app {
    background:#f5f6ef;
    border:1px solid #d8e1d2;
    border-radius:18px;
    padding:20px;
    max-width:1320px;
    color:#263e2c;
}
.agri-app .widget-html,
.agri-app .widget-label {
    color:#263e2c !important;
}
.agri-hero {
    background:linear-gradient(115deg,#214d35,#39744d);
    border-radius:14px;
    padding:24px 26px;
    margin-bottom:12px;
}
.agri-hero h2 {
    color:#fff !important;
    margin:0 0 7px;
    font-size:29px;
    font-weight:700;
}
.agri-hero p {
    color:#e0edda;
    margin:0;
    font-size:14px;
}
.agri-section {
    margin:18px 0 9px;
    color:#315e3f;
    font-size:15px;
    font-weight:700;
}
.agri-help {
    color:#60715c;
    font-size:12px;
    line-height:1.85;
    padding:7px 0;
}
.agri-status {
    padding:10px 13px;
    border-radius:9px;
    background:#e8efdf;
    color:#36533a;
    font-size:12px;
}
.agri-grid {
    display:flex;
    flex-wrap:wrap;
    gap:10px;
    margin:12px 0;
}
.agri-card {
    flex:1 1 140px;
    background:#fff;
    border:1px solid #dce5d5;
    border-radius:11px;
    padding:15px;
    border-top:3px solid #789c59;
}
.agri-card small {
    color:#65745e;
    font-size:12px;
}
.agri-card strong {
    display:block;
    color:#285138;
    font-size:23px;
    font-weight:700;
    margin-top:8px;
}
.agri-panel {
    background:#fff;
    border-radius:11px;
}
.agri-panel .widget-html {
    padding-bottom:6px;
}
.agri-table {
    width:100%;
    border-collapse:collapse;
    color:#30442f;
    background:#fff;
}
.agri-table th {
    background:#e8efdf;
    color:#35533a;
    font-weight:600;
}
.agri-table th,.agri-table td {
    padding:10px;
    border-bottom:1px solid #e2e8dc;
    text-align:left;
}
.agri-table tr:nth-child(even) td {
    background:#f8faf5;
}
.agri-app .widget-image {
    object-fit:contain;
}
.agri-app .widget-button {
    border-radius:7px;
}
.agri-app .agri-run {
    background:#39734b !important;
    color:#fff !important;
    font-weight:600;
    border:0;
}
.agri-app .agri-run:disabled {
    background:#a9bba0 !important;
}
.agri-parameters {
    padding:11px 13px;
    margin-top:12px;
    background:#edf2e5;
    border:1px solid #d6e1cc;
    border-radius:9px;
    color:#3d5737;
    line-height:1.7;
}
</style>
"""))

def cards(items):
    return '<div class="agri-grid">' + "".join(
        '<div class="agri-card"><small>{}</small>'
        '<strong>{}</strong></div>'.format(
            html.escape(str(label)),
            html.escape(str(value))
        )
        for label, value in items
    ) + "</div>"

upload = widgets.FileUpload(
    accept=".jpg,.jpeg,.png",
    multiple=False,
    description="上传图片"
)

resolution = widgets.Dropdown(
    options=[
        ("256 × 256", (256, 256)),
        ("320 × 240", (320, 240)),
        ("640 × 480", (640, 480)),
    ],
    value=(640, 480),
    description="处理尺寸:",
    layout=widgets.Layout(width="230px")
)

app_mode = widgets.Dropdown(
    options=[
        ("农业表型分析", "Plant Phenotyping"),
        ("通用边缘处理", "General"),
        ("显微图像增强", "Microscopy"),
    ],
    description="应用场景:",
    layout=widgets.Layout(width="270px")
)

profile_widget = widgets.Dropdown(
    options=[
        ("种子", "Seeds"),
        ("绿色叶片", "Green leaves"),
    ],
    description="分析对象:",
    layout=widgets.Layout(width="230px")
)

algo = widgets.Dropdown(
    options=["Sobel", "Prewitt"],
    description="边缘算法:",
    layout=widgets.Layout(width="230px")
)

backend_widget = widgets.Dropdown(
    options=[
        ("FPGA 硬件", "FPGA"),
        ("PS 软件", "PS"),
    ],
    description="计算后端:",
    layout=widgets.Layout(width="230px")
)

auto_widget = widgets.Checkbox(
    value=True,
    description="自动估计参数",
    indent=False,
    layout=widgets.Layout(width="170px")
)

threshold_widget = widgets.IntSlider(
    value=40,
    min=0,
    max=255,
    description="边缘阈值:",
    continuous_update=False,
    layout=widgets.Layout(width="310px")
)

area_widget = widgets.BoundedFloatText(
    value=1000,
    min=1,
    max=307200,
    description="最小面积:",
    layout=widgets.Layout(width="230px")
)

run = widgets.Button(
    description="开始分析",
    icon="play",
    disabled=True,
    layout=widgets.Layout(width="180px", height="40px")
)
run.add_class("agri-run")

status = widgets.HTML()
input_info = widgets.HTML("上传 JPG 或 PNG，开始图像分析。")
notice = widgets.HTML()
mode_help = widgets.HTML()
parameter_info = widgets.HTML()
phenotype_cards = widgets.HTML()
performance_cards = widgets.HTML()
table = widgets.HTML()
errors = widgets.Output()

images = [widgets.Image(format="png") for _ in range(3)]

titles = [
    widgets.HTML("<b>原始图像 · Original RGB</b>"),
    widgets.HTML("<b>边缘结果 · FPGA</b>"),
    widgets.HTML("<b>分析标注 · Annotated Result</b>"),
]

panels = []
for title, view in zip(titles, images):
    view.layout = widgets.Layout(
        width="100%",
        max_width="390px",
        height="auto"
    )

    panel = widgets.VBox(
        [title, view],
        layout=widgets.Layout(
            flex="1 1 260px",
            min_width="0",
            padding="12px",
            margin="3px",
            border="1px solid #dce5d5",
        )
    )
    panel.add_class("agri-panel")
    panels.append(panel)

controls = [
    upload,
    resolution,
    app_mode,
    profile_widget,
    algo,
    backend_widget,
    auto_widget,
    threshold_widget,
    area_widget,
]

dashboard = widgets.VBox([
    widgets.HTML("""
    <div class="agri-hero">
      <h2>AgriVision-FPGA</h2>
      <p>种子计数 · 叶片表型 · 图像边缘分析</p>
    </div>
    """),

    status,

    widgets.HTML('<div class="agri-section">01　选择图像</div>'),
    widgets.HBox(
        [upload, resolution, app_mode],
        layout=widgets.Layout(flex_flow="row wrap")
    ),
    input_info,
    widgets.HTML(
        '<div class="agri-help">'
        '处理尺寸：越高保留的细节越多。图像等比例缩放，必要时补白。'
        '</div>'
    ),

    widgets.HTML('<div class="agri-section">02　设置分析方式</div>'),
    widgets.HBox(
        [profile_widget, algo, backend_widget],
        layout=widgets.Layout(flex_flow="row wrap")
    ),
    widgets.HBox(
        [auto_widget, threshold_widget, area_widget],
        layout=widgets.Layout(flex_flow="row wrap")
    ),
    widgets.HTML(
        '<div class="agri-help">'
        '<b>自动参数</b>：根据图像和所选算法估计；取消勾选后可手动调整。'
        '<br><b>边缘阈值</b>：越高杂边越少，但可能断边；0 显示梯度强度。'
        '<br><b>最小面积</b>：过滤小目标，过大会漏掉小种子，单位 pixel²。'
        '<br><b>计算后端</b>：FPGA 使用硬件；PS 使用板端 ARM 软件。'
        '</div>'
    ),
    mode_help,
    run,
    parameter_info,
    notice,

    widgets.HTML('<div class="agri-section">03　查看分析结果</div>'),
    widgets.HBox(
        panels,
        layout=widgets.Layout(flex_flow="row wrap")
    ),
    widgets.HTML(
        '<div class="agri-help">'
        '<span style="color:#876900;font-weight:700">黄色＋深色描边</span>'
        '：目标轮廓或边缘；'
        '<span style="color:#288246;font-weight:700">绿色框</span>'
        '：目标外接矩形。颜色仅用于展示，不影响测量。'
        '</div>'
    ),

    phenotype_cards,
    table,

    widgets.HTML('<div class="agri-section">04　性能与验证</div>'),
    performance_cards,
    widgets.HTML(
        '<div class="agri-help">'
        '尺寸为当前处理图像的 pixel / pixel²。'
        'FPS 仅指边缘计算，不代表完整应用帧率。'
        '<br>完整分析包含预处理、参数估计、参考计算和表型分析，'
        '不含页面绘制。Golden PASS 验证后端计算一致性，不代表测量精度。'
        '</div>'
    ),
    errors,
])

dashboard.add_class("agri-app")

ui_state = {
    "source": None,
    "name": None,
    "mode": None,
    "result": None,
}

# In[3]:

def preview_bytes(array):
    h, w = array.shape[:2]
    scale = min(1.0, 390.0 / w)

    if scale < 1:
        array = cv2.resize(
            array,
            (int(round(w * scale)), int(round(h * scale))),
            interpolation=cv2.INTER_AREA
        )

    return png_bytes(np.ascontiguousarray(array))

def outlined_edges(rgb, mask):
    """深色外沿＋黄色中心，只改变显示图。"""
    visible = (mask > 0).astype(np.uint8)
    outer = cv2.dilate(
        visible, np.ones((3, 3), np.uint8), iterations=1
    )

    result = rgb.copy()
    result[(outer > 0) & (visible == 0)] = (35, 45, 25)
    result[visible > 0] = (255, 225, 35)
    return result

def annotation_preview(result):
    """
    根据本次结果重新生成清晰预览。
    在预览尺寸上绘制，避免细线缩小后消失。
    不修改算法输出和统计值。
    """
    rgb = result["original_rgb"]
    h, w = rgb.shape[:2]
    scale = min(1.0, 390.0 / w)
    pw = max(1, int(round(w * scale)))
    ph = max(1, int(round(h * scale)))

    preview = cv2.resize(
        rgb, (pw, ph),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST
    )

    phenotype = result["phenotype_result"]

    if phenotype is not None:
        # clean_edges 对应本次所用的候选轮廓来源：
        # 种子为闭运算边缘，绿色叶片为颜色区域。
        clean = result["clean_edges"]
        contours = cv2.findContours(
            clean.copy(),
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )[-2]

        # 通过原始 bbox 匹配已经通过过滤的对象，
        # 不把被面积/边缘支持过滤的候选重新画出来。
        contour_map = {}
        for contour in contours:
            key = tuple(int(v) for v in cv2.boundingRect(contour))
            contour_map.setdefault(key, []).append(contour)

        sx, sy = pw / float(w), ph / float(h)

        for obj in phenotype["objects"]:
            key = tuple(obj["bbox"])
            matches = contour_map.get(key, [])

            if matches:
                # bbox 相同时选面积最接近实际记录的轮廓。
                contour = min(
                    matches,
                    key=lambda c: abs(cv2.contourArea(c) - obj["area"])
                )
                points = contour.astype(np.float32)
                points[:, :, 0] *= sx
                points[:, :, 1] *= sy
                points = np.rint(points).astype(np.int32)
            else:
                # 无匹配时不伪造轮廓，仅保留该对象框与编号。
                points = None

            box = np.asarray(obj["rotated_box"], dtype=np.float32)
            box[:, 0] *= sx
            box[:, 1] *= sy
            box = np.rint(box).astype(np.int32)

            # 先画框，再画轮廓，确保黄色边缘处于上层。
            cv2.polylines(
                preview, [box], True, (40, 140, 65), 1
            )

            if points is not None:
                cv2.drawContours(
                    preview, [points], -1, (30, 40, 20), 3
                )
                cv2.drawContours(
                    preview, [points], -1, (255, 225, 35), 1
                )

            x, y, _, _ = obj["bbox"]
            label_x = max(0, min(pw - 20, int(round(x * sx))))
            label_y = max(14, min(ph - 3, int(round(y * sy)) - 3))
            position = (label_x, label_y)

            cv2.putText(
                preview, str(obj["id"]), position,
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (25, 40, 25), 3
            )
            cv2.putText(
                preview, str(obj["id"]), position,
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 245), 1
            )

    else:
        edge = result["display_edge"]

        if result["parameters"]["threshold"] > 0:
            mask = np.where(edge > 0, 255, 0).astype(np.uint8)
        else:
            # Microscopy 的 display_edge 已是应用后处理二值图。
            cutoff = 1 if result["application"] == "Microscopy" else 40
            mask = np.where(edge >= cutoff, 255, 0).astype(np.uint8)

        # 用面积插值保留缩小时的边缘覆盖，再二值化。
        small_mask = cv2.resize(
            mask, (pw, ph),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST
        )
        small_mask = np.where(small_mask >= 64, 255, 0).astype(np.uint8)

        # 显示稠密边缘时使用轮廓线，减少黄色块覆盖。
        contours = cv2.findContours(
            small_mask,
            cv2.RETR_LIST,
            cv2.CHAIN_APPROX_SIMPLE
        )[-2]
        line_mask = np.zeros((ph, pw), np.uint8)
        cv2.drawContours(line_mask, contours, -1, 255, 1)

        preview = outlined_edges(preview, line_mask)

    return png_bytes(np.ascontiguousarray(preview))

def refresh_controls():
    plant = app_mode.value == "Plant Phenotyping"

    profile_widget.disabled = not plant
    threshold_widget.disabled = auto_widget.value
    area_widget.disabled = auto_widget.value or not plant

    run.disabled = (
        ui_state["source"] is None
        or (backend_widget.value == "FPGA" and engine.fault)
    )

    if engine.fault:
        text = "FPGA 异常 · 需要恢复硬件后重新初始化"
        color = "#a33a2a"
    elif engine.verified:
        text = "FPGA 已验证　|　DMA 最近一次传输完成　|　Overlay 已加载"
        color = "#315f3e"
    else:
        text = "Overlay 已加载　|　FPGA / DMA 等待首帧验证"
        color = "#75612a"

    status.value = (
        '<div class="agri-status" style="color:{}">{}</div>'
    ).format(color, text)

    suffix = (
        " · 有效视野" if app_mode.value == "Microscopy" else ""
    )
    titles[1].value = "<b>边缘结果 · {}{}</b>".format(
        backend_widget.value, suffix
    )

    if app_mode.value == "Microscopy":
        explanation = (
            "显微模式：增强局部对比度、轻度去噪并屏蔽视野边界。"
            "黄色显示边缘，不进行细胞计数。"
        )
    elif plant and profile_widget.value == "Green leaves":
        explanation = (
            "绿色叶片：颜色区域约束结合后端边缘检查，抑制灰色阴影。"
            "不适用绿色背景；测量可能不包含褐色叶柄。"
        )
    elif plant:
        explanation = (
            "种子模式：适合简单背景、相互分离的种子。"
            "自动参数仍需检查漏检、阴影和粘连。"
        )
    else:
        explanation = "通用模式：显示边缘与叠加图，不计算目标数量和尺寸。"

    mode_help.value = '<div class="agri-help">{}</div>'.format(explanation)

def clear_results():
    ui_state["result"] = None

    for view in images[1:]:
        view.value = b""

    phenotype_cards.value = cards([
        ("目标数量 · Count", "—"),
        ("平均面积 · Area", "—"),
        ("平均长度 · Length", "—"),
        ("平均宽度 · Width", "—"),
        ("平均周长 · Perimeter", "—"),
    ])

    performance_cards.value = ""
    parameter_info.value = ""
    table.value = ""

def show_input():
    if ui_state["source"] is None:
        return

    width, height = resolution.value
    rgb, _, _ = prepare_image(ui_state["source"], width, height)
    images[0].value = preview_bytes(rgb)

    sh, sw = ui_state["source"].shape[:2]
    input_info.value = (
        '<div class="agri-help"><b>{}</b>'
        '　原始 {}×{}　→　处理 {}×{}　|　{} → RGB</div>'
    ).format(
        html.escape(ui_state["name"]),
        sw, sh, width, height,
        html.escape(ui_state["mode"])
    )

def uploaded(change):
    clear_results()
    ui_state["source"] = None
    images[0].value = b""

    try:
        value = upload.value
        if not value:
            input_info.value = "请上传图片。"
            return

        if isinstance(value, dict):
            name = list(value.keys())[-1]
            content = bytes(value[name]["content"])
        else:
            item = value[-1]
            name = item["name"]
            content = bytes(item["content"])

        rgb, mode = decode_image(content)
        ui_state.update(source=rgb, name=name, mode=mode)

        show_input()
        notice.value = (
            '<p style="color:#39734b">图片已就绪，选择分析方式后点击“开始分析”。</p>'
        )

    except Exception as exc:
        input_info.value = "没有有效输入图片。"
        notice.value = (
            '<p style="color:#a33a2a">读取失败：{}</p>'
        ).format(html.escape(str(exc)))

    finally:
        refresh_controls()

def changed(change):
    clear_results()
    show_input()
    refresh_controls()
    notice.value = (
        '<p style="color:#75612a">设置已改变，请重新运行分析。</p>'
    )

def render_table(objects):
    if not objects:
        return (
            '<div class="agri-help">'
            '未检测到有效完整目标，请检查模式、背景及参数。'
            '</div>'
        )

    rows = "".join(
        "<tr><td>{id}</td><td>{area:.1f}</td>"
        "<td>{length:.1f}</td><td>{width:.1f}</td>"
        "<td>{perimeter:.1f}</td></tr>".format(**obj)
        for obj in objects
    )

    return (
        '<table class="agri-table"><tr>'
        "<th>编号</th>"
        "<th>面积 / pixel²</th>"
        "<th>长度 / pixel</th>"
        "<th>宽度 / pixel</th>"
        "<th>周长 / pixel</th>"
        "</tr>" + rows + "</table>"
    )

def execute(_):
    if ui_state["source"] is None:
        return

    clear_results()
    errors.clear_output()

    for control in controls + [run]:
        control.disabled = True

    notice.value = (
        '<p style="color:#75612a">正在分析，请勿同时运行其他硬件任务……</p>'
    )

    try:
        result = engine.analyze(
            ui_state["source"],
            resolution.value,
            algo.value,
            backend_widget.value,
            app_mode.value,
            automatic=auto_widget.value,
            threshold=threshold_widget.value,
            min_area=area_widget.value,
            profile=profile_widget.value,
        )

        ui_state["result"] = result

        images[0].value = preview_bytes(result["original_rgb"])
        images[1].value = preview_bytes(result["display_edge"])
        images[2].value = annotation_preview(result)

        p = result["parameters"]
        area_text = (
            "{:.1f} pixel²".format(p["min_area"])
            if app_mode.value == "Plant Phenotyping"
            else "不适用"
        )

        parameter_info.value = (
            '<div class="agri-parameters">'
            '<b>本次实际参数</b>　{}　|　阈值 {}　|　最小面积 {}'
            '<br><small>{}</small></div>'
        ).format(
            result["algorithm"],
            p["threshold"],
            area_text,
            html.escape(p["note"])
        )

        pheno = result["phenotype_result"]

        if pheno is not None:
            phenotype_cards.value = cards([
                ("目标数量 · Count", pheno["count"]),
                ("平均面积 · Area",
                 "{:.1f} pixel²".format(pheno["avg_area"])),
                ("平均长度 · Length",
                 "{:.1f} pixel".format(pheno["avg_length"])),
                ("平均宽度 · Width",
                 "{:.1f} pixel".format(pheno["avg_width"])),
                ("平均周长 · Perimeter",
                 "{:.1f} pixel".format(pheno["avg_perimeter"])),
            ])
            table.value = render_table(pheno["objects"])
        else:
            phenotype_cards.value = ""
            table.value = (
                '<div class="agri-help">'
                '当前场景仅展示边缘，不输出农业目标数量或尺寸。'
                '</div>'
            )

        ps_ms = result["ps_ms"]
        pl_ms = result["fpga_ms"]
        active_ms = pl_ms if pl_ms is not None else ps_ms
        fps = 1000.0 / active_ms if active_ms > 0 else 0.0

        performance_cards.value = cards([
            ("PS 边缘计算", "{:.3f} ms".format(ps_ms)),
            ("FPGA E2E",
             "{:.3f} ms".format(pl_ms)
             if pl_ms is not None else "未运行"),
            ("{} 边缘 FPS".format(result["backend"]),
             "{:.1f}".format(fps)),
            ("PS / FPGA 加速比",
             "{:.2f}×".format(ps_ms / pl_ms)
             if pl_ms is not None and pl_ms > 0 else "—"),
            ("Golden 验证",
             "PASS" if result["golden"] else "未运行 · PS 模式"),
            ("图像预处理",
             "{:.1f} ms".format(result["preprocess_ms"])),
            ("参数准备",
             "{:.1f} ms".format(result["parameter_ms"])),
            ("完整分析",
             "{:.1f} ms".format(result["analysis_ms"])),
        ])

        notice.value = (
            '<p style="color:#39734b">'
            '分析完成。黄色线为清晰显示效果，未改变原始计算与测量结果。'
            '</p>'
        )

    except KeyboardInterrupt:
        notice.value = (
            '<p style="color:#a33a2a">'
            '运行已中断；若中断发生在 FPGA 调用期间，请先恢复硬件。'
            '</p>'
        )

    except Exception as exc:
        clear_results()
        notice.value = (
            '<p style="color:#a33a2a">分析失败：{}</p>'
        ).format(html.escape(str(exc)))

        with errors:
            traceback.print_exc()

    finally:
        for control in controls + [run]:
            control.disabled = False
        refresh_controls()

upload.observe(uploaded, names="value")

for control in [
    resolution,
    app_mode,
    profile_widget,
    algo,
    backend_widget,
    auto_widget,
    threshold_widget,
    area_widget,
]:
    control.observe(changed, names="value")

run.on_click(execute)

clear_results()
refresh_controls()
display(dashboard)

