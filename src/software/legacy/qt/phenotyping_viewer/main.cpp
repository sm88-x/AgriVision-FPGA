// AgriVision Edge · Day 5 —— Qt 界面（单粒表型交互 + YOLO/FPGA 融合结果）

//

// 基线：detection 的 agri_morph_gui_4（QProcess 拉起 Python 后端 + 轮询 live_status.json），

//       本文件在其上增加任务书 C1/C2 要求的 phenotyping 功能：

//   · 四视图 Original / FPGA Output / Detection · YOLO / Phenotyping

//   · SEED 目标列表（Seed ID / Confidence / Length / Width / Area，异常行首 ⚠）

//   · 单粒详情卡（Area / Length / Width / Perimeter / Aspect Ratio / Circularity /

//     Status，并直接引用 B 给的 status_reason）

//   · 双向选中：点列表 → 图中目标高亮；点 Detection / Phenotyping 图 → 反选列表行

//   · 一键导出 Annotated Image + CSV + JSON 到同一批次目录

//     YYYYMMDD_HHMMSS_batch_xxx

//

// 数据来源（两种结构都认，Qt 侧一律只读不重算）：

//   A. B 的原生输出 data/evidence/phenotyping/objects.json

//      {schema, units, detector, frames:[{frame_id, seed_count, letterbox, objects:[…]}],

//       objects:[展平…]}，单目标字段 bbox_xyxy_src / seed_id / status_reason …

//   B. C 侧契约 docs/phenotyping_result_format.md 的 objects.json

//      {schema_version, frame_id, seed_count, objects:[{bbox: [x1,y1,x2,y2], …}]}

//

// 单位：未做毫米标定，Length/Width/Perimeter 为 px，Area 为 px²；

//       Aspect Ratio、Circularity 无量纲。

#include <QtWidgets>

#include <QElapsedTimer>

#include <QSaveFile>

#include <QSet>

#include <algorithm>

#include <cmath>

#include <functional>

// ---------------------------------------------------------------- 数据模型

struct SeedObject {

    int id = 0;                  // 1 起；界面显示 Seed #001

    QString seedIdText;          // B 的 "Seed #001"（有就用，没有按 id 生成）

    double confidence = 0.0;

    QRectF bbox;                 // 原图像素坐标 [x1,y1,x2,y2]

    double areaPx = 0.0;         // px²

    double lengthPx = 0.0;       // px（最小外接旋转矩形长边）

    double widthPx = 0.0;        // px（短边）

    double perimeterPx = 0.0;    // px

    double aspectRatio = 0.0;    // 无量纲，Length/Width ≥ 1

    double circularity = 0.0;    // 无量纲，4πA/P²

    QString status;              // OK / MERGED / BROKEN_CONTOUR / INVALID / 空

    QString statusReason;        // B 的一句话原因，详情卡直接引用

    bool hasMetrics = false;     // live 检测只有 bbox，没有 6 项表型

};

struct FrameData {

    int frameId = -1;

    int seedCount = -1;          // JSON 声明的目标数（-1 = 未给）

    QVector<SeedObject> objects;

    QImage original;             // 原图（YOLO 输入）

    QImage fpga;                 // PL 管线输出（二值边缘图）

    QString imageDir;            // 这一帧的图取自哪个目录（可核对）

};

struct ViewState {

    QImage composite;

    double scale = 1.0;

    QPointF offset;

};

static QColor statusColor(const QString &status)

{

    if (status == "OK") return QColor(30, 125, 52);

    if (status == "MERGED") return QColor(199, 119, 0);

    if (status == "BROKEN_CONTOUR") return QColor(179, 65, 31);

    if (status == "INVALID") return QColor(139, 26, 26);

    return QColor(96, 96, 96);       // 未知/未声明状态：中性灰

}

static QString displayTag(const SeedObject &o)

{

    if (!o.seedIdText.isEmpty()) return o.seedIdText;

    return QString("Seed #%1").arg(o.id, 3, 10, QChar('0'));

}

// 选中高亮色：必须和四种 status 色都不同，否则密集场景里分不出选中项

static const QColor kSelectColor(21, 101, 192);

class ClickableLabel : public QLabel

{

public:

    using QLabel::QLabel;

    std::function<void(QPoint)> onClicked;

protected:

    void mousePressEvent(QMouseEvent *event) override

    {

        if (onClicked) onClicked(event->position().toPoint());

        QLabel::mousePressEvent(event);

    }

};

// 从可执行文件目录往上找仓库根下的文件（编译目录深浅变化都能定位）

static QString findRepoFile(const QString &relative)

{

    QDir dir(QCoreApplication::applicationDirPath());

    for (int i = 0; i < 6; ++i) {

        const QString candidate = dir.absoluteFilePath(relative);

        if (QFileInfo::exists(candidate)) return candidate;

        if (!dir.cdUp()) break;

    }

    return QString();

}

// 找某一帧的原图 / PL 结果图：按候选目录 × 候选文件名逐个试

static QString frameImagePath(const QStringList &dirs, int frameId, bool fpga)

{

    QStringList names;

    if (frameId >= 0) {

        const QString f6 = QString::number(frameId).rightJustified(6, '0');

        const QString f3 = QString::number(frameId).rightJustified(3, '0');

        if (fpga) {

            names << "pl_gray_" + f6 + ".png" << "pl_gray_" + f3 + ".png"

                  << "frame_" + f3 + "_fpga.png" << "fpga_" + f3 + ".png";

        } else {

            names << "raw_bgr_" + f6 + ".png" << "raw_bgr_" + f3 + ".png"

                  << "frame_" + f3 + "_original.png" << "original_" + f3 + ".png";

        }

    }

    names << (fpga ? "fpga.png" : "original.png");

    for (const QString &dir : dirs) {

        for (const QString &name : names) {

            const QString path = QDir(dir).filePath(name);

            if (QFileInfo::exists(path)) return path;

        }

    }

    return QString();

}

// 候选图像目录：显式指定 > JSON 所在目录 > 上级目录 > 仓库 hardware/*（*_pc 优先，

// B 的 README 指明正式数据是 hardware/image_run01_pc）

static QStringList buildImageDirs(const QString &jsonPath, const QString &explicitDir)

{

    QStringList dirs;

    auto add = [&](const QString &path) {

        if (path.isEmpty()) return;

        const QString clean = QDir(path).absolutePath();

        if (!dirs.contains(clean)) dirs << clean;

    };

    add(explicitDir);

    const QDir jsonDir = QFileInfo(jsonPath).absoluteDir();

    add(jsonDir.absolutePath());

    QDir up = jsonDir;

    QString root;

    for (int i = 0; i < 5; ++i) {

        if (!up.cdUp()) break;

        const bool isRepoRoot = QFileInfo(up.absoluteFilePath("hardware")).isDir()

                                || QFileInfo(up.absoluteFilePath(".git")).exists();

        if (isRepoRoot) {

            add(up.absolutePath());   // 仓库根也当作候选（方便放 original.png/fpga.png）

            root = up.absolutePath();

            break;

        }

    }

    if (!root.isEmpty()) {

        const QString hardware = root + "/hardware";

        QStringList subs = QDir(hardware).entryList(QDir::Dirs | QDir::NoDotAndDotDot);

        subs.sort();

        for (const QString &s : subs)

            if (s == "image_run01_pc") add(hardware + "/" + s);

        for (const QString &s : subs)

            if (s.endsWith("_pc") && s != "image_run01_pc") add(hardware + "/" + s);

        for (const QString &s : subs)

            if (!s.endsWith("_pc") && s != "image_run01_pc") add(hardware + "/" + s);

    }

    return dirs;

}

int main(int argc, char *argv[])

{

    // 高分屏（125%/150% 缩放）下让字号按真实比例走：否则行高被压缩，控件会挤在一起

    QGuiApplication::setHighDpiScaleFactorRoundingPolicy(

        Qt::HighDpiScaleFactorRoundingPolicy::PassThrough);

    QApplication app(argc, argv);

    app.setStyle("Fusion");

    app.setFont(QFont("Microsoft YaHei UI", 10));

    // ------------------------------------------------------------ 启动参数

    QCommandLineParser cli;

    cli.setApplicationDescription("AgriVision Edge phenotyping Qt 界面（表型交互）");

    cli.addHelpOption();

    // phenotyping：离线表型交互

    QCommandLineOption optLoad("load", "启动即载入表型结果 JSON（B 的 objects.json 或契约 objects.json）", "json");

    QCommandLineOption optImages("images", "帧图像所在目录（默认自动探测）", "dir");

    QCommandLineOption optFrame("frame", "载入后切到第 N 帧（从 0 数，证据截图用）", "n", "0");

    QCommandLineOption optExport("export", "载入后把当前帧导出批次到该目录", "dir");

    QCommandLineOption optExportAll("export-all", "把载入的所有帧各导出一个批次", "dir");

    QCommandLineOption optShot("shot", "延时对窗口截图（界面证据/逐像素核对）", "png");

    QCommandLineOption optSelectRow("select-row", "合成一次“点列表行选目标”（选中信号自检）", "id");

    QCommandLineOption optClickSeed("click-seed", "合成一次“点图选目标”（hit-test 自检）", "id");

    QCommandLineOption optQuit("quit", "截图/导出完成后退出");

    // detection 既有：真链路 / 后端

    QCommandLineOption optCapture("capture", "等界面就绪后截图到指定 PNG 再退出", "png");

    QCommandLineOption optCaptureTimeout("capture-timeout", "截图等待上限(ms)", "ms", "30000");

    QCommandLineOption optAutoStart("auto-start", "启动后自动开始（截图/无人值守用）");

    QCommandLineOption optMode("mode", "Live/Image/Batch", "mode", "Live");

    QCommandLineOption optSource("source", "board(真链路) / mock(不开相机不占板)", "src", "mock");

    QCommandLineOption optInput("input", "Image 图片或 Batch/Live 目录", "path");

    QCommandLineOption optDetect("detect", "是否开启 AI 检测", "0/1", "1");

    QCommandLineOption optConf("conf", "置信度阈值", "float", "0.25");

    QCommandLineOption optDetector("detector", "mock / auto", "d", "mock");

    QCommandLineOption optFrames("frames", "Live 帧数(0=持续)", "n", "0");

    QCommandLineOption optBackend("backend", "后端适配器脚本", "py");

    QCommandLineOption optPython("python", "Python 解释器", "exe", "python");

    QCommandLineOption optHost("host", "板卡 IP", "ip", "192.168.2.99");

    QCommandLineOption optPort("port", "板卡端口", "p", "9000");

    QCommandLineOption optSize("size", "分辨率 WxH", "size", "640x480");

    QCommandLineOption optOutDir("out-dir", "本次运行证据目录", "dir");

    // 只影响界面外观（不同窗口尺寸/展开状态的证据截图用）

    QCommandLineOption optUiSize("ui-size", "窗口尺寸 WxH（默认按屏幕自适应）", "WxH");

    QCommandLineOption optUiExpand("ui-expand", "折叠区块与日志默认展开（证据截图用）");

    cli.addOptions({optLoad, optImages, optFrame, optExport, optExportAll, optShot,

                    optSelectRow, optClickSeed, optQuit, optCapture, optCaptureTimeout,

                    optAutoStart, optMode, optSource, optInput, optDetect, optConf,

                    optDetector, optFrames, optBackend, optPython, optHost, optPort,

                    optSize, optOutDir, optUiSize, optUiExpand});

    cli.process(app);

    const QString sizeText = cli.value(optSize);

    const int runWidth = sizeText.section('x', 0, 0).toInt();

    const int runHeight = sizeText.section('x', 1, 1).toInt();

    const QString appDirPath = QCoreApplication::applicationDirPath();

    // 默认后端 = 队友 detection 验证过的适配器（本界面只换显示层，不动那条链路）

    // detection 的 agri_morph_gui_4 后端适配器（复用，不另存一份，避免和队友分叉）

    const QString adapterRel = "src/software/adapters/detection_adapter.py";

    QString backendDefault = findRepoFile(adapterRel);

    if (backendDefault.isEmpty())

        backendDefault = QDir(appDirPath).absoluteFilePath("../../../../" + adapterRel);

    const QString backendPath = cli.value(optBackend).isEmpty()

        ? backendDefault : cli.value(optBackend);

    const QString outDir = cli.value(optOutDir).isEmpty()

        ? QDir(appDirPath).absoluteFilePath("phenotyping_run") : cli.value(optOutDir);

    const QString capturePath = cli.value(optCapture);

    const QString shotPath = cli.value(optShot);

    const QString exportDir = cli.value(optExport);

    const QString exportAllDir = cli.value(optExportAll);

    const QString loadPath = cli.value(optLoad);

    const QString imagesArg = cli.value(optImages);

    const int captureTimeout = cli.value(optCaptureTimeout).toInt();

    const bool autoStart = cli.isSet(optAutoStart) || !capturePath.isEmpty();

    const QString uiSizeText = cli.value(optUiSize);

    const bool uiExpand = cli.isSet(optUiExpand);

    // ------------------------------------------------------------ 窗口与样式

    QMainWindow win;

    win.setWindowTitle("AgriVision Edge · Day 5 · YOLO + FPGA + Phenotyping");

    {

        // 初始尺寸跟屏幕走：小屏/高缩放时不要把窗口撑出屏幕，否则控件会被压在一起

        const QRect avail = win.screen() ? win.screen()->availableGeometry()

                                         : QRect(0, 0, 1920, 1080);

        int wantW = qMin(1620, int(avail.width() * 0.92));

        int wantH = qMin(980, int(avail.height() * 0.92));

        if (!uiSizeText.isEmpty()) {

            wantW = uiSizeText.section('x', 0, 0).toInt();

            wantH = uiSizeText.section('x', 1, 1).toInt();

        }

        win.setMinimumSize(1120, 700);

        win.resize(qMax(wantW, 1120), qMax(wantH, 700));

    }

    win.setStyleSheet(R"(

        QMainWindow { background:#f3f6ee; }

        QWidget { color:#214630; }

        QFrame#header { background:#153e2b; border-radius:9px; }

        QFrame#header QLabel { color:white; font-size:17px; font-weight:700; }

        QFrame#card { background:white; border:1px solid #d8e3cf; border-radius:10px; }

        QFrame#ai { background:#eef7ea; border:1px solid #bcd8ae; border-radius:10px; }

        QFrame#phenotyping { background:#fdf6e7; border:1px solid #e3cfa5; border-radius:10px; }

        QFrame#divider { background:#e4ecdc; max-height:1px; min-height:1px; }

        QLabel#view { background:#edf3e6; border:1px dashed #bfd1b1;

                      border-radius:8px; color:#7f9871; }

        QLabel#big { font-size:20px; font-weight:700; color:#2c6b3f; }

        QLabel#chip { background:#eef4e9; border:1px solid #dbe6d1; border-radius:9px;

                      padding:3px 10px; color:#2c5a3c; font-size:12px; }

        QLabel#caption { font-size:13px; font-weight:700; }

        QLabel#consistency { font-size:12px; font-weight:700; }

        QLabel#detailKey { color:#5c7a63; }

        QLabel#detailValue { font-size:15px; font-weight:700; }

        QLabel#hint { color:#7d947f; font-size:12px; }

        QToolButton#sectionHead { background:#f6faf2; border:1px solid #e2ebdb;

                      border-radius:6px; color:#2c5a3c; font-weight:700;

                      padding:5px 8px; text-align:left; }

        QToolButton#sectionHead:hover { background:#ecf5e4; }

        QPushButton { background:#e7f0dc; padding:7px; border:1px solid #c7d9b8;

                      border-radius:6px; }

        QPushButton:hover { background:#d8e9c6; }

        QPushButton#start { background:#387342; color:white; }

        QPushButton#export { background:#8a5a19; color:white; }

        QPushButton:disabled { color:#9aab90; background:#e9eee2; }

        QLineEdit,QComboBox,QSpinBox { background:white; padding:5px;

                      border:1px solid #ccdaba; border-radius:5px; min-height:20px; }

        QPlainTextEdit { background:#f9fbf6; border:1px solid #d9e4cf; border-radius:6px; }

        QTableWidget { background:white; border:1px solid #dce6d5;

                      border-radius:6px; gridline-color:#eef3ea; }

        QHeaderView::section { background:#eef4e9; border:0; padding:5px;

                      font-weight:700; color:#2c5a3c; }

        QTableWidget::item:selected { background:#d9ecd2; color:#153e2d; }

        QScrollArea { background:transparent; border:0; }

        QScrollArea > QWidget > QWidget { background:transparent; }

        QScrollBar:vertical { background:#eef4e9; width:9px; border-radius:4px; }

        QScrollBar::handle:vertical { background:#bccfae; border-radius:4px;

                      min-height:30px; }

        QScrollBar::handle:vertical:hover { background:#9fba8c; }

        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }

        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {

                      background:transparent; }

        QSplitter::handle { background:#dfe9d7; }

        QSplitter::handle:horizontal { width:7px; }

        QSplitter::handle:hover { background:#b6cfa4; }

    )");

    auto *root = new QWidget;

    win.setCentralWidget(root);

    auto *vl = new QVBoxLayout(root);

    vl->setContentsMargins(18, 16, 18, 16);

    vl->setSpacing(8);

    auto *header = new QFrame;

    header->setObjectName("header");

    auto *head = new QHBoxLayout(header);

    head->addWidget(new QLabel("AGRIVISION EDGE"));

    head->addStretch();

    head->addWidget(new QLabel("DAY 5 · CAMERA/IMAGE → PL V4 → YOLO → ROI → 6 项表型 → Qt"));

    vl->addWidget(header);

    // 状态行做成小胶囊并把高度钉住：live 时文字长短变化不会让整页上下抖

    auto *status = new QLabel("待运行 · 先「载入表型结果 JSON」看 B 的结果，或 Start 跑一次真链路");

    status->setObjectName("chip");

    status->setWordWrap(true);

    status->setMinimumHeight(24);

    vl->addWidget(status);

    auto *metrics = new QLabel("FRAME -- | Camera FPS -- | Live FPS -- | RTT -- | AI 推理 --");

    metrics->setObjectName("chip");

    metrics->setWordWrap(true);

    metrics->setMinimumHeight(24);

    vl->addWidget(metrics);

    // 折叠区块：点标题展开/收起。有选项的控件块常驻；纯说明信息默认收起，省版面。

    // 标题只放短词：QToolButton 不会换行，长标题会把整栏的最小宽度撑大、拖窄时被裁掉。

    auto makeCollapsible = [](QVBoxLayout *host, const QString &title,

                              bool expanded, const QString &tip = QString())

        -> QVBoxLayout * {

        auto *btn = new QToolButton;

        btn->setObjectName("sectionHead");

        btn->setCheckable(true);

        btn->setChecked(expanded);

        btn->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);

        // 横向 Ignored：按钮宽度跟着栏宽走，绝不反过来撑大栏宽

        btn->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);

        btn->setText(title);

        if (!tip.isEmpty()) btn->setToolTip(tip);

        auto *content = new QWidget;

        auto *lay = new QVBoxLayout(content);

        lay->setContentsMargins(6, 4, 2, 2);

        lay->setSpacing(5);

        content->setVisible(expanded);

        host->addWidget(btn);

        host->addWidget(content);

        QObject::connect(btn, &QToolButton::toggled, content,

                         [btn, content](bool on) {

                             content->setVisible(on);

                             btn->setArrowType(on ? Qt::DownArrow : Qt::RightArrow);

                         });

        btn->setArrowType(expanded ? Qt::DownArrow : Qt::RightArrow);

        return lay;

    };

    // 三栏用 QSplitter：分割条自带双向箭头光标，可拖着改各栏宽度

    auto *body = new QSplitter(Qt::Horizontal);

    body->setChildrenCollapsible(false);

    body->setHandleWidth(7);

    // ---------------- 左侧控制区 ----------------

    auto *left = new QFrame;

    left->setObjectName("card");

    left->setMinimumWidth(296);

    left->setMaximumWidth(470);

    auto *leftOuter = new QVBoxLayout(left);

    leftOuter->setContentsMargins(6, 6, 6, 6);

    leftOuter->setSpacing(6);

    // 上半部分可滚动（内容再多也不会被压扁），下半部分操作条钉死在底部

    auto *leftScroll = new QScrollArea;

    leftScroll->setWidgetResizable(true);

    leftScroll->setFrameShape(QFrame::NoFrame);

    // AsNeeded 只是兜底：万一内容仍比栏宽，宁可出横向滚动条也不要静默裁掉选项

    leftScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);

    auto *leftBody = new QWidget;

    leftScroll->setWidget(leftBody);

    leftOuter->addWidget(leftScroll, 1);

    auto *pl = new QVBoxLayout(leftBody);

    pl->setContentsMargins(8, 6, 8, 6);

    pl->setSpacing(8);

    body->addWidget(left);

    auto field = [](QVBoxLayout *host, const QString &text, QWidget *w,

                    const QString &tip = QString()) {

        auto *lb = new QLabel(text);

        lb->setWordWrap(true);

        if (!tip.isEmpty()) {

            lb->setToolTip(tip);

            w->setToolTip(tip);

        }

        host->addWidget(lb);

        host->addWidget(w);

    };

    // phenotyping 主入口放在最上面：演示时先点这两个

    auto *phenotypingBox = new QFrame;

    phenotypingBox->setObjectName("phenotyping");

    auto *d5 = new QVBoxLayout(phenotypingBox);

    d5->setContentsMargins(10, 10, 10, 10);

    d5->setSpacing(6);

    d5->addWidget(new QLabel("表型结果（B → C）"));

    auto *openJson = new QPushButton("⇩ 载入表型结果 JSON");

    openJson->setToolTip("B 的 data/evidence/phenotyping/objects.json（多帧）或契约 objects.json");

    auto *save = new QPushButton("导出 CSV / JSON / Annotated");

    save->setObjectName("export");

    save->setEnabled(false);

    auto *frameBox = new QComboBox;

    frameBox->setEnabled(false);

    frameBox->setToolTip("B 的结果里一帧一行；切换帧立刻刷新四视图、列表与详情卡");

    d5->addWidget(openJson);

    d5->addWidget(save);

    d5->addWidget(new QLabel("帧 / FRAME"));

    d5->addWidget(frameBox);

    pl->addWidget(phenotypingBox);

    auto *mode = new QComboBox;

    mode->addItems({"Live", "Image", "Batch"});

    mode->setCurrentText(cli.value(optMode));

    field(pl, "模式", mode);

    auto *source = new QComboBox;

    source->addItems({"mock", "board"});

    source->setCurrentText(cli.value(optSource));

    field(pl, "数据源", source, "mock = 不开相机、不占板卡；board = 真链路（连板卡）");

    auto *sizeBox = new QComboBox;

    sizeBox->addItems({"640x480", "320x240", "256x256"});

    sizeBox->setCurrentText(sizeText);

    field(pl, "分辨率", sizeBox);

    auto *framesSpin = new QSpinBox;

    framesSpin->setRange(0, 100000);

    framesSpin->setValue(cli.value(optFrames).toInt());

    framesSpin->setSpecialValueText("持续运行，直到 Stop");

    field(pl, "Live 帧数", framesSpin, "0 = 持续运行，直到点 Stop");

    auto *aiBox = new QFrame;

    aiBox->setObjectName("ai");

    auto *aiLayout = new QVBoxLayout(aiBox);

    aiLayout->setContentsMargins(10, 10, 10, 10);

    auto *detect = new QCheckBox("AI Detection（YOLO 单类 seed）");

    detect->setChecked(cli.value(optDetect) != "0");

    aiLayout->addWidget(detect);

    auto *detector = new QComboBox;

    detector->addItems({"mock", "auto"});

    detector->setCurrentText(cli.value(optDetector));

    detector->setToolTip("mock = 假检测框；auto = 接 B 的 infer_seed.py 真模型");

    auto *detectorLabel = new QLabel("检测器");

    detectorLabel->setToolTip(detector->toolTip());

    aiLayout->addWidget(detectorLabel);

    aiLayout->addWidget(detector);

    auto *confLabel = new QLabel;

    auto *conf = new QSlider(Qt::Horizontal);

    conf->setRange(5, 95);

    conf->setValue(int(cli.value(optConf).toDouble() * 100));

    confLabel->setText(QString("置信度阈值 %1").arg(conf->value() / 100.0, 0, 'f', 2));

    aiLayout->addWidget(confLabel);

    aiLayout->addWidget(conf);

    pl->addWidget(aiBox);

    // 设备与后端：填一次就不动的项，默认折叠，点击标题才展开

    auto *devLay = makeCollapsible(

        pl, "设备与后端", uiExpand,

        "板卡 IP / Python 程序 / 后端脚本 / 本次运行目录 —— 点标题展开");

    auto *host = new QLineEdit(cli.value(optHost));

    field(devLay, "板卡 IP（端口 " + cli.value(optPort) + "）", host,

          "板卡的 IP；TCP 端口见括号，命令行 --port 可改");

    auto *python = new QLineEdit(cli.value(optPython));

    field(devLay, "Python 程序", python, "python 或 python.exe 的完整路径");

    auto *script = new QLineEdit(backendPath);

    field(devLay, "后端脚本", script,

          "默认 src/software/adapters/detection_adapter.py");

    auto *outDirEdit = new QLineEdit(outDir);

    field(devLay, "本次运行目录", outDirEdit);

    auto *pick = new QPushButton("选择 Image / Batch 输入");

    auto *inputLabel = new QLabel(cli.value(optInput).isEmpty() ? "（未选择）"

                                                                : cli.value(optInput));

    inputLabel->setWordWrap(true);

    devLay->addWidget(pick);

    devLay->addWidget(inputLabel);

    pl->addStretch();

    // —— 底部操作条：钉在左栏最下方，内容再长也永远看得见 ——

    auto *divider = new QFrame;

    divider->setObjectName("divider");

    leftOuter->addWidget(divider);

    auto *start = new QPushButton("▶ Start");

    start->setObjectName("start");

    start->setMinimumHeight(34);

    auto *stop = new QPushButton("■ Stop");

    auto *restart = new QPushButton("↻ Restart");

    stop->setEnabled(false);

    restart->setEnabled(false);

    leftOuter->addWidget(start);

    auto *stopRow = new QHBoxLayout;

    stopRow->setSpacing(6);

    stopRow->addWidget(stop);

    stopRow->addWidget(restart);

    leftOuter->addLayout(stopRow);

    auto *folder = new QPushButton("打开本次运行目录");

    leftOuter->addWidget(folder);

    // ---------------- 中间四视图 ----------------

    auto *mid = new QWidget;

    auto *gallery = new QGridLayout(mid);

    gallery->setSpacing(10);

    gallery->setContentsMargins(0, 0, 0, 0);

    body->addWidget(mid);

    const QStringList viewTitles = {"① Original · 原图（YOLO 输入）",

                                    "② FPGA Output · PL 处理结果",

                                    "③ Detection · YOLO",

                                    "④ Phenotyping · 单粒表型"};

    QVector<ClickableLabel *> viewList;

    for (int i = 0; i < viewTitles.size(); ++i) {

        auto *card = new QFrame;

        card->setObjectName("card");

        auto *cl = new QVBoxLayout(card);

        cl->setContentsMargins(10, 10, 10, 10);

        auto *caption = new QLabel(viewTitles[i]);

        caption->setObjectName("caption");

        cl->addWidget(caption);

        auto *view = new ClickableLabel("等待结果");

        view->setObjectName("view");

        view->setAlignment(Qt::AlignCenter);

        view->setMinimumSize(200, 120);

        view->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Ignored);

        cl->addWidget(view, 1);

        viewList.append(view);

        gallery->addWidget(card, i / 2, i % 2);

    }

    // ---------------- 右侧：计数 + 列表 + 详情卡 ----------------

    auto *right = new QFrame;

    right->setObjectName("card");

    right->setMinimumWidth(368);

    right->setMaximumWidth(620);

    auto *rightOuter = new QVBoxLayout(right);

    rightOuter->setContentsMargins(6, 6, 6, 6);

    auto *rightScroll = new QScrollArea;

    rightScroll->setWidgetResizable(true);

    rightScroll->setFrameShape(QFrame::NoFrame);

    rightScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    auto *rightBody = new QWidget;

    rightScroll->setWidget(rightBody);

    rightOuter->addWidget(rightScroll);

    auto *rl = new QVBoxLayout(rightBody);

    rl->setContentsMargins(10, 8, 10, 8);

    rl->setSpacing(8);

    body->addWidget(right);

    // 中栏吃满富余宽度；初值按常用比例分

    body->setStretchFactor(1, 1);

    body->setSizes({300, 900, 392});

    // 计数与一致性检查放同一行：省一行高度给表格

    auto *countRow = new QHBoxLayout;

    auto *count = new QLabel("--");

    count->setObjectName("big");

    auto *countTag = new QLabel("SEED COUNT");

    countTag->setObjectName("detailKey");

    countRow->addWidget(countTag);

    countRow->addWidget(count);

    countRow->addStretch();

    rl->addLayout(countRow);

    // 一致性检查单独一行（右栏空间富余，宁可多一行也不要让字挤成两行）

    auto *consistency = new QLabel("尚未载入对象");

    consistency->setObjectName("consistency");

    consistency->setWordWrap(false);

    rl->addWidget(consistency);

    auto *modelState = new QLabel("MODEL --");

    auto *inferLabel = new QLabel("AI 推理 -- ms");

    auto *modelRow = new QHBoxLayout;

    modelRow->addWidget(modelState);

    modelRow->addStretch();

    modelRow->addWidget(inferLabel);

    rl->addLayout(modelRow);

    // 单位写在标题上，表头用短词（出现滚动条时列宽变窄，长表头会被截）

    auto *listCaption = new QLabel("SEED 目标列表（长度 px · 面积 px²）");

    listCaption->setObjectName("caption");

    rl->addWidget(listCaption);

    auto *table = new QTableWidget(0, 5);

    table->setHorizontalHeaderLabels({"Seed", "Conf", "Length", "Width", "Area"});

    table->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);

    table->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);

    table->horizontalHeader()->setMinimumSectionSize(46);

    table->setFont(QFont("Microsoft YaHei UI", 9));

    table->verticalHeader()->setVisible(false);

    table->verticalHeader()->setDefaultSectionSize(22);

    table->setEditTriggers(QAbstractItemView::NoEditTriggers);

    table->setSelectionBehavior(QAbstractItemView::SelectRows);

    table->setSelectionMode(QAbstractItemView::SingleSelection);

    table->setAlternatingRowColors(true);

    table->setMinimumHeight(200);

    rl->addWidget(table, 1);

    auto *detailCaption = new QLabel("单粒详情 / PHENOTYPE");

    detailCaption->setObjectName("caption");

    rl->addWidget(detailCaption);

    auto *detailFrame = new QFrame;

    auto *dl = new QGridLayout(detailFrame);

    dl->setContentsMargins(0, 0, 0, 0);

    dl->setVerticalSpacing(3);

    const QStringList detailKeys = {"Area (px²)", "Length (px)", "Width (px)",

                                    "Perimeter (px)", "Aspect Ratio (-)",

                                    "Circularity (-)"};

    QVector<QLabel *> detailValues;

    for (int i = 0; i < detailKeys.size(); ++i) {

        auto *k = new QLabel(detailKeys[i]);

        k->setObjectName("detailKey");

        auto *v = new QLabel("—");

        v->setObjectName("detailValue");

        v->setAlignment(Qt::AlignRight | Qt::AlignVCenter);

        dl->addWidget(k, i, 0);

        dl->addWidget(v, i, 1);

        detailValues.append(v);

    }

    auto *statusKey = new QLabel("Status");

    statusKey->setObjectName("detailKey");

    auto *statusBadge = new QLabel("无选中");

    statusBadge->setAlignment(Qt::AlignCenter);

    statusBadge->setStyleSheet("background:#9aa995;color:white;border-radius:6px;"

                               "padding:4px 8px;font-weight:700;");

    dl->addWidget(statusKey, detailKeys.size(), 0);

    dl->addWidget(statusBadge, detailKeys.size(), 1);

    dl->setColumnStretch(1, 1);

    rl->addWidget(detailFrame);

    // B 给的 status_reason 直接显示（不阻塞：异常目标照常可选中、可导出）

    auto *reason = new QLabel;

    reason->setObjectName("detailKey");

    reason->setWordWrap(true);

    rl->addWidget(reason);

    // 说明性文字：默认收起，点标题才看

    auto *noteLay = makeCollapsible(rl, "单位与字段说明", uiExpand);

    auto *note = new QLabel(

        "未标定：Length/Width/Perimeter 为 px，Area 为 px²\n"

        "AR = Length/Width，Circularity = 4πA/P²，均无量纲\n"

        "Detection / Phenotyping 视图可点击选目标");

    note->setObjectName("detailKey");

    note->setWordWrap(true);

    noteLay->addWidget(note);

    // 底部日志：默认收起，省下的高度给四视图（--ui-expand 时默认展开）

    // 单独包一层 QWidget 才能塞进竖向 QSplitter —— 拖它上面的分割条可改日志高度

    auto *logPanel = new QWidget;

    auto *logPanelLay = new QVBoxLayout(logPanel);

    logPanelLay->setContentsMargins(0, 0, 0, 0);

    logPanelLay->setSpacing(0);

    auto *logBox = makeCollapsible(logPanelLay, "运行日志", uiExpand,

                                   "点标题展开/收起；拖它上面的分割条可改高度");

    auto *log = new QPlainTextEdit;

    log->setReadOnly(true);

    log->setMaximumBlockCount(300);

    log->setMinimumHeight(80);   // 只留下限，上限交给分割条

    logBox->addWidget(log);

    // 主体三栏与日志区之间也是可拖拽的（竖向分割条）

    auto *mainSplit = new QSplitter(Qt::Vertical);

    mainSplit->setChildrenCollapsible(false);

    mainSplit->setHandleWidth(7);

    mainSplit->addWidget(body);

    mainSplit->addWidget(logPanel);

    mainSplit->setStretchFactor(0, 1);

    vl->addWidget(mainSplit, 1);

    mainSplit->setSizes({uiExpand ? 700 : 860, uiExpand ? 180 : 42});

    // ===================== 运行时状态 =====================

    QProcess process;

    QTimer poll, captureWatch;

    poll.setInterval(200);

    captureWatch.setInterval(300);

    QVector<FrameData> frames;        // B 的多帧结果，或 live 的单帧

    int currentFrame = -1;

    QVector<SeedObject> objects;      // = frames[currentFrame].objects（引用语义的浅拷贝）

    int selectedObj = -1;

    int batchSeq = 0;

    bool selectionSync = false;

    bool busy = false;

    bool showLive = false;            // true=显示 live 检测结果；false=显示载入的表型结果

    bool captureDone = false;

    QString loadedJson;

    QString runDir = outDirEdit->text();

    QByteArray output;

    bool stopping = false;

    bool pendingRestart = false;

    QElapsedTimer captureClock;

    ViewState viewStates[4];

    int lastLiveFrame = -1;

    QString lastLiveSignature;

    // 先声明、后赋值：载入/绘制/列表之间互相调用，必须先有名字

    std::function<void()> redraw;

    std::function<void()> fillTable;

    std::function<void()> updateConsistency;

    std::function<void()> updateDetail;

    std::function<void(int)> selectObject;

    std::function<QString(const QString &, int)> exportBatchTo;

    std::function<void()> launch;

    auto append = [&](const QString &s) {

        log->appendPlainText(QTime::currentTime().toString("HH:mm:ss") + "  " + s);

    };

    auto current = [&]() -> FrameData * {

        if (currentFrame < 0 || currentFrame >= frames.size()) return nullptr;

        return &frames[currentFrame];

    };

    // ---------------- 解析 ----------------

    // 单目标：兼容 B 的 bbox_xyxy_src / seed_id 与契约的 bbox

    auto parseObject = [&](const QJsonObject &o, int idx, SeedObject *out) -> QString {

        static const char *required[] = {

            "id", "confidence", "area_px", "length_px", "width_px",

            "perimeter_px", "aspect_ratio", "circularity", "status"};

        for (const char *key : required) {

            if (!o.contains(QLatin1String(key)))

                return QString("objects[%1] 缺少字段 %2").arg(idx).arg(QLatin1String(key));

        }

        const QJsonArray bb = o.contains("bbox_xyxy_src")

            ? o.value("bbox_xyxy_src").toArray() : o.value("bbox").toArray();

        if (bb.size() != 4)

            return QString("objects[%1] 的 bbox 必须为 [x1,y1,x2,y2]"

                           "（B 用 bbox_xyxy_src，契约用 bbox）").arg(idx);

        out->id = o.value("id").toInt();

        out->seedIdText = o.value("seed_id").toString();

        out->confidence = o.value("confidence").toDouble();

        out->bbox = QRectF(bb[0].toDouble(), bb[1].toDouble(),

                           bb[2].toDouble() - bb[0].toDouble(),

                           bb[3].toDouble() - bb[1].toDouble());

        out->areaPx = o.value("area_px").toDouble();

        out->lengthPx = o.value("length_px").toDouble();

        out->widthPx = o.value("width_px").toDouble();

        out->perimeterPx = o.value("perimeter_px").toDouble();

        out->aspectRatio = o.value("aspect_ratio").toDouble();

        out->circularity = o.value("circularity").toDouble();

        out->status = o.value("status").toString();

        out->statusReason = o.value("status_reason").toString();

        out->hasMetrics = true;

        return QString();

    };

    auto parseObjects = [&](const QJsonArray &arr, QVector<SeedObject> *out) -> QString {

        int idx = 0;

        for (const QJsonValue &value : arr) {

            SeedObject s;

            const QString err = parseObject(value.toObject(), idx, &s);

            if (!err.isEmpty()) return err;

            out->append(s);

            ++idx;

        }

        QSet<int> seen;

        for (const SeedObject &s : *out) {

            if (seen.contains(s.id)) return QString("Seed ID 重复: %1").arg(s.id);

            seen.insert(s.id);

        }

        return QString();

    };

    // 载入：把 JSON 变成 frames[]（两种结构都认）

    auto loadJsonPath = [&](const QString &path) -> QString {

        QFile file(path);

        if (!file.open(QIODevice::ReadOnly))

            return "无法读取：" + file.errorString();

        QJsonParseError parse;

        const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &parse);

        if (parse.error != QJsonParseError::NoError || !doc.isObject())

            return "JSON 无效：" + parse.errorString();

        const QJsonObject rootObj = doc.object();

        const QStringList imageDirs = buildImageDirs(path, imagesArg);

        QVector<FrameData> parsedFrames;

        if (rootObj.contains("frames")) {                 // A. B 的原生格式

            for (const QJsonValue &fv : rootObj.value("frames").toArray()) {

                const QJsonObject fo = fv.toObject();

                FrameData fd;

                fd.frameId = fo.value("frame_id").toInt(-1);

                fd.seedCount = fo.contains("seed_count")

                    ? fo.value("seed_count").toInt() : -1;

                const QString err = parseObjects(fo.value("objects").toArray(),

                                                 &fd.objects);

                if (!err.isEmpty()) return "对象结构不符：" + err;

                parsedFrames.append(fd);

            }

            if (parsedFrames.isEmpty())

                return "JSON 的 frames 数组为空";

        } else if (rootObj.contains("objects")) {          // B. C 侧契约

            FrameData fd;

            fd.frameId = rootObj.value("frame_id").toInt(-1);

            fd.seedCount = rootObj.contains("seed_count")

                ? rootObj.value("seed_count").toInt() : -1;

            const QString err = parseObjects(rootObj.value("objects").toArray(),

                                             &fd.objects);

            if (!err.isEmpty()) return "对象结构不符：" + err;

            parsedFrames.append(fd);

        } else {

            return "顶层既没有 frames 也没有 objects（B 的 objects.json 应有 frames）";

        }

        // 每帧配原图与 PL 结果图

        for (FrameData &fd : parsedFrames) {

            const QString orig = frameImagePath(imageDirs, fd.frameId, false);

            const QString fpga = frameImagePath(imageDirs, fd.frameId, true);

            if (!orig.isEmpty()) {

                QImageReader reader(orig);

                reader.setAutoTransform(true);

                fd.original = reader.read();

                fd.imageDir = QFileInfo(orig).absolutePath();

            }

            if (!fpga.isEmpty()) {

                QImageReader reader(fpga);

                reader.setAutoTransform(true);

                fd.fpga = reader.read();

                if (fd.imageDir.isEmpty()) fd.imageDir = QFileInfo(fpga).absolutePath();

            }

        }

        frames = parsedFrames;

        loadedJson = path;

        showLive = false;

        currentFrame = 0;

        frameBox->clear();

        for (const FrameData &fd : frames)

            frameBox->addItem(QString("帧 %1（%2 粒）")

                                  .arg(fd.frameId).arg(fd.objects.size()));

        frameBox->setEnabled(frames.size() > 1);

        frameBox->setCurrentIndex(0);

        objects = frames[0].objects;

        selectedObj = -1;

        fillTable();

        updateConsistency();

        updateDetail();

        if (!objects.isEmpty()) selectObject(0);

        redraw();

        save->setEnabled(true);

        const int nonOk = int(std::count_if(objects.begin(), objects.end(),

            [](const SeedObject &o) { return !o.status.isEmpty() && o.status != "OK"; }));

        append(QString("载入 %1：%2 帧，当前帧 %3 粒（非 OK %4 粒）")

                   .arg(QFileInfo(path).fileName())

                   .arg(frames.size()).arg(objects.size()).arg(nonOk));

        if (!frames[0].imageDir.isEmpty())

            append("帧图像取自：" + frames[0].imageDir);

        else

            append("警告：没找到这一帧的原图/PL 图，视图会留空（可用 --images 指定目录）");

        status->setText(QString("已载入表型结果：%1 · 帧 %2 · Seeds %3 · 单位 px / px²")

                            .arg(QFileInfo(path).fileName())

                            .arg(frames[0].frameId).arg(objects.size()));

        return QString();

    };

    // ---------------- 绘制 ----------------

    auto drawBoxes = [&](const QImage &base, bool exportMode) {

        QImage img = base.convertToFormat(QImage::Format_RGB32);

        if (objects.isEmpty()) return img;

        QPainter p(&img);

        p.setRenderHint(QPainter::Antialiasing, true);

        const int w = img.width();

        // 图上标注按源图坐标画、显示时整体缩放：字号要按缩放后的观感给足

        const int fontPx = qBound(15, w / 36, 34);

        const int thin = qMax(2, w / 400);

        const int thick = qMax(3, w / 200);

        QFont font("Arial");

        font.setPixelSize(fontPx);

        font.setBold(true);

        p.setFont(font);

        QFontMetrics fm(font);

        // 目标多时标签会互相压住，反而看不见选中框：密集场景只给选中项画标签

        const bool dense = objects.size() > 15;

        for (int i = 0; i < objects.size(); ++i) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            p.setPen(QPen(sel ? kSelectColor : color, sel ? thick : thin));

            p.setBrush(Qt::NoBrush);

            p.drawRect(o.bbox);

        }

        // 标签分两轮：先画可省的目标，选中项最后画（永远在最上层）

        QVector<int> order;

        for (int i = 0; i < objects.size(); ++i)

            if (i != selectedObj) order.append(i);

        if (selectedObj >= 0 && selectedObj < objects.size()) order.append(selectedObj);

        QVector<QRectF> used;

        for (int i : order) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            QString text;

            if (sel) {

                text = displayTag(o) + QString("  c=%1").arg(o.confidence, 0, 'f', 2);

            } else if (dense) {

                text = exportMode ? QString("S%1").arg(o.id) : QString();

            } else {

                text = displayTag(o) + QString("  c=%1").arg(o.confidence, 0, 'f', 2);

            }

            if (text.isEmpty()) continue;

            const QRect tr = fm.boundingRect(text).adjusted(-4, -2, 4, 2);

            QRectF bg(o.bbox.x(), o.bbox.y() - tr.height() - 2, tr.width(), tr.height());

            if (bg.top() < 0) bg.moveTop(o.bbox.y() + 2);

            if (bg.x() < 0) bg.moveLeft(1);

            if (bg.right() > w) bg.moveRight(w - 1);

            // 自动避让：与已画标签相交的就不画了（否则字会糊成一片）

            bool clash = false;

            for (const QRectF &u : used)

                if (u.intersects(bg)) { clash = true; break; }

            if (clash && !sel) continue;

            used.append(bg);

            const QColor bgc = sel ? kSelectColor : color;

            p.fillRect(bg, QColor(bgc.red(), bgc.green(), bgc.blue(),

                                  sel || exportMode ? 210 : 150));

            p.setPen(Qt::white);

            p.drawText(bg, Qt::AlignCenter, text);

        }

        return img;

    };

    auto drawPheno = [&](const QImage &base) {

        QImage img = base.convertToFormat(QImage::Format_RGB32);

        if (objects.isEmpty()) return img;

        QPainter p(&img);

        p.setRenderHint(QPainter::Antialiasing, true);

        const FrameData *fd = current();

        const QImage *ref = (fd && !fd->original.isNull()) ? &fd->original : nullptr;

        const double fx = ref ? double(img.width()) / ref->width() : 1.0;

        const double fy = ref ? double(img.height()) / ref->height() : 1.0;

        const int w = img.width();

        // 图上标注按源图坐标画、显示时整体缩放：字号要按缩放后的观感给足

        const int fontPx = qBound(15, w / 36, 34);

        const int thin = qMax(2, w / 400);

        const int thick = qMax(3, w / 200);

        QFont font("Arial");

        font.setPixelSize(fontPx);

        font.setBold(true);

        p.setFont(font);

        QFontMetrics fm(font);

        const bool dense = objects.size() > 15;

        QVector<QRectF> scaled(objects.size());

        for (int i = 0; i < objects.size(); ++i) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            const QRectF box(o.bbox.x() * fx, o.bbox.y() * fy,

                             o.bbox.width() * fx, o.bbox.height() * fy);

            scaled[i] = box;

            p.setPen(QPen(sel ? kSelectColor : color, sel ? thick : thin, Qt::DashLine));

            p.setBrush(Qt::NoBrush);

            p.drawRect(box);

        }

        // 标签分两轮：先画可省的目标，选中项最后画（永远在最上层）

        QVector<int> order;

        for (int i = 0; i < objects.size(); ++i)

            if (i != selectedObj) order.append(i);

        if (selectedObj >= 0 && selectedObj < objects.size()) order.append(selectedObj);

        QVector<QRectF> used;

        for (int i : order) {

            const SeedObject &o = objects[i];

            const bool sel = (i == selectedObj);

            const QColor color = statusColor(o.status);

            QString text;

            if (sel) text = displayTag(o) + QString("  A=%1px²").arg(o.areaPx, 0, 'f', 0);

            else if (!dense) text = displayTag(o);

            if (text.isEmpty()) continue;

            const QRect tr = fm.boundingRect(text).adjusted(-4, -2, 4, 2);

            QRectF bg(scaled[i].x(), scaled[i].y() - tr.height() - 2,

                      tr.width(), tr.height());

            if (bg.top() < 0) bg.moveTop(scaled[i].y() + 2);

            if (bg.x() < 0) bg.moveLeft(1);

            if (bg.right() > w) bg.moveRight(w - 1);

            // 自动避让：与已画标签相交的就不画了（否则字会糊成一片）

            bool clash = false;

            for (const QRectF &u : used)

                if (u.intersects(bg)) { clash = true; break; }

            if (clash && !sel) continue;

            used.append(bg);

            const QColor bgc = sel ? kSelectColor : color;

            p.fillRect(bg, QColor(bgc.red(), bgc.green(), bgc.blue(), sel ? 220 : 150));

            p.setPen(Qt::white);

            p.drawText(bg, Qt::AlignCenter, text);

        }

        // live 链路只出 YOLO 检测框，这里说清楚为什么没有 6 项指标

        const bool anyMetrics = std::any_of(objects.begin(), objects.end(),

            [](const SeedObject &o) { return o.hasMetrics; });

        if (!anyMetrics) {

            const QString hint = "live：只有 YOLO 检测框；6 项表型需载入 B 的结果 JSON";

            QFont hf("Microsoft YaHei UI");

            hf.setPixelSize(qBound(12, w / 42, 22));

            p.setFont(hf);

            const QFontMetrics hfm(hf);

            const int barW = qMin(img.width() - 16,

                                  hfm.horizontalAdvance(hint) + 16);

            const QRectF bar(8, img.height() - hfm.height() - 14, barW,

                             hfm.height() + 8);

            p.setPen(Qt::NoPen);

            p.setBrush(QColor(21, 62, 43, 190));

            p.drawRoundedRect(bar, 5, 5);

            p.setPen(Qt::white);

            p.drawText(bar, Qt::AlignCenter, hint);

        }

        // 选中粒的 6 项表型直接贴在图上（截图即可当作证据）

        if (selectedObj >= 0 && selectedObj < objects.size()) {

            const SeedObject &o = objects[selectedObj];

            QStringList lines{displayTag(o)};

            if (o.hasMetrics) {

                lines << QString("Area        %1 px²").arg(o.areaPx, 0, 'f', 1)

                      << QString("Length      %1 px").arg(o.lengthPx, 0, 'f', 2)

                      << QString("Width       %1 px").arg(o.widthPx, 0, 'f', 2)

                      << QString("Perimeter   %1 px").arg(o.perimeterPx, 0, 'f', 2)

                      << QString("AspectRatio %1 (-)").arg(o.aspectRatio, 0, 'f', 3)

                      << QString("Circularity %1 (-)").arg(o.circularity, 0, 'f', 4)

                      << QString("Status      %1").arg(o.status.isEmpty() ? "--" : o.status);

            } else {

                lines << QString("Conf        %1").arg(o.confidence, 0, 'f', 2)

                      << "live：只有检测框"

                      << "6 项表型需载入 B 的结果 JSON";

            }

            QFont pf("Consolas");

            pf.setPixelSize(qBound(12, w / 42, 22));

            p.setFont(pf);

            const QFontMetrics pfm(pf);

            int textW = 0;

            for (const QString &l : lines)

                textW = qMax(textW, pfm.horizontalAdvance(l));

            const int lh = pfm.height();

            const QRectF panel(8, 8, textW + 16, lh * lines.size() + 10);

            p.setPen(Qt::NoPen);

            p.setBrush(QColor(21, 62, 43, 205));

            p.drawRoundedRect(panel, 6, 6);

            p.setPen(Qt::white);

            for (int k = 0; k < lines.size(); ++k)

                p.drawText(QPointF(panel.x() + 8, panel.y() + 6 + lh * (k + 1) - 4),

                           lines[k]);

        }

        return img;

    };

    auto withHint = [&](const QImage &base, const QString &hint) {

        QImage img = base.convertToFormat(QImage::Format_RGB32);

        QPainter p(&img);

        QFont font("Microsoft YaHei UI");

        font.setPixelSize(qBound(12, img.width() / 40, 32));

        font.setBold(true);

        p.setFont(font);

        p.setPen(QColor(120, 140, 120));

        p.drawText(img.rect(), Qt::AlignCenter, hint);

        return img;

    };

    redraw = [&]() {

        const FrameData *fd = current();

        QImage comp[4];

        if (fd) {

            comp[0] = fd->original;

            comp[1] = fd->fpga;

            if (!comp[0].isNull())

                comp[2] = objects.isEmpty()

                    ? withHint(comp[0], "未载入 Seed 对象") : drawBoxes(comp[0], false);

            if (!comp[1].isNull())

                comp[3] = objects.isEmpty()

                    ? withHint(comp[1], "等待表型结果") : drawPheno(comp[1]);

        }

        for (int i = 0; i < 4; ++i) {

            ClickableLabel *v = viewList[i];

            if (comp[i].isNull()) {

                v->setPixmap(QPixmap());

                v->setText(i == 3 ? "Phenotyping：等在 FPGA 结果图上叠加"

                                  : "等待结果");

                viewStates[i] = ViewState();

                continue;

            }

            const QRect cr = v->contentsRect();

            if (cr.width() < 8 || cr.height() < 8) continue;

            const double sc = qMin(double(cr.width()) / comp[i].width(),

                                   double(cr.height()) / comp[i].height());

            viewStates[i].composite = comp[i];

            viewStates[i].scale = sc;

            viewStates[i].offset = QPointF(

                cr.x() + (cr.width() - comp[i].width() * sc) / 2.0,

                cr.y() + (cr.height() - comp[i].height() * sc) / 2.0);

            v->setPixmap(QPixmap::fromImage(comp[i]).scaled(

                cr.size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));

        }

    };

    // ---------------- 列表 / 详情卡 ----------------

    fillTable = [&]() {

        selectionSync = true;

        table->setRowCount(int(objects.size()));

        for (int i = 0; i < objects.size(); ++i) {

            const SeedObject &o = objects[i];

            QString tag = displayTag(o);

            if (!o.status.isEmpty() && o.status != "OK") tag = "⚠ " + tag;

            auto *c0 = new QTableWidgetItem(tag);

            auto *c1 = new QTableWidgetItem(QString::number(o.confidence, 'f', 2));

            auto *c2 = new QTableWidgetItem(o.hasMetrics

                ? QString::number(o.lengthPx, 'f', 1) : "--");

            auto *c3 = new QTableWidgetItem(o.hasMetrics

                ? QString::number(o.widthPx, 'f', 1) : "--");

            auto *c4 = new QTableWidgetItem(o.hasMetrics

                ? QString::number(o.areaPx, 'f', 0) : "--");

            c0->setForeground(statusColor(o.status));

            for (QTableWidgetItem *it : {c1, c2, c3, c4})

                it->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);

            table->setItem(i, 0, c0);

            table->setItem(i, 1, c1);

            table->setItem(i, 2, c2);

            table->setItem(i, 3, c3);

            table->setItem(i, 4, c4);

        }

        selectionSync = false;

    };

    updateConsistency = [&]() {

        const FrameData *fd = current();

        if (!fd || objects.isEmpty()) {

            consistency->setText("尚未载入对象");

            consistency->setStyleSheet("color:#5c7a63;");

            return;

        }

        const int declared = fd->seedCount < 0 ? int(objects.size()) : fd->seedCount;

        const bool match = (declared == objects.size());

        // C2 要求：Seed Count 必须等于目标列表行数，这里把两个数并排摆出来

        consistency->setText(match ? QString("Seed Count %1 = 列表 %2 · 一致 ✓")

                                         .arg(declared).arg(objects.size())

                                   : QString("⚠ Seed Count %1 ≠ 列表 %2 · 不一致")

                                         .arg(declared).arg(objects.size()));

        consistency->setToolTip(match

            ? QString("Seed Count（结果自带 %1）与列表行数（%2）一致")

                  .arg(declared).arg(objects.size())

            : QString("Seed Count（结果自带 %1）与列表行数（%2）不一致")

                  .arg(declared).arg(objects.size()));

        consistency->setStyleSheet(match ? "color:#1e7d34;font-weight:700;"

                                         : "color:#b3411f;font-weight:700;");

        count->setText(QString::number(objects.size()));

    };

    updateDetail = [&]() {

        if (selectedObj < 0 || selectedObj >= objects.size()) {

            for (QLabel *v : detailValues) v->setText("—");

            statusBadge->setText("无选中");

            statusBadge->setStyleSheet("background:#9aa995;color:white;border-radius:6px;"

                                       "padding:4px 8px;font-weight:700;");

            reason->clear();

            return;

        }

        const SeedObject &o = objects[selectedObj];

        if (!o.hasMetrics) {   // live 检测框：没有表型数值就如实显示 —

            for (QLabel *v : detailValues) v->setText("—");

        } else {

            detailValues[0]->setText(QString::number(o.areaPx, 'f', 1));

            detailValues[1]->setText(QString::number(o.lengthPx, 'f', 2));

            detailValues[2]->setText(QString::number(o.widthPx, 'f', 2));

            detailValues[3]->setText(QString::number(o.perimeterPx, 'f', 2));

            detailValues[4]->setText(QString::number(o.aspectRatio, 'f', 3));

            detailValues[5]->setText(QString::number(o.circularity, 'f', 4));

        }

        const QString st = o.status.isEmpty() ? "--" : o.status;

        statusBadge->setText(st);

        statusBadge->setStyleSheet(

            QString("background:%1;color:white;border-radius:6px;"

                    "padding:4px 8px;font-weight:700;").arg(statusColor(o.status).name()));

        reason->setText(o.statusReason.isEmpty()

                            ? QString() : "原因：" + o.statusReason);

    };

    selectObject = [&](int index) {

        if (index < -1 || index >= objects.size()) return;

        if (selectedObj == index) return;

        selectedObj = index;

        updateDetail();

        if (index >= 0 && !selectionSync) {

            selectionSync = true;

            table->selectRow(index);

            table->scrollToItem(table->item(index, 0));

            selectionSync = false;

        }

        redraw();

    };

    auto switchFrame = [&](int index) {

        if (index < 0 || index >= frames.size()) return;

        currentFrame = index;

        objects = frames[index].objects;

        selectedObj = -1;

        // 程序化切帧（--frame / --export-all）时下拉框也要跟着走，

        // 但不能再触发一次 currentIndexChanged，否则选中项被重置

        if (frameBox->currentIndex() != index) {

            const QSignalBlocker blocker(frameBox);

            frameBox->setCurrentIndex(index);

        }

        fillTable();

        updateConsistency();

        updateDetail();

        if (!objects.isEmpty()) selectObject(0);

        redraw();

        status->setText(QString("已载入表型结果：%1 · 帧 %2 · Seeds %3 · 单位 px / px²")

                            .arg(QFileInfo(loadedJson).fileName())

                            .arg(frames[index].frameId).arg(objects.size()));

    };

    // ---------------- 导出 ----------------

    // 一键导出（界面按钮 / --export / --export-all 共用同一条代码路径）

    exportBatchTo =

        [&](const QString &rootDirPath, int frameIndex) -> QString {

        if (frameIndex < 0 || frameIndex >= frames.size())

            return "没有可导出的帧";

        const FrameData &fd = frames[frameIndex];

        const QString dir = rootDirPath + "/"

            + QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss")

            + QString("_batch_%1").arg(++batchSeq, 3, 10, QChar('0'));

        if (!QDir().mkpath(dir))

            return "无法创建目录：" + dir;

        bool ok = true;

        if (!fd.original.isNull()) ok = fd.original.save(dir + "/original.png") && ok;

        if (!fd.fpga.isNull()) ok = fd.fpga.save(dir + "/fpga.png") && ok;

        if (!fd.original.isNull()) ok = drawBoxes(fd.original, true).save(dir + "/annotated.png") && ok;

        if (!fd.fpga.isNull()) ok = drawPheno(fd.fpga).save(dir + "/phenotyping.png") && ok;

        auto write = [&](const QString &name, const QByteArray &data) {

            QSaveFile file(dir + "/" + name);

            if (!file.open(QIODevice::WriteOnly)) return false;

            if (file.write(data) != data.size()) return false;

            return file.commit();

        };

        QString csv = "seed_id,confidence,length_px,width_px,area_px,"

                      "perimeter_px,aspect_ratio,circularity,status,"

                      "bbox_x1,bbox_y1,bbox_x2,bbox_y2\n";

        for (const SeedObject &o : fd.objects) {

            csv += QString("%1,%2,%3,%4,%5,%6,%7,%8,%9,%10,%11,%12,%13\n")

                       .arg(o.id)

                       .arg(o.confidence, 0, 'f', 4)

                       .arg(o.lengthPx, 0, 'f', 2)

                       .arg(o.widthPx, 0, 'f', 2)

                       .arg(o.areaPx, 0, 'f', 2)

                       .arg(o.perimeterPx, 0, 'f', 2)

                       .arg(o.aspectRatio, 0, 'f', 4)

                       .arg(o.circularity, 0, 'f', 4)

                       .arg(o.status)

                       .arg(o.bbox.x(), 0, 'f', 1)

                       .arg(o.bbox.y(), 0, 'f', 1)

                       .arg(o.bbox.x() + o.bbox.width(), 0, 'f', 1)

                       .arg(o.bbox.y() + o.bbox.height(), 0, 'f', 1);

        }

        ok = write("results.csv", csv.toUtf8()) && ok;

        QJsonArray objectsJson;

        for (const SeedObject &o : fd.objects) {

            QJsonObject j{{"id", o.id},

                          {"confidence", o.confidence},

                          {"bbox", QJsonArray{o.bbox.x(), o.bbox.y(),

                                              o.bbox.x() + o.bbox.width(),

                                              o.bbox.y() + o.bbox.height()}},

                          {"area_px", o.areaPx},

                          {"length_px", o.lengthPx},

                          {"width_px", o.widthPx},

                          {"perimeter_px", o.perimeterPx},

                          {"aspect_ratio", o.aspectRatio},

                          {"circularity", o.circularity},

                          {"status", o.status}};

            if (!o.seedIdText.isEmpty()) j.insert("seed_id", o.seedIdText);

            if (!o.statusReason.isEmpty()) j.insert("status_reason", o.statusReason);

            objectsJson.append(j);

        }

        QJsonObject out{{"schema_version", 1},

                        {"frame_id", fd.frameId},

                        {"seed_count", fd.objects.size()},

                        {"unit", QJsonObject{{"length", "px"}, {"width", "px"},

                                             {"perimeter", "px"}, {"area", "px^2"}}},

                        {"source_json", loadedJson},

                        {"exported_at", QDateTime::currentDateTime().toString(

                             "yyyy-MM-dd HH:mm:ss")},

                        {"images", QJsonObject{{"original", "original.png"},

                                               {"fpga", "fpga.png"},

                                               {"annotated", "annotated.png"},

                                               {"phenotyping", "phenotyping.png"}}},

                        {"objects", objectsJson}};

        ok = write("objects.json", QJsonDocument(out).toJson()) && ok;

        append((ok ? "批次已导出：" : "导出不完整，请检查磁盘：") + dir);

        return ok ? dir : ("导出不完整：" + dir);

    };

    // ---------------- 后端（detection 真链路） ----------------

    auto setBusy = [&](bool value) {

        busy = value;

        for (QWidget *w : QList<QWidget *>{mode, source, sizeBox, framesSpin, host,

                                           python, script, pick, detect, detector,

                                           conf, openJson})

            w->setEnabled(!value);

        start->setEnabled(!value);

        stop->setEnabled(value && !stopping);

        restart->setEnabled(value && !stopping);

        save->setEnabled(!value && !frames.isEmpty());

    };

    auto backendRequest = [&]() {

        QJsonObject req{{"mode", mode->currentText()},

                        {"source", source->currentText()},

                        {"output_dir", runDir},

                        {"width", runWidth},

                        {"height", runHeight},

                        {"frames", framesSpin->value()},

                        {"host", host->text()},

                        {"port", cli.value(optPort).toInt()},

                        {"threshold", 40},

                        {"morph", 1},

                        {"gauss", true},

                        {"detect", detect->isChecked()},

                        {"conf", conf->value() / 100.0},

                        {"detector", detector->currentText()},

                        {"mock_fps", 8.0}};

        const QString inputPath = inputLabel->text() == "（未选择）"

            ? QString() : inputLabel->text();

        if (mode->currentText() == "Batch") req.insert("input_dir", inputPath);

        else if (mode->currentText() == "Image") req.insert("image_path", inputPath);

        else if (!inputPath.isEmpty() && source->currentText() == "mock")

            req.insert("input_dir", inputPath);

        return req;

    };

    launch = [&]() {

        if (process.state() != QProcess::NotRunning) return;

        if (!QFileInfo::exists(script->text())) {

            QMessageBox::warning(&win, "缺少后端", "找不到 " + script->text());

            return;

        }

        QDir().mkpath(runDir);

        QFile::remove(runDir + "/stop.request");

        QFile::remove(runDir + "/live_status.json");

        QSaveFile requestFile(runDir + "/request.json");

        if (!requestFile.open(QIODevice::WriteOnly)

            || requestFile.write(QJsonDocument(backendRequest()).toJson()) < 0

            || !requestFile.commit()) {

            QMessageBox::warning(&win, "文件错误", "请求 JSON 保存失败");

            return;

        }

        showLive = true;

        lastLiveFrame = -1;

        lastLiveSignature.clear();

        output.clear();

        append(QString("启动后端：%1（%2 / %3 / 检测=%4 / 阈值=%5）")

                   .arg(QFileInfo(script->text()).fileName(), mode->currentText(),

                        source->currentText(), detect->isChecked() ? "开" : "关",

                        QString::number(conf->value() / 100.0, 'f', 2)));

        process.setWorkingDirectory(QFileInfo(script->text()).absolutePath());

        process.start(python->text().trimmed(),

                      {QFileInfo(script->text()).absoluteFilePath(),

                       "--request", runDir + "/request.json"});

        poll.start();

        captureClock.restart();

        if (!capturePath.isEmpty()) captureWatch.start();

        setBusy(true);

    };

    auto stopBackend = [&]() {

        if (process.state() == QProcess::NotRunning || stopping) return;

        stopping = true;

        QFile marker(runDir + "/stop.request");

        marker.open(QIODevice::WriteOnly);

        marker.write("stop");

        marker.close();

        process.terminate();

        append("已请求停止");

        setBusy(false);

        stop->setEnabled(false);

        restart->setEnabled(false);

    };

    auto shutdownBackend = [&]() {

        if (process.state() == QProcess::NotRunning) return;

        stopping = true;

        QFile marker(runDir + "/stop.request");

        marker.open(QIODevice::WriteOnly);

        marker.write("stop");

        marker.close();

        process.terminate();

        if (!process.waitForFinished(5000)) {

            process.kill();

            process.waitForFinished(3000);

        }

    };

    // ---------------- 交互 ----------------

    QObject::connect(openJson, &QPushButton::clicked, [&]() {

        const QString path = QFileDialog::getOpenFileName(

            &win, "载入表型结果 JSON", QDir::currentPath(), "JSON (*.json)");

        if (path.isEmpty()) return;

        const QString err = loadJsonPath(path);

        if (!err.isEmpty()) QMessageBox::warning(&win, "载入失败", err);

    });

    QObject::connect(frameBox, QOverload<int>::of(&QComboBox::currentIndexChanged),

                     [&](int index) { switchFrame(index); });

    QObject::connect(table, &QTableWidget::itemSelectionChanged, [&]() {

        if (selectionSync) return;

        const auto rows = table->selectionModel()->selectedRows();

        if (!rows.isEmpty()) selectObject(rows.first().row());

    });

    for (int i = 0; i < viewList.size(); ++i) {

        ClickableLabel *view = viewList[i];

        view->onClicked = [&, i](QPoint pos) {

            if (objects.isEmpty()) return;

            if (i != 2 && i != 3) return;

            const ViewState &vs = viewStates[i];

            if (vs.composite.isNull() || vs.scale <= 0) return;

            const QPointF imgPt = (QPointF(pos) - vs.offset) / vs.scale;

            QPointF origPt = imgPt;

            const FrameData *fd = current();

            if (i == 3 && fd && !fd->original.isNull() && !fd->fpga.isNull()) {

                origPt = QPointF(imgPt.x() * fd->original.width() / double(fd->fpga.width()),

                                 imgPt.y() * fd->original.height() / double(fd->fpga.height()));

            }

            int best = -1;

            double bestArea = 0;

            for (int k = 0; k < objects.size(); ++k) {

                const QRectF &b = objects[k].bbox;

                if (b.contains(origPt)) {

                    const double a = b.width() * b.height();

                    if (best < 0 || a < bestArea) {

                        best = k;

                        bestArea = a;

                    }

                }

            }

            if (best >= 0) {

                selectObject(best);

                append(QString("点击选中 %1（视图 %2）")

                           .arg(displayTag(objects[best])).arg(viewTitles[i]));

            }

        };

    }

    QObject::connect(save, &QPushButton::clicked, [&]() {

        if (currentFrame < 0) return;

        const QString rootDirPath = QFileDialog::getExistingDirectory(

            &win, "选择导出根目录", QDir::currentPath());

        if (rootDirPath.isEmpty()) return;

        const QString out = exportBatchTo(rootDirPath, currentFrame);

        QMessageBox::information(&win,

            out.contains("不") || out.contains("无法") ? "导出提示" : "导出完成", out);

    });

    QObject::connect(start, &QPushButton::clicked, [&]() { launch(); });

    QObject::connect(stop, &QPushButton::clicked, [&]() { stopBackend(); });

    QObject::connect(restart, &QPushButton::clicked, [&]() {

        pendingRestart = true;

        stopBackend();

    });

    QObject::connect(detect, &QCheckBox::toggled, [&](bool on) {

        append(on ? "AI Detection 打开" : "AI Detection 关闭（只跑 FPGA 链路）");

        if (process.state() != QProcess::NotRunning) {

            pendingRestart = true;

            stopBackend();

        }

    });

    QObject::connect(conf, &QSlider::valueChanged, [&](int v) {

        confLabel->setText(QString("置信度阈值 %1").arg(v / 100.0, 0, 'f', 2));

    });

    QObject::connect(pick, &QPushButton::clicked, [&]() {

        QString path;

        if (mode->currentText() == "Batch")

            path = QFileDialog::getExistingDirectory(&win, "选择批量图像目录");

        else if (mode->currentText() == "Image")

            path = QFileDialog::getOpenFileName(

                &win, "选择图片", QString(),

                "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)");

        else

            path = QFileDialog::getExistingDirectory(&win, "选择 Live 回放目录（mock）");

        if (!path.isEmpty()) inputLabel->setText(path);

    });

    QObject::connect(folder, &QPushButton::clicked, [&]() {

        QDesktopServices::openUrl(QUrl::fromLocalFile(runDir));

    });

    QObject::connect(&process, &QProcess::readyReadStandardOutput, [&]() {

        output += process.readAllStandardOutput();

    });

    QObject::connect(&process, &QProcess::readyReadStandardError, [&]() {

        append(QString::fromUtf8(process.readAllStandardError()).trimmed());

    });

    QObject::connect(&process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),

                     [&](int code, QProcess::ExitStatus) {

        poll.stop();

        captureWatch.stop();

        append(QString("后端退出，code=%1").arg(code));

        setBusy(false);

        stopping = false;

        if (pendingRestart) {

            pendingRestart = false;

            QTimer::singleShot(400, &win, launch);

        }

    });

    // live 轮询：只有"显示 live 结果"时才覆盖界面，载入的表型结果不会被冲掉

    QObject::connect(&poll, &QTimer::timeout, [&]() {

        if (!showLive) return;

        QFile file(runDir + "/live_status.json");

        if (!file.open(QIODevice::ReadOnly)) return;

        const QByteArray raw = file.readAll();

        QJsonParseError error;

        const QJsonDocument doc = QJsonDocument::fromJson(raw, &error);

        if (error.error != QJsonParseError::NoError || !doc.isObject()) return;

        const QJsonObject rootObj = doc.object();

        const QJsonObject ai = rootObj.value("ai").toObject();

        const QJsonObject preview = rootObj.value("preview").toObject();

        const QJsonObject latency = rootObj.value("latency").toObject();

        const QJsonObject fps = rootObj.value("fps").toObject();

        const QJsonObject counters = rootObj.value("counters").toObject();

        QJsonValue frameId = rootObj.contains("frame_id") ? rootObj.value("frame_id")

                                                          : rootObj.value("frame");

        lastLiveFrame = frameId.toInt();

        const int seedCount = ai.value("seed_count").toInt();

        QVector<SeedObject> dets;

        for (const QJsonValue &value : ai.value("detections").toArray()) {

            const QJsonObject item = value.toObject();

            const QJsonArray bbox = item.value("bbox_xyxy").toArray();

            if (bbox.size() != 4) continue;

            SeedObject det;

            det.id = item.value("id").toInt();

            det.confidence = item.value("confidence").toDouble();

            det.bbox = QRectF(bbox[0].toDouble(), bbox[1].toDouble(),

                              bbox[2].toDouble() - bbox[0].toDouble(),

                              bbox[3].toDouble() - bbox[1].toDouble());

            det.status = "";            // live 只有检测框，没有表型与状态判定

            det.hasMetrics = false;

            dets.append(det);

        }

        // 列表/表格只在帧号或数量变化时重建，避免每 200ms 重置选中

        const QString signature = QString("%1|%2").arg(lastLiveFrame).arg(dets.size());

        const bool rebuild = signature != lastLiveSignature;

        if (rebuild) {

            lastLiveSignature = signature;

            // live 每帧都会重建列表：记住用户点中的目标 id，重建后按 id 选回来

            const int keepId = (selectedObj >= 0 && selectedObj < objects.size())

                                   ? objects[selectedObj].id : -1;

            FrameData fd;

            fd.frameId = lastLiveFrame;

            fd.seedCount = seedCount;

            fd.objects = dets;

            auto loadImg = [](const QString &p) {

                if (p.isEmpty()) return QImage();

                QImageReader reader(p);

                reader.setAutoTransform(true);

                return reader.read();

            };

            fd.original = loadImg(preview.value("cam").toString());

            fd.fpga = loadImg(preview.value("board").toString());

            frames = QVector<FrameData>{fd};

            currentFrame = 0;

            frameBox->clear();

            frameBox->addItem(QString("live 帧 %1").arg(lastLiveFrame));

            frameBox->setEnabled(false);

            objects = fd.objects;

            selectedObj = -1;

            fillTable();

            updateConsistency();

            bool reselected = false;

            if (keepId >= 0) {

                for (int k = 0; k < objects.size(); ++k) {

                    if (objects[k].id == keepId) {

                        selectObject(k);

                        reselected = true;

                        break;

                    }

                }

            }

            if (!reselected) updateDetail();

        } else if (currentFrame == 0) {

            // 帧没变但图可能是新的

            auto loadImg = [](const QString &p) {

                if (p.isEmpty()) return QImage();

                QImageReader reader(p);

                reader.setAutoTransform(true);

                return reader.read();

            };

            frames[0].original = loadImg(preview.value("cam").toString());

            frames[0].fpga = loadImg(preview.value("board").toString());

        }

        modelState->setText("MODEL " + (ai.value("model_state").toString().isEmpty()

                                            ? "OFFLINE"

                                            : ai.value("model_state").toString()));

        inferLabel->setText("AI 推理 " + QString::number(ai.value("infer_ms").toDouble(), 'f', 3)

                            + " ms" + (ai.value("enabled").toBool(false) ? "" : "（已关闭）"));

        const bool running = rootObj.value("running").toBool();

        status->setText(QString("LIVE | FRAME %1 | CAMERA %2 | FPGA %3 | DMA %4 | MODEL %5 | 检测框 %6")

                            .arg(lastLiveFrame)

                            .arg(rootObj.value("camera").toString(running ? "ONLINE" : "OFFLINE"))

                            .arg(rootObj.value("fpga").toString("--"))

                            .arg(rootObj.value("dma").toString("--"))

                            .arg(ai.value("model_state").toString("--"))

                            .arg(dets.size()));

        const QJsonValue cameraFps = fps.contains("camera") ? fps.value("camera")

                                                            : rootObj.value("camera_fps");

        const QJsonValue liveFps = fps.contains("live") ? fps.value("live")

                                                        : rootObj.value("live_fps");

        metrics->setText(QString("Camera FPS %1 | Live FPS %2 | RTT %3 ms | AI 推理 %4 ms"

                                 " | OK %5 / 超时 %6 / DMA %7")

                             .arg(QString::number(cameraFps.toDouble(), 'f', 1))

                             .arg(QString::number(liveFps.toDouble(), 'f', 1))

                             .arg(QString::number(latency.value("rtt_ms").toDouble(), 'f', 2))

                             .arg(QString::number(ai.value("infer_ms").toDouble(), 'f', 3))

                             .arg(counters.value("ok").toInt())

                             .arg(counters.value("timeout").toInt())

                             .arg(counters.value("dma_err").toInt()));

        redraw();

    });

    // --capture：等"有画面 + 列表与检测数一致"再截（live 证据图用）

    QObject::connect(&captureWatch, &QTimer::timeout, [&]() {

        if (captureDone) return;

        const bool enoughFrames = lastLiveFrame >= 5 || !showLive;

        const bool ready = enoughFrames && !frames.isEmpty()

                           && !frames[0].original.isNull()

                           && table->rowCount() == int(objects.size())

                           && (detect->isChecked() ? !objects.isEmpty() : true);

        const bool backendGone = (process.state() == QProcess::NotRunning);

        if (!ready && !backendGone && captureClock.elapsed() < captureTimeout) return;

        captureDone = true;

        captureWatch.stop();

        redraw();

        const bool saved = win.grab().save(capturePath);

        append((saved ? "已截图：" : "截图失败：") + capturePath

               + QString("（检测框 %1 个）").arg(objects.size()));

        QTimer::singleShot(300, &app, [&, saved]() {

            shutdownBackend();

            app.exit(saved ? 0 : 2);

        });

    });

    QTimer refresh;

    QObject::connect(&refresh, &QTimer::timeout, [&]() { if (!busy) redraw(); });

    refresh.start(400);

    append("就绪。phenotyping：点「载入表型结果 JSON」看 B 的结果（多帧可切换）；"

           "或 Start 跑一次真链路看 YOLO 检测。");

    // ---------------- 启动参数动作 ----------------

    if (!loadPath.isEmpty()) {

        QTimer::singleShot(0, [&]() {

            const QString err = loadJsonPath(loadPath);

            if (!err.isEmpty()) {

                append("启动参数载入失败：" + err);

                return;

            }

            append("启动参数载入：" + loadPath);

            const int wantFrame = cli.value(optFrame).toInt();

            if (wantFrame > 0) {

                if (wantFrame < frames.size()) {

                    switchFrame(wantFrame);

                    append(QString("--frame %1：已切到帧 %2")

                               .arg(wantFrame).arg(frames[wantFrame].frameId));

                } else {

                    append(QString("--frame %1 超出范围（共 %2 帧）")

                               .arg(wantFrame).arg(frames.size()));

                }

            }

        });

    }

    if (!exportDir.isEmpty() || !exportAllDir.isEmpty()) {

        QTimer::singleShot(700, [&]() {

            if (frames.isEmpty()) {

                append("导出跳过：还没有数据（先 --load 一个结果 JSON）");

                return;

            }

            if (!exportAllDir.isEmpty()) {

                for (int i = 0; i < frames.size(); ++i) {

                    switchFrame(i);

                    append(QString("导出帧 %1：%2")

                               .arg(frames[i].frameId)

                               .arg(exportBatchTo(exportAllDir, i)));

                }

            } else {

                append("自动导出：" + exportBatchTo(exportDir, currentFrame));

            }

        });

    }

    // --click-seed / --select-row 自检：等数据到位再合成动作（live 要等第一帧）

    auto runWhenReady = [&](const QString &what, std::function<bool()> action) {

        auto *timer = new QTimer(&win);

        auto *attempts = new int(0);

        timer->setInterval(300);

        QObject::connect(timer, &QTimer::timeout, [&, timer, attempts, what, action]() {

            if (objects.isEmpty()) {

                if (++(*attempts) > 25) {

                    append(what + "：等不到目标数据，跳过");

                    timer->stop();

                }

                return;

            }

            timer->stop();

            if (!action()) append(what + "：找不到指定 Seed ID");

        });

        timer->start();

    };

    const QString clickSeed = cli.value(optClickSeed);

    if (!clickSeed.isEmpty()) {

        runWhenReady("--click-seed " + clickSeed, [&, clickSeed]() {

            const int id = clickSeed.toInt();

            int idx = -1;

            for (int k = 0; k < objects.size(); ++k)

                if (objects[k].id == id) idx = k;

            if (idx < 0) return false;

            const ViewState &vs = viewStates[2];

            const QPoint pos = (objects[idx].bbox.center() * vs.scale + vs.offset).toPoint();

            selectObject(-1);

            viewList[2]->onClicked(pos);

            append(QString("--click-seed %1：合成点击 (%2,%3) -> 选中 %4")

                       .arg(id).arg(pos.x()).arg(pos.y())

                       .arg(selectedObj >= 0 ? displayTag(objects[selectedObj])

                                             : QString("无")));

            return true;

        });

    }

    const QString selectRow = cli.value(optSelectRow);

    if (!selectRow.isEmpty()) {

        runWhenReady("--select-row " + selectRow, [&, selectRow]() {

            const int id = selectRow.toInt();

            int idx = -1;

            for (int k = 0; k < objects.size(); ++k)

                if (objects[k].id == id) idx = k;

            if (idx < 0) return false;

            selectObject(-1);

            table->selectRow(idx);

            append(QString("--select-row %1：选中第 %2 行 -> 详情卡 %3")

                       .arg(id).arg(idx)

                       .arg(selectedObj >= 0 ? displayTag(objects[selectedObj])

                                             : QString("无")));

            return true;

        });

    }

    if (!shotPath.isEmpty()) {

        QTimer::singleShot(1500, [&]() {

            append(win.grab().save(shotPath) ? "窗口截图：" + shotPath : "窗口截图失败");

            if (cli.isSet(optQuit)) QApplication::quit();

        });

    } else if (cli.isSet(optQuit)) {

        QTimer::singleShot(2500, []() { QApplication::quit(); });

    }

    win.show();

    if (autoStart) QTimer::singleShot(600, &win, launch);

    QObject::connect(&app, &QCoreApplication::aboutToQuit, [&]() { shutdownBackend(); });

    return app.exec();

}

