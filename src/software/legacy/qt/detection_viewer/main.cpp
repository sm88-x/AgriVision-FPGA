// AgriVision Edge · Day 4 —— Qt 界面（AI 检测叠加 + 目标列表）

//

// 架构与 live_stream 的 agri_morph_gui_3 一致：Qt 只做界面与交互，

// 通过 QProcess 拉起 Python 后端（detection_adapter.py），每 200ms 轮询

// 后端写的 live_status.json 刷新画面。detection 新增：

//   · AI Detection 开关 + 置信度阈值

//   · bbox / 序号 / 置信度叠加（按原始像素 → 控件像素正确映射）

//   · 侧栏 Seed Count + 目标列表（点条目高亮对应框）

//   · MODEL READY/LOADING、推理延迟、Camera FPS、Live FPS 卡片

//   · --capture：离屏渲染截一张界面图后退出（用于产出工程证据图）

#include <QtWidgets>

struct Detection {

    int id = 0;

    double x1 = 0, y1 = 0, x2 = 0, y2 = 0, conf = 0;

};

static QString num(const QJsonValue &v, int decimals = 1)

{

    if (!v.isDouble())

        return "--";

    return QString::number(v.toDouble(), 'f', decimals);

}

int main(int argc, char *argv[])

{

    QApplication app(argc, argv);

    app.setStyle("Fusion");

    app.setFont(QFont("Microsoft YaHei UI", 10));

    QCommandLineParser cli;

    cli.setApplicationDescription("AgriVision Edge detection Qt 界面");

    cli.addHelpOption();

    QCommandLineOption optCapture("capture", "截图到指定 PNG 后退出", "png");

    QCommandLineOption optCaptureTimeout("capture-timeout", "截图等待上限(ms)", "ms", "30000");

    QCommandLineOption optAutoStart("auto-start", "启动后自动开始（截图/无人值守用）");

    QCommandLineOption optMode("mode", "Live/Image/Batch", "mode", "Live");

    QCommandLineOption optSource("source", "board(真链路) / mock(不开相机不占板)", "src", "mock");

    QCommandLineOption optInput("input", "Image 图片或 Batch 目录", "path");

    QCommandLineOption optDetect("detect", "是否开启 AI 检测", "0/1", "1");

    QCommandLineOption optConf("conf", "置信度阈值", "float", "0.25");

    QCommandLineOption optDetector("detector", "mock / auto", "d", "mock");

    QCommandLineOption optFrames("frames", "Live 帧数(0=持续)", "n", "0");

    QCommandLineOption optBackend("backend", "后端适配器脚本", "py");

    QCommandLineOption optPython("python", "Python 解释器", "exe", "python");

    QCommandLineOption optHost("host", "板卡 IP", "ip", "192.168.2.99");

    QCommandLineOption optPort("port", "板卡端口", "p", "9000");

    QCommandLineOption optSize("size", "分辨率 WxH", "size", "640x480");

    QCommandLineOption optOutDir("out-dir", "本次证据目录", "dir");

    cli.addOptions({optCapture, optCaptureTimeout, optAutoStart, optMode, optSource,

                    optInput, optDetect, optConf, optDetector, optFrames, optBackend,

                    optPython, optHost, optPort, optSize, optOutDir});

    cli.process(app);

    const QString capturePath = cli.value(optCapture);

    const int captureTimeout = cli.value(optCaptureTimeout).toInt();

    const bool autoStart = cli.isSet(optAutoStart) || !capturePath.isEmpty();

    const QString sizeText = cli.value(optSize);

    const int width = sizeText.section('x', 0, 0).toInt();

    const int height = sizeText.section('x', 1, 1).toInt();

    const QString appDir = QCoreApplication::applicationDirPath();

    const QString repoGuess = QDir(appDir).absoluteFilePath("../../..");

    const QString backendPath = cli.value(optBackend).isEmpty()

        ? QDir(repoGuess).absoluteFilePath("src/software/adapters/detection_adapter.py")

        : cli.value(optBackend);

    const QString outDir = cli.value(optOutDir).isEmpty()

        ? QDir(appDir).absoluteFilePath("detection_run") : cli.value(optOutDir);

    QMainWindow win;

    win.setWindowTitle("AgriVision Edge · Day 4 · YOLO Seed Detection + Qt");

    win.resize(1560, 940);

    win.setStyleSheet(R"(

        QMainWindow { background:#f3f6ee; }

        QWidget { color:#214630; }

        QFrame#header { background:#153e2b; border-radius:9px; }

        QFrame#header QLabel { color:white; font-size:20px; font-weight:700; }

        QFrame#card { background:white; border:1px solid #d8e3cf; border-radius:10px; }

        QFrame#ai { background:#eef7ea; border:1px solid #bcd8ae; border-radius:10px; }

        QLabel#view { background:#edf3e6; border:1px dashed #bfd1b1;

                      border-radius:8px; color:#7f9871; }

        QLabel#big { font-size:22px; font-weight:700; color:#2c6b3f; }

        QPushButton { background:#e7f0dc; padding:8px; border:1px solid #c7d9b8;

                      border-radius:6px; }

        QPushButton:hover { background:#d8e9c6; }

        QPushButton#start { background:#387342; color:white; }

        QPushButton:disabled { color:#9aab90; background:#e9eee2; }

        QLineEdit,QComboBox,QSpinBox { background:white; padding:5px;

                      border:1px solid #ccdaba; border-radius:5px; }

        QPlainTextEdit { background:#f9fbf6; border:1px solid #d9e4cf; }

        QListWidget { background:white; border:1px solid #d8e3cf; border-radius:6px; }

    )");

    auto *root = new QWidget;

    win.setCentralWidget(root);

    auto *vl = new QVBoxLayout(root);

    vl->setContentsMargins(18, 16, 18, 16);

    auto *header = new QFrame;

    header->setObjectName("header");

    auto *head = new QHBoxLayout(header);

    head->addWidget(new QLabel("AGRIVISION EDGE"));

    head->addStretch();

    head->addWidget(new QLabel("DAY 4 · CAMERA → PL V4 → PC · YOLO SEED DETECTION → Qt"));

    vl->addWidget(header);

    auto *status = new QLabel("CAMERA -- | FPGA -- | MODEL -- | DMA --");

    status->setWordWrap(true);

    vl->addWidget(status);

    auto *metrics = new QLabel("Camera FPS -- | Live FPS -- | RTT -- | AI 推理 --");

    metrics->setWordWrap(true);

    vl->addWidget(metrics);

    auto *body = new QHBoxLayout;

    vl->addLayout(body, 1);

    // ---------------- 左侧控制区 ----------------

    auto *left = new QFrame;

    left->setObjectName("card");

    left->setFixedWidth(330);

    auto *pl = new QVBoxLayout(left);

    auto field = [&](const QString &text, QWidget *w) {

        pl->addWidget(new QLabel(text));

        pl->addWidget(w);

    };

    auto *mode = new QComboBox;

    mode->addItems({"Live", "Image", "Batch"});

    mode->setCurrentText(cli.value(optMode));

    field("模式", mode);

    auto *source = new QComboBox;

    source->addItems({"mock", "board"});

    source->setCurrentText(cli.value(optSource));

    field("数据源（mock=不开相机不占板）", source);

    auto *sizeBox = new QComboBox;

    sizeBox->addItems({"640x480", "320x240", "256x256"});

    sizeBox->setCurrentText(sizeText);

    field("分辨率", sizeBox);

    auto *frames = new QSpinBox;

    frames->setRange(0, 100000);

    frames->setValue(cli.value(optFrames).toInt());

    frames->setSpecialValueText("持续运行，直到 Stop");

    field("Live 帧数（0=持续）", frames);

    auto *host = new QLineEdit(cli.value(optHost));

    field("板卡 IP · TCP 端口 " + cli.value(optPort), host);

    auto *python = new QLineEdit(cli.value(optPython));

    field("Python 程序或完整路径", python);

    auto *script = new QLineEdit(backendPath);

    field("后端脚本（detection_adapter.py）", script);

    auto *outDirEdit = new QLineEdit(outDir);

    field("本次证据目录", outDirEdit);

    // ---- AI 检测区 ----

    auto *aiBox = new QFrame;

    aiBox->setObjectName("ai");

    auto *aiLayout = new QVBoxLayout(aiBox);

    auto *detect = new QCheckBox("AI Detection（YOLO 单类 seed）");

    detect->setChecked(cli.value(optDetect) != "0");

    aiLayout->addWidget(detect);

    auto *detector = new QComboBox;

    detector->addItems({"mock", "auto"});

    detector->setCurrentText(cli.value(optDetector));

    aiLayout->addWidget(new QLabel("检测器（auto=接 B 的 infer_seed.py）"));

    aiLayout->addWidget(detector);

    auto *confLabel = new QLabel;

    auto *conf = new QSlider(Qt::Horizontal);

    conf->setRange(5, 95);

    conf->setValue(int(cli.value(optConf).toDouble() * 100));

    confLabel->setText(QString("置信度阈值 %1").arg(conf->value() / 100.0, 0, 'f', 2));

    aiLayout->addWidget(confLabel);

    aiLayout->addWidget(conf);

    pl->addWidget(aiBox);

    auto *pick = new QPushButton("选择 Image / Batch 输入");

    auto *inputLabel = new QLabel(cli.value(optInput).isEmpty() ? "（未选择）"

                                                                : cli.value(optInput));

    inputLabel->setWordWrap(true);

    pl->addWidget(pick);

    pl->addWidget(inputLabel);

    auto *start = new QPushButton("Start");

    start->setObjectName("start");

    auto *stop = new QPushButton("Stop");

    auto *restart = new QPushButton("Restart");

    stop->setEnabled(false);

    restart->setEnabled(false);

    pl->addWidget(start);

    pl->addWidget(stop);

    pl->addWidget(restart);

    auto *folder = new QPushButton("打开本次证据目录");

    pl->addWidget(folder);

    pl->addStretch();

    body->addWidget(left);

    // ---------------- 中间三视图 ----------------

    auto *middle = new QHBoxLayout;

    body->addLayout(middle, 1);

    QList<QLabel*> views;

    for (const QString &title : QStringList{"Original · 原图（YOLO 输入）",

                                            "FPGA Result · PL 处理",

                                            "Annotated · 检测框叠加"}) {

        auto *card = new QFrame;

        card->setObjectName("card");

        auto *cl = new QVBoxLayout(card);

        cl->addWidget(new QLabel(title));

        auto *view = new QLabel("等待图像");

        view->setObjectName("view");

        view->setMinimumSize(300, 240);

        view->setAlignment(Qt::AlignCenter);

        cl->addWidget(view, 1);

        middle->addWidget(card, 1);

        views.append(view);

    }

    // ---------------- 右侧计数与列表 ----------------

    auto *right = new QFrame;

    right->setObjectName("card");

    right->setFixedWidth(300);

    auto *rl = new QVBoxLayout(right);

    rl->addWidget(new QLabel("SEED COUNT"));

    auto *count = new QLabel("0");

    count->setObjectName("big");

    rl->addWidget(count);

    auto *modelState = new QLabel("MODEL --");

    auto *inferLabel = new QLabel("AI 推理 -- ms");

    rl->addWidget(modelState);

    rl->addWidget(inferLabel);

    rl->addWidget(new QLabel("目标列表（点条目高亮对应框）"));

    auto *list = new QListWidget;

    rl->addWidget(list, 1);

    body->addWidget(right);

    auto *log = new QPlainTextEdit;

    log->setReadOnly(true);

    log->setMaximumBlockCount(400);

    log->setFixedHeight(130);

    vl->addWidget(log);

    // ---------------- 状态与逻辑 ----------------

    QProcess process;

    QTimer poll, captureWatch;

    poll.setInterval(200);

    captureWatch.setInterval(250);

    QString inputPath = cli.value(optInput);

    QString runDir = outDirEdit->text();

    QByteArray output;

    bool stopping = false;

    bool pendingRestart = false;

    bool captureDone = false;

    QElapsedTimer captureClock;

    QList<QImage> images = {QImage(), QImage(), QImage()};

    QList<Detection> detections;

    int highlightId = -1;

    QString lastAiSignature;

    int lastFrameId = -1;            // 截图判据用：太早截会拍到还没出图的空界面

    bool lastAiEnabled = false;

    auto append = [&](const QString &s) {

        log->appendPlainText(QTime::currentTime().toString("HH:mm:ss") + "  " + s);

    };

    // 把原图像素坐标映射到控件像素坐标后画框（缩放下也必须对齐）

    auto paint = [&](int index, const QPixmap &base) {

        QPixmap pm = base;

        if (images[index].isNull() || detections.isEmpty())

            return pm;

        QPainter painter(&pm);

        painter.setRenderHint(QPainter::Antialiasing, true);

        const double sx = double(pm.width()) / double(images[index].width());

        const double sy = double(pm.height()) / double(images[index].height());

        for (const Detection &d : detections) {

            const bool hot = (d.id == highlightId);

            QPen pen(hot ? QColor(230, 140, 30) : QColor(60, 160, 70));

            pen.setWidthF(hot ? 4.0 : 2.0);

            painter.setPen(pen);

            const QRectF box(d.x1 * sx, d.y1 * sy,

                             (d.x2 - d.x1) * sx, (d.y2 - d.y1) * sy);

            painter.drawRect(box);

            const QString label = QString("#%1 %2").arg(d.id + 1).arg(d.conf, 0, 'f', 2);

            QFont f = painter.font();

            f.setPointSizeF(hot ? 10.5 : 9.0);

            painter.setFont(f);

            const QFontMetricsF metrics(f);

            const QRectF textSize = metrics.boundingRect(label);

            const QRectF textBox(box.topLeft(),

                                 QSizeF(textSize.width() + 8, textSize.height() + 4));

            painter.fillRect(textBox, hot ? QColor(230, 140, 30) : QColor(60, 160, 70));

            painter.setPen(Qt::white);

            painter.drawText(textBox, Qt::AlignCenter, label);

        }

        return pm;

    };

    auto redraw = [&]() {

        for (int i = 0; i < views.size(); ++i) {

            if (images[i].isNull())

                continue;

            const QSize target = views[i]->size() - QSize(12, 12);

            QPixmap base = QPixmap::fromImage(images[i]).scaled(

                target, Qt::KeepAspectRatio, Qt::SmoothTransformation);

            views[i]->setPixmap(i == 2 ? paint(2, base) : base);

        }

    };

    auto load = [&](int index, const QString &path) {

        if (path.isEmpty())

            return;

        QImage image(path);

        if (!image.isNull())

            images[index] = image;

    };

    auto setBusy = [&](bool busy) {

        for (QWidget *w : QList<QWidget*>{mode, source, sizeBox, frames, host, python,

                                          script, pick, detect, detector, conf})

            w->setEnabled(!busy);

        start->setEnabled(!busy);

        stop->setEnabled(busy && !stopping);

        restart->setEnabled(busy && !stopping);

    };

    auto backendRequest = [&]() {

        QJsonObject req{

            {"mode", mode->currentText()},

            {"source", source->currentText()},

            {"output_dir", runDir},

            {"width", width},

            {"height", height},

            {"frames", frames->value()},

            {"host", host->text()},

            {"port", cli.value(optPort).toInt()},

            {"threshold", 40},

            {"morph", 1},

            {"gauss", true},

            {"detect", detect->isChecked()},

            {"conf", conf->value() / 100.0},

            {"detector", detector->currentText()},

            {"mock_fps", 8.0},

        };

        if (mode->currentText() == "Batch")

            req.insert("input_dir", inputPath);

        else if (mode->currentText() == "Image")

            req.insert("image_path", inputPath);

        else if (!inputPath.isEmpty() && source->currentText() == "mock")

            req.insert("input_dir", inputPath);

        return req;

    };

    std::function<void()> launch;

    launch = [&]() {

        if (process.state() != QProcess::NotRunning)

            return;

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

        images = {QImage(), QImage(), QImage()};

        detections.clear();

        highlightId = -1;

        list->clear();

        count->setText("0");

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

        if (!capturePath.isEmpty())

            captureWatch.start();

        setBusy(true);

    };

    auto stopBackend = [&]() {

        if (process.state() == QProcess::NotRunning || stopping)

            return;

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

    // 退出时必须把后端子进程一起收掉：否则相机被占、板卡连接也不释放

    auto shutdownBackend = [&]() {

        if (process.state() == QProcess::NotRunning)

            return;

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

    QObject::connect(list, &QListWidget::currentRowChanged, [&](int row) {

        highlightId = (row >= 0 && row < detections.size()) ? row : -1;

        redraw();

    });

    QObject::connect(pick, &QPushButton::clicked, [&]() {

        if (mode->currentText() == "Batch")

            inputPath = QFileDialog::getExistingDirectory(&win, "选择批量图像目录");

        else if (mode->currentText() == "Image")

            inputPath = QFileDialog::getOpenFileName(

                &win, "选择图片", QString(),

                "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)");

        else

            inputPath = QFileDialog::getExistingDirectory(&win, "选择 Live 回放目录（mock）");

        inputLabel->setText(inputPath.isEmpty() ? "（未选择）" : inputPath);

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

    QObject::connect(&poll, &QTimer::timeout, [&]() {

        QFile file(runDir + "/live_status.json");

        if (!file.open(QIODevice::ReadOnly))

            return;

        const QByteArray raw = file.readAll();

        QJsonParseError error;

        const QJsonDocument doc = QJsonDocument::fromJson(raw, &error);

        if (error.error != QJsonParseError::NoError || !doc.isObject())

            return;

        const QJsonObject root = doc.object();

        const QJsonObject ai = root.value("ai").toObject();

        const QJsonObject preview = root.value("preview").toObject();

        const QJsonObject latency = root.value("latency").toObject();

        lastAiEnabled = ai.value("enabled").toBool(detect->isChecked());

        load(0, preview.value("cam").toString());

        load(1, preview.value("board").toString());

        const QString annot = ai.value("annot").toString().isEmpty()

            ? preview.value("annot").toString() : ai.value("annot").toString();

        load(2, annot);

        detections.clear();

        for (const QJsonValue &value : ai.value("detections").toArray()) {

            const QJsonObject item = value.toObject();

            const QJsonArray bbox = item.value("bbox_xyxy").toArray();

            if (bbox.size() != 4)

                continue;

            Detection det;

            det.id = item.value("id").toInt();

            det.x1 = bbox.at(0).toDouble();

            det.y1 = bbox.at(1).toDouble();

            det.x2 = bbox.at(2).toDouble();

            det.y2 = bbox.at(3).toDouble();

            det.conf = item.value("confidence").toDouble();

            detections.append(det);

        }

        // 列表与计数严格同源，避免"数量与条数不一致"

        const QString signature = QString("%1|%2|%3")

            .arg(ai.value("seed_count").toInt())

            .arg(detections.size())

            .arg(highlightId);

        if (signature != lastAiSignature) {

            lastAiSignature = signature;

            if (list->count() != detections.size()) {

                const int keep = highlightId;

                list->clear();

                for (const Detection &d : detections) {

                    list->addItem(QString("#%1  conf %2   [%3, %4, %5, %6]")

                                      .arg(d.id + 1)

                                      .arg(d.conf, 0, 'f', 2)

                                      .arg(int(d.x1)).arg(int(d.y1))

                                      .arg(int(d.x2)).arg(int(d.y2)));

                }

                if (keep >= 0 && keep < list->count())

                    list->setCurrentRow(keep);

            }

        }

        count->setText(QString::number(detections.size()));

        modelState->setText("MODEL " + (ai.value("model_state").toString().isEmpty()

                                            ? "OFFLINE" : ai.value("model_state").toString()));

        inferLabel->setText("AI 推理 " + num(ai.value("infer_ms"), 3) + " ms"

                            + (ai.value("enabled").toBool(false) ? "" : "（已关闭）"));

        const bool running = root.value("running").toBool();

        // 真链路（B 的后端）用 frame_id / fps{camera,live} / camera+fpga+dma；

        // mock 数据源用 frame / camera_fps / live_fps —— 两种都兼容，不写死一种

        const QJsonObject fps = root.value("fps").toObject();

        const QJsonObject counters = root.value("counters").toObject();

        const QJsonValue frameId = root.contains("frame_id") ? root.value("frame_id")

                                                             : root.value("frame");

        lastFrameId = frameId.toInt();

        const QJsonValue cameraFps = fps.contains("camera") ? fps.value("camera")

                                                            : root.value("camera_fps");

        const QJsonValue liveFps = fps.contains("live") ? fps.value("live")

                                                        : root.value("live_fps");

        status->setText(QString("FRAME %1 | CAMERA %2 | FPGA %3 | DMA %4 | MODEL %5 | 检测框 %6")

                            .arg(frameId.toInt())

                            .arg(root.value("camera").toString(

                                     running ? "ONLINE" : "OFFLINE"))

                            .arg(root.value("fpga").toString("--"))

                            .arg(root.value("dma").toString("--"))

                            .arg(ai.value("model_state").toString("--"))

                            .arg(detections.size()));

        metrics->setText(QString("Camera FPS %1 | Live FPS %2 | RTT %3 ms | AI 推理 %4 ms"

                                 " | OK %5 / 超时 %6 / DMA %7")

                             .arg(num(cameraFps))

                             .arg(num(liveFps))

                             .arg(num(latency.value("rtt_ms"), 2))

                             .arg(num(ai.value("infer_ms"), 3))

                             .arg(counters.value("ok").toInt())

                             .arg(counters.value("timeout").toInt())

                             .arg(counters.value("dma_err").toInt()));

        redraw();

    });

    QObject::connect(&captureWatch, &QTimer::timeout, [&]() {

        if (captureDone)

            return;

        // 只要求"有画面且列表与检测数一致"；检测关闭时没有框也要能截（对比用）

        const bool enoughFrames = lastFrameId >= 5;      // 前几帧界面还是空的，别急着截

        const bool boxesOk = !lastAiEnabled || !detections.isEmpty();

        const bool ready = enoughFrames && boxesOk && !images[2].isNull()

                           && list->count() == detections.size();

        const bool backendGone = (process.state() == QProcess::NotRunning);

        if (!ready && !backendGone && captureClock.elapsed() < captureTimeout)

            return;

        captureDone = true;

        captureWatch.stop();

        redraw();

        const bool saved = win.grab().save(capturePath);

        append((saved ? "已截图：" : "截图失败：") + capturePath

               + QString("（检测框 %1 个）").arg(detections.size()));

        QTimer::singleShot(300, &app, [&, saved]() {

            shutdownBackend();          // 截图模式下也要把后端收掉再退

            app.exit(saved ? 0 : 2);

        });

    });

    win.show();

    if (autoStart)

        QTimer::singleShot(600, &win, launch);

    QObject::connect(&app, &QCoreApplication::aboutToQuit, [&]() { shutdownBackend(); });

    return app.exec();

}

