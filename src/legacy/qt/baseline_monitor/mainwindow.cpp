#include "mainwindow.h"

#include <QApplication>

#include <QWidget>

#include <QHBoxLayout>

#include <QVBoxLayout>

#include <QGridLayout>

#include <QFrame>

#include <QFileDialog>

#include <QMessageBox>

#include <QScrollArea>

#include <QResizeEvent>

#include <QFileInfo>

#include <QGraphicsDropShadowEffect>

#include <QCoreApplication>

#include <QDir>

#include <QJsonDocument>

#include <QJsonObject>

#include <QProcessEnvironment>

#include <QSaveFile>

#include <QStandardPaths>

#include <QUuid>

static QLabel *makeLabel(

    const QString &text,

    const QString &objectName = "")

{

    QLabel *label = new QLabel(text);

    if (!objectName.isEmpty())

        label->setObjectName(objectName);

    return label;

}

static QFrame *makeCard()

{

    QFrame *card = new QFrame;

    card->setObjectName("card");

    return card;

}

MainWindow::MainWindow(QWidget *parent)

    : QMainWindow(parent)

{

    setWindowTitle(

        "AgriVision Edge | Agricultural Vision Intelligence"

        );

    resize(1480, 900);

    setMinimumSize(1100, 720);

    QWidget *central = new QWidget;

    setCentralWidget(central);

    QVBoxLayout *root = new QVBoxLayout(central);

    root->setContentsMargins(0, 0, 0, 0);

    root->setSpacing(0);

    // ---------- Top navigation ----------

    QFrame *header = new QFrame;

    header->setObjectName("header");

    header->setFixedHeight(82);

    QHBoxLayout *headerLayout = new QHBoxLayout(header);

    headerLayout->setContentsMargins(30, 12, 30, 12);

    QVBoxLayout *brandLayout = new QVBoxLayout;

    brandLayout->setSpacing(2);

    QLabel *brand = makeLabel(

        "◈  AGRIVISION EDGE", "brand"

        );

    QLabel *tagline = makeLabel(

        "INTELLIGENT AGRICULTURAL PHENOTYPING",

        "tagline"

        );

    brandLayout->addWidget(brand);

    brandLayout->addWidget(tagline);

    QLabel *version = makeLabel(

        "MORPHOLOGY V4  /  FPGA ACCELERATED",

        "version"

        );

    fpgaStatus = makeLabel(

        "● FPGA OFFLINE", "offlineBadge"

        );

    headerLayout->addLayout(brandLayout);

    headerLayout->addStretch();

    headerLayout->addWidget(version);

    headerLayout->addSpacing(24);

    headerLayout->addWidget(fpgaStatus);

    root->addWidget(header);

    // ---------- Main area ----------

    QHBoxLayout *body = new QHBoxLayout;

    body->setContentsMargins(0, 0, 0, 0);

    body->setSpacing(0);

    body->addWidget(createSidebar());

    QScrollArea *scroll = new QScrollArea;

    scroll->setWidgetResizable(true);

    scroll->setFrameShape(QFrame::NoFrame);

    scroll->setObjectName("mainScroll");

    scroll->setWidget(createDashboard());

    body->addWidget(scroll, 1);

    root->addLayout(body, 1);

    applyStyle();

    backendProcess = new QProcess(this);

    connect(backendProcess, &QProcess::readyReadStandardOutput, this, [this]() {

        backendOutput += backendProcess->readAllStandardOutput();

    });

    connect(backendProcess, &QProcess::readyReadStandardError, this, [this]() {

        const QString details = QString::fromUtf8(backendProcess->readAllStandardError()).trimmed();

        if (!details.isEmpty())

            statusBar()->showMessage(details.left(220));

    });

    connect(backendProcess,

            QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished),

            this, &MainWindow::processFinished);

    connect(backendProcess, &QProcess::errorOccurred,

            this, &MainWindow::processError);

}

QWidget *MainWindow::createSidebar()

{

    QFrame *sidebar = new QFrame;

    sidebar->setObjectName("sidebar");

    sidebar->setFixedWidth(300);

    QVBoxLayout *layout = new QVBoxLayout(sidebar);

    layout->setContentsMargins(22, 28, 22, 22);

    layout->setSpacing(13);

    layout->addWidget(

        makeLabel("CONTROL CENTER", "sectionTitle")

        );

    layout->addWidget(

        makeLabel("图像处理参数配置", "muted")

        );

    layout->addSpacing(12);

    // Image import

    layout->addWidget(

        makeLabel("01   IMAGE SOURCE", "sideHeading")

        );

    QPushButton *importButton =

        new QPushButton("＋  导入农业图像");

    importButton->setObjectName("secondaryButton");

    importButton->setMinimumHeight(45);

    layout->addWidget(importButton);

    fileLabel = makeLabel("尚未选择图像", "muted");

    fileLabel->setWordWrap(true);

    layout->addWidget(fileLabel);

    connect(importButton, &QPushButton::clicked,

            this, &MainWindow::importImage);

    layout->addSpacing(15);

    // Edge detection

    layout->addWidget(

        makeLabel("02   EDGE DETECTION", "sideHeading")

        );

    edgeCombo = new QComboBox;

    edgeCombo->addItems({

        "Sobel",

        "Prewitt"

    });

    layout->addWidget(edgeCombo);

    layout->addSpacing(12);

    // Threshold

    layout->addWidget(

        makeLabel("03   THRESHOLD", "sideHeading")

        );

    QHBoxLayout *thresholdRow = new QHBoxLayout;

    thresholdRow->addWidget(makeLabel("Binary Threshold"));

    thresholdValue = makeLabel("128", "accentValue");

    thresholdRow->addStretch();

    thresholdRow->addWidget(thresholdValue);

    layout->addLayout(thresholdRow);

    thresholdSlider = new QSlider(Qt::Horizontal);

    thresholdSlider->setRange(0, 255);

    thresholdSlider->setValue(128);

    layout->addWidget(thresholdSlider);

    connect(thresholdSlider, &QSlider::valueChanged,

            this, [this](int value) {

                thresholdValue->setText(QString::number(value));

            });

    layout->addSpacing(12);

    // Morphology

    layout->addWidget(

        makeLabel("04   MORPHOLOGY", "sideHeading")

        );

    morphCombo = new QComboBox;

    morphCombo->addItems({

        "Off / Bypass",

        "Erode  ·  腐蚀",

        "Dilate  ·  膨胀"

    });

    layout->addWidget(morphCombo);

    QLabel *morphHelp = makeLabel(

        "3×3 Morphology Kernel\n"

        "FPGA Streaming Architecture",

        "muted"

        );

    layout->addWidget(morphHelp);

    layout->addSpacing(12);

    // Resolution

    layout->addWidget(

        makeLabel("05   RESOLUTION", "sideHeading")

        );

    resolutionCombo = new QComboBox;

    resolutionCombo->addItems({

        "256 × 256",

        "320 × 240",

        "640 × 480"

    });

    layout->addWidget(resolutionCombo);

    layout->addSpacing(22);

    QPushButton *runButton =

        new QPushButton("▶   RUN FPGA ANALYSIS");

    runButton->setObjectName("primaryButton");

    runButton->setMinimumHeight(52);

    layout->addWidget(runButton);

    connect(runButton, &QPushButton::clicked,

            this, &MainWindow::runProcessing);

    QPushButton *resetButton =

        new QPushButton("↺   RESET RESULTS");

    resetButton->setObjectName("secondaryButton");

    resetButton->setMinimumHeight(43);

    layout->addWidget(resetButton);

    connect(resetButton, &QPushButton::clicked,

            this, &MainWindow::resetResults);

    layout->addStretch();

    QLabel *footer = makeLabel(

        "AGRIVISION EDGE\n"

        "PYNQ-Z2  ·  ZYNQ-7000\n"

        "DAY 1 / MORPHOLOGY V4",

        "sidebarFooter"

        );

    layout->addWidget(footer);

    return sidebar;

}

QWidget *MainWindow::createImageCard(

    const QString &title,

    const QString &subtitle,

    QLabel *&imageLabel)

{

    QFrame *card = makeCard();

    QVBoxLayout *layout = new QVBoxLayout(card);

    layout->setContentsMargins(17, 17, 17, 17);

    layout->setSpacing(10);

    QLabel *titleLabel = makeLabel(title, "cardTitle");

    QLabel *subtitleLabel = makeLabel(subtitle, "muted");

    imageLabel = new QLabel("NO IMAGE");

    imageLabel->setObjectName("imagePlaceholder");

    imageLabel->setAlignment(Qt::AlignCenter);

    imageLabel->setMinimumHeight(205);

    imageLabel->setSizePolicy(

        QSizePolicy::Expanding,

        QSizePolicy::Expanding

        );

    layout->addWidget(titleLabel);

    layout->addWidget(subtitleLabel);

    layout->addWidget(imageLabel, 1);

    return card;

}

QWidget *MainWindow::createMetric(

    const QString &title,

    QLabel *&valueLabel,

    const QString &initialValue)

{

    QFrame *card = makeCard();

    QVBoxLayout *layout = new QVBoxLayout(card);

    layout->setContentsMargins(20, 17, 20, 17);

    layout->setSpacing(10);

    layout->addWidget(makeLabel(title, "metricTitle"));

    valueLabel = makeLabel(initialValue, "metricValue");

    layout->addWidget(valueLabel);

    return card;

}

QWidget *MainWindow::createDashboard()

{

    QWidget *dashboard = new QWidget;

    dashboard->setObjectName("dashboard");

    QVBoxLayout *layout = new QVBoxLayout(dashboard);

    layout->setContentsMargins(30, 28, 30, 28);

    layout->setSpacing(22);

    // Dashboard heading

    QLabel *heading = makeLabel(

        "Agricultural Vision Dashboard",

        "dashboardTitle"

        );

    QLabel *description = makeLabel(

        "Real-time image processing · FPGA morphology · "

        "Crop phenotyping",

        "muted"

        );

    layout->addWidget(heading);

    layout->addWidget(description);

    // Status row

    QFrame *statusCard = makeCard();

    QHBoxLayout *statusLayout = new QHBoxLayout(statusCard);

    statusLayout->setContentsMargins(22, 15, 22, 15);

    statusLayout->addWidget(

        makeLabel("SYSTEM STATUS", "sideHeading")

        );

    statusLayout->addStretch();

    dmaStatus = makeLabel("● DMA NOT CONNECTED", "statusText");

    goldenStatus = makeLabel("● GOLDEN NOT RUN", "statusText");

    statusLayout->addWidget(dmaStatus);

    statusLayout->addSpacing(24);

    statusLayout->addWidget(goldenStatus);

    layout->addWidget(statusCard);

    // Image panels

    QGridLayout *imageGrid = new QGridLayout;

    imageGrid->setSpacing(18);

    imageGrid->addWidget(

        createImageCard(

            "01  ORIGINAL IMAGE",

            "Raw agricultural input",

            originalView

            ), 0, 0

        );

    imageGrid->addWidget(

        createImageCard(

            "02  EDGE DETECTION",

            "Sobel / Prewitt output",

            edgeView

            ), 0, 1

        );

    imageGrid->addWidget(

        createImageCard(

            "03  FPGA MORPHOLOGY",

            "Hardware accelerated Erode / Dilate",

            morphView

            ), 1, 0

        );

    imageGrid->addWidget(

        createImageCard(

            "04  PHENOTYPE ANALYSIS",

            "Annotated agricultural result",

            annotatedView

            ), 1, 1

        );

    imageGrid->setColumnStretch(0, 1);

    imageGrid->setColumnStretch(1, 1);

    layout->addLayout(imageGrid, 1);

    // Metrics

    layout->addWidget(

        makeLabel("PERFORMANCE MONITOR", "sectionTitle")

        );

    QHBoxLayout *metrics = new QHBoxLayout;

    metrics->setSpacing(15);

    metrics->addWidget(

        createMetric("PL E2E LATENCY", latencyValue, "-- ms")

        );

    metrics->addWidget(

        createMetric("THROUGHPUT", fpsValue, "-- FPS")

        );

    QLabel *modeValue;

    metrics->addWidget(

        createMetric("PROCESSING ENGINE",

                     modeValue, "ZYNQ PL")

        );

    layout->addLayout(metrics);

    QLabel *bottom = makeLabel(

        "AgriVision Edge  /  Intelligent Agriculture  /  "

        "Hardware-Software Co-Design",

        "muted"

        );

    bottom->setAlignment(Qt::AlignCenter);

    layout->addWidget(bottom);

    return dashboard;

}

void MainWindow::importImage()

{

    QString path = QFileDialog::getOpenFileName(

        this,

        "选择农业图像",

        QString(),

        "Images (*.png *.jpg *.jpeg *.bmp)"

        );

    if (path.isEmpty())

        return;

    QPixmap pixmap(path);

    if (pixmap.isNull()) {

        QMessageBox::warning(

            this, "读取失败", "无法打开所选图像。"

            );

        return;

    }

    originalPixmap = pixmap;

    inputPath = QFileInfo(path).absoluteFilePath();

    fileLabel->setText(QFileInfo(path).fileName());

    displayImage(originalView, originalPixmap);

    edgeView->setPixmap(QPixmap());

    edgeView->setText("AWAITING PROCESSING");

    morphView->setPixmap(QPixmap());

    morphView->setText("AWAITING FPGA RESULT");

    annotatedView->setPixmap(QPixmap());

    annotatedView->setText("AWAITING ANALYSIS");

    goldenStatus->setText("● GOLDEN NOT RUN");

    fpgaStatus->setText("● FPGA READY");

    dmaStatus->setText("● DMA WAITING");

    latencyValue->setText("-- ms");

    fpsValue->setText("-- FPS");

}

void MainWindow::runProcessing()

{

    if (originalPixmap.isNull()) {

        QMessageBox::information(

            this,

            "提示",

            "请先导入种子或叶片图像。"

            );

        return;

    }

    if (backendProcess->state() != QProcess::NotRunning) {

        QMessageBox::information(this, "正在处理", "上一张图像仍在发送到 PYNQ 板卡，请稍候。");

        return;

    }

    const QString bridge = QStringLiteral(AGRIVISION_SOURCE_DIR) + "/backend/bridge.py";

    if (!QFileInfo::exists(bridge)) {

        QMessageBox::critical(this, "缺少桥接程序", "找不到 backend/bridge.py：\n" + bridge);

        return;

    }

    const QSize resolutions[] = {QSize(256, 256), QSize(320, 240), QSize(640, 480)};

    const QSize size = resolutions[resolutionCombo->currentIndex()];

    const QString root = QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation)

        + "/runs/" + QUuid::createUuid().toString(QUuid::WithoutBraces);

    if (!QDir().mkpath(root)) {

        QMessageBox::critical(this, "目录错误", "无法创建本次运行目录。");

        return;

    }

    runDirectory = root;

    const QJsonObject request{

        {"schema_version", 1}, {"image_path", inputPath},

        {"width", size.width()}, {"height", size.height()},

        {"threshold", thresholdSlider->value()},

        {"edge_mode", edgeCombo->currentText().toLower()},

        {"morph_mode", morphCombo->currentIndex()}, {"output_dir", root}

    };

    const QString requestPath = root + "/request.json";

    QSaveFile file(requestPath);

    if (!file.open(QIODevice::WriteOnly)

        || file.write(QJsonDocument(request).toJson()) < 0 || !file.commit()) {

        QMessageBox::critical(this, "文件错误", "无法写入请求文件。");

        return;

    }

    const QString python = qEnvironmentVariable("AGRIVISION_PYTHON", "python");

    backendOutput.clear();

    fpgaStatus->setText("● FPGA RUNNING");

    dmaStatus->setText("● DMA TRANSFERRING");

    goldenStatus->setText("● GOLDEN RUNNING");

    statusBar()->showMessage("正在向 PYNQ 发送图像并等待真实 PL 结果…");

    backendProcess->start(python, {bridge, "--request", requestPath});

}

void MainWindow::processError(QProcess::ProcessError error)

{

    if (error == QProcess::FailedToStart) {

        fpgaStatus->setText("● FPGA UNREACHABLE");

        dmaStatus->setText("● DMA NOT RUN");

        goldenStatus->setText("● GOLDEN NOT RUN");

        QMessageBox::critical(this, "Python 未启动",

            "无法启动本机 Python。请安装 Python，或设置环境变量 AGRIVISION_PYTHON。\n\n"

            + backendProcess->errorString());

    }

}

void MainWindow::processFinished(int exitCode, QProcess::ExitStatus exitStatus)

{

    backendOutput += backendProcess->readAllStandardOutput();

    QJsonParseError parseError;

    const QJsonDocument document = QJsonDocument::fromJson(backendOutput, &parseError);

    if (exitStatus != QProcess::NormalExit || exitCode != 0 || !document.isObject()

        || !document.object().value("ok").toBool()) {

        fpgaStatus->setText("● FPGA ERROR");

        dmaStatus->setText("● DMA NOT READY");

        goldenStatus->setText("● GOLDEN NOT RUN");

        const QString error = document.isObject()

            ? document.object().value("error").toString()

            : QString::fromUtf8(backendOutput).left(900);

        QMessageBox::critical(this, "FPGA 分析失败",

            error.isEmpty() ? parseError.errorString() : error);

        return;

    }

    const QJsonObject result = document.object();

    const QJsonObject golden = result.value("golden").toObject();

    const QJsonObject images = result.value("images").toObject();

    const QString edgePath = images.value("edge").toString();

    const QString morphologyPath = images.value("morphology").toString();

    const QString annotatedPath = images.value("annotated").toString();

    const QPixmap edge(edgePath), morphology(morphologyPath), annotated(annotatedPath);

    if (edge.isNull() || morphology.isNull() || annotated.isNull()) {

        fpgaStatus->setText("● FPGA RESPONSE ERROR");

        QMessageBox::critical(this, "结果文件错误", "板端响应没有提供可读取的 PNG 结果。");

        return;

    }

    displayImage(edgeView, edge);

    displayImage(morphView, morphology);

    displayImage(annotatedView, annotated);

    fpgaStatus->setText(result.value("fpga_online").toBool() ? "● FPGA ONLINE" : "● FPGA OFFLINE");

    dmaStatus->setText(result.value("dma_ready").toBool() ? "● DMA READY" : "● DMA NOT READY");

    const bool pass = golden.value("correct").toBool();

    goldenStatus->setText(pass ? "● GOLDEN PASS" : "● GOLDEN FAIL");

    latencyValue->setText(QString::number(result.value("pl_e2e_ms").toDouble(), 'f', 3) + " ms");

    fpsValue->setText(QString::number(result.value("fps").toDouble(), 'f', 1) + " FPS");

    statusBar()->showMessage(QString("完成：Golden %1，差异像素 %2，最大差值 %3。归档：%4")

        .arg(pass ? "PASS" : "FAIL")

        .arg(golden.value("different_pixel_count").toInt())

        .arg(golden.value("max_diff").toInt())

        .arg(runDirectory));

}

void MainWindow::resetResults()

{

    originalPixmap = QPixmap();

    inputPath.clear();

    QLabel *views[] = {

        originalView,

        edgeView,

        morphView,

        annotatedView

    };

    for (QLabel *view : views) {

        view->setPixmap(QPixmap());

        view->setText("NO IMAGE");

    }

    fileLabel->setText("尚未选择图像");

    thresholdSlider->setValue(128);

    edgeCombo->setCurrentIndex(0);

    morphCombo->setCurrentIndex(0);

    resolutionCombo->setCurrentIndex(0);

    goldenStatus->setText("● GOLDEN NOT RUN");

    latencyValue->setText("-- ms");

    fpsValue->setText("-- FPS");

}

void MainWindow::displayImage(

    QLabel *label,

    const QPixmap &pixmap)

{

    if (pixmap.isNull())

        return;

    label->setPixmap(

        pixmap.scaled(

            label->size(),

            Qt::KeepAspectRatio,

            Qt::SmoothTransformation

            )

        );

}

void MainWindow::resizeEvent(QResizeEvent *event)

{

    QMainWindow::resizeEvent(event);

    if (!originalPixmap.isNull())

        displayImage(originalView, originalPixmap);

}

void MainWindow::applyStyle()

{

    setStyleSheet(R"(

        QMainWindow {

            background: #F3F7F1;

        }

        #header {

            background: #103C2B;

            border-bottom: 3px solid #79B98A;

        }

        #brand {

            color: #FFFFFF;

            font-size: 23px;

            font-weight: 800;

            letter-spacing: 2px;

        }

        #tagline {

            color: #A9CEB4;

            font-size: 10px;

            letter-spacing: 2px;

        }

        #version {

            color: #C6E5CF;

            font-size: 11px;

            font-weight: 600;

        }

        #offlineBadge {

            background: #315342;

            color: #F3C5A7;

            border-radius: 15px;

            padding: 9px 15px;

            font-size: 11px;

            font-weight: 700;

        }

        #sidebar {

            background: #FFFFFF;

            border-right: 1px solid #DEE8DD;

        }

        #dashboard, #mainScroll {

            background: #F3F7F1;

        }

        #sectionTitle {

            color: #27583E;

            font-size: 12px;

            font-weight: 800;

            letter-spacing: 2px;

        }

        #sideHeading {

            color: #42614D;

            font-size: 11px;

            font-weight: 800;

            letter-spacing: 1px;

        }

        #dashboardTitle {

            color: #173E2C;

            font-size: 26px;

            font-weight: 800;

        }

        #muted {

            color: #83958A;

            font-size: 11px;

        }

        #card {

            background: #FFFFFF;

            border: 1px solid #E0EADF;

            border-radius: 14px;

        }

        #cardTitle {

            color: #204C35;

            font-size: 14px;

            font-weight: 800;

            letter-spacing: 1px;

        }

        #imagePlaceholder {

            background: #F0F5EF;

            border: 1px dashed #CADBCB;

            border-radius: 10px;

            color: #9DB3A1;

            font-size: 12px;

            font-weight: 700;

            letter-spacing: 1px;

        }

        #metricTitle {

            color: #78917E;

            font-size: 11px;

            font-weight: 700;

        }

        #metricValue {

            color: #236B46;

            font-size: 25px;

            font-weight: 800;

        }

        #accentValue {

            color: #26915A;

            font-size: 14px;

            font-weight: 800;

        }

        #statusText {

            color: #A17A43;

            font-size: 11px;

            font-weight: 700;

        }

        #sidebarFooter {

            color: #9AAF9E;

            font-size: 10px;

            line-height: 1.5;

        }

        QComboBox {

            background: #F4F8F3;

            border: 1px solid #D8E5D8;

            border-radius: 8px;

            padding: 11px;

            color: #294F38;

            font-size: 12px;

            min-height: 20px;

        }

        QComboBox:hover {

            border: 1px solid #66AD7D;

        }

        QComboBox QAbstractItemView {

            background: white;

            color: #294F38;

            selection-background-color: #DCEFE1;

            selection-color: #173E2C;

        }

        #primaryButton {

            background: #247A4D;

            color: white;

            border: none;

            border-radius: 9px;

            font-weight: 800;

            font-size: 12px;

            letter-spacing: 1px;

        }

        #primaryButton:hover {

            background: #30945F;

        }

        #primaryButton:pressed {

            background: #185C39;

        }

        #secondaryButton {

            background: #EDF5ED;

            color: #326B47;

            border: 1px solid #D5E6D7;

            border-radius: 9px;

            font-weight: 700;

        }

        #secondaryButton:hover {

            background: #DCEEDF;

        }

        QSlider::groove:horizontal {

            height: 6px;

            background: #DDE9DD;

            border-radius: 3px;

        }

        QSlider::sub-page:horizontal {

            background: #399B65;

            border-radius: 3px;

        }

        QSlider::handle:horizontal {

            background: #247A4D;

            width: 16px;

            margin: -5px 0;

            border-radius: 8px;

        }

        QScrollBar:vertical {

            background: #F3F7F1;

            width: 8px;

        }

        QScrollBar::handle:vertical {

            background: #C4D9C6;

            border-radius: 4px;

        }

    )");

}

