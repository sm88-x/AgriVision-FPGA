#include <QtWidgets>

int main(int argc, char *argv[])

{

    QApplication app(argc, argv);

    app.setStyle("Fusion");

    app.setFont(QFont("Microsoft YaHei UI", 10));

    QMainWindow win;

    win.setWindowTitle("AgriVision Edge · Day 3 Live");

    win.resize(1420, 920);

    win.setStyleSheet(R"(

        QMainWindow { background:#f3f6ee; }

        QWidget { color:#214630; }

        QFrame#header { background:#153e2b; border-radius:9px; }

        QFrame#header QLabel { color:white; font-size:21px; font-weight:700; }

        QFrame#card {

            background:white; border:1px solid #d8e3cf;

            border-radius:10px;

        }

        QLabel#view {

            background:#edf3e6; border:1px dashed #bfd1b1;

            border-radius:8px; color:#7f9871;

        }

        QPushButton {

            background:#e7f0dc; padding:9px;

            border:1px solid #c7d9b8; border-radius:6px;

        }

        QPushButton:hover { background:#d8e9c6; }

        QPushButton#start { background:#387342; color:white; }

        QPushButton:disabled { color:#9aab90; background:#e9eee2; }

        QLineEdit,QComboBox,QSpinBox {

            background:white; padding:6px;

            border:1px solid #ccdaba; border-radius:5px;

        }

        QPlainTextEdit {

            background:#f9fbf6; border:1px solid #d9e4cf;

        }

    )");

    auto *root = new QWidget;

    win.setCentralWidget(root);

    auto *vl = new QVBoxLayout(root);

    vl->setContentsMargins(20,18,20,18);

    auto *header = new QFrame;

    header->setObjectName("header");

    auto *head = new QHBoxLayout(header);

    head->addWidget(new QLabel("AGRIVISION EDGE"));

    head->addStretch();

    head->addWidget(new QLabel("DAY 3 · CAMERA → ETHERNET → FPGA → Qt"));

    vl->addWidget(header);

    auto *status = new QLabel("CAMERA OFFLINE | FPGA 未确认 | DMA 未确认");

    status->setWordWrap(true);

    vl->addWidget(status);

    auto *metrics = new QLabel(

        "Camera FPS -- | Backend Live FPS -- | RTT -- | Full -- | PL DMA：板端 CSV");

    metrics->setWordWrap(true);

    vl->addWidget(metrics);

    auto *middle = new QHBoxLayout;

    vl->addLayout(middle,1);

    auto *panel = new QFrame;

    panel->setObjectName("card");

    panel->setFixedWidth(300);

    auto *pl = new QVBoxLayout(panel);

    middle->addWidget(panel);

    auto field = [&](const QString &name, QWidget *widget) {

        pl->addWidget(new QLabel(name));

        pl->addWidget(widget);

    };

    auto *mode = new QComboBox;

    mode->addItems({"Image","Batch","Live"});

    mode->setCurrentIndex(2);

    field("运行模式",mode);

    auto *preset = new QComboBox;

    preset->addItems({

        "seed_preset",

        "sobel_thr80_erode",

        "sobel_thr80_dilate",

        "prewitt_thr80_erode",

        "prewitt_thr0_dilate"

    });

    field("Pipeline Preset",preset);

    auto *size = new QComboBox;

    size->addItems({"640 × 480","320 × 240","256 × 256"});

    field("网络输入尺寸",size);

    auto *camera = new QSpinBox;

    camera->setRange(0,5);

    field("本机摄像头编号",camera);

    auto *frames = new QSpinBox;

    frames->setRange(0,100000);

    frames->setValue(300);

    frames->setSpecialValueText("持续运行，直到 Stop");

    field("Live 帧数（0=持续运行）",frames);

    auto *host = new QLineEdit("192.168.2.99");

    field("板卡 IP · TCP 9000",host);

    auto *python = new QLineEdit("python");

    field("Python 程序或完整路径",python);

    auto *script = new QLineEdit(

        "./app/python/live_adapter.py");

    field("后端脚本",script);

    auto *pick = new QPushButton("选择 Image / Batch 输入");

    auto *inputLabel = new QLabel("Live 使用本机摄像头");

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

    auto *full = new QPushButton("全屏 / F11");

    pl->addWidget(folder);

    pl->addWidget(full);

    pl->addStretch();

    QList<QLabel*> views;

    for (const QString &title :

         QStringList{"Original · 灰度输入","FPGA Result","Annotated · 预留"}) {

        auto *card = new QFrame;

        card->setObjectName("card");

        auto *cl = new QVBoxLayout(card);

        cl->addWidget(new QLabel(title));

        auto *view = new QLabel("等待图像");

        view->setObjectName("view");

        view->setMinimumSize(170,220);

        view->setAlignment(Qt::AlignCenter);

        cl->addWidget(view,1);

        middle->addWidget(card,1);

        views.append(view);

    }

    auto *log = new QPlainTextEdit;

    log->setReadOnly(true);

    log->setMaximumBlockCount(300);

    log->setFixedHeight(135);

    vl->addWidget(log);

    QProcess process;

    QTimer poll, killDeadline;

    poll.setInterval(200);

    killDeadline.setSingleShot(true);

    QString input, runDir;

    QByteArray output;

    bool stopping=false, pendingRestart=false;

    bool reportedDMA=false;

    QElapsedTimer freshness;

    QByteArray lastStatus;

    QList<QImage> images={QImage(),QImage(),QImage()};

    auto append = [&](const QString &s) {

        log->appendPlainText(

            QTime::currentTime().toString("HH:mm:ss")+"  "+s);

    };

    auto redraw = [&]() {

        for (int i=0;i<views.size();++i) {

            if (images[i].isNull()) continue;

            views[i]->setPixmap(QPixmap::fromImage(images[i]).scaled(

                views[i]->size()-QSize(12,12),

                Qt::KeepAspectRatio,Qt::SmoothTransformation));

        }

    };

    auto load = [&](int index,const QString &path) {

        if (path.isEmpty()) return;

        // 文件名固定，每次重新读取；写入未完成时保留上一帧。

        QImage image(path);

        if (!image.isNull()) images[index]=image;

    };

    auto setBusy = [&](bool busy) {

        for (QWidget *w : QList<QWidget*>{

                 mode,preset,size,camera,frames,host,python,script,pick})

        w->setEnabled(!busy);

        start->setEnabled(!busy);

        stop->setEnabled(busy && !stopping);

        restart->setEnabled(busy && !stopping);

    };

    auto number = [](const QJsonValue &v) {

        return v.isDouble() ? QString::number(v.toDouble(),'f',1) : QString("--");

    };

    QObject::connect(pick,&QPushButton::clicked,[&]() {

        if (mode->currentText()=="Batch") {

            input=QFileDialog::getExistingDirectory(&win,"选择批量图像目录");

        } else if (mode->currentText()=="Image") {

            input=QFileDialog::getOpenFileName(

                &win,"选择图片",QString(),

                "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)");

        } else {

            inputLabel->setText("Live 使用本机 camera_index");

            return;

        }

        inputLabel->setText(input);

    });

    std::function<void()> launch;

    launch=[&]() {

        if (process.state()!=QProcess::NotRunning) return;

        if (!QFileInfo::exists(script->text())) {

            QMessageBox::warning(&win,"缺少后端","找不到 live_adapter.py");

            return;

        }

        if (mode->currentText()!="Live" && input.isEmpty()) {

            QMessageBox::warning(&win,"缺少输入","先选择图片或 Batch 目录");

            return;

        }

        runDir=QStandardPaths::writableLocation(

                     QStandardPaths::DocumentsLocation)

                 +"/AgriVisionlive_stream/"

                 +QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss_zzz")

                 +"_"+QUuid::createUuid().toString(QUuid::WithoutBraces).left(6);

        if (!QDir().mkpath(runDir)) {

            QMessageBox::warning(&win,"目录错误","无法创建输出目录");

            return;

        }

        const QSize sizes[]={QSize(640,480),QSize(320,240),QSize(256,256)};

        const QSize chosen=sizes[size->currentIndex()];

        QJsonObject req{

            {"schema_version",1},

            {"mode",mode->currentText().toLower()},

            {"host",host->text().trimmed()},

            {"port",9000},

            {"width",chosen.width()},

            {"height",chosen.height()},

            {"camera_index",camera->value()},

            {"preset",preset->currentText()},

            {"output_dir",runDir},

            {"golden_audit",true},

            {"timeout",5.0}

        };

        req.insert("frames",frames->value()==0

                                 ? QJsonValue(QJsonValue::Null) : QJsonValue(frames->value()));

        if (mode->currentText()=="Image") req.insert("image_path",input);

        if (mode->currentText()=="Batch") req.insert("input_dir",input);

        QSaveFile requestFile(runDir+"/request.json");

        const QByteArray data=QJsonDocument(req).toJson();

        if (!requestFile.open(QIODevice::WriteOnly)

            || requestFile.write(data)!=data.size()

            || !requestFile.commit()) {

            QMessageBox::warning(&win,"文件错误","请求 JSON 保存失败");

            return;

        }

        output.clear();

        stopping=false;

        pendingRestart=false;

        reportedDMA=false;

        lastStatus.clear();

        freshness.start();

        images={QImage(),QImage(),QImage()};

        for (auto *v:views) {

            v->setPixmap(QPixmap());

            v->setText("等待本次结果");

        }

        status->setText("启动中 · 摄像头/FPGA/DMA 待确认");

        metrics->setText("Camera FPS -- | Backend Live FPS -- | RTT -- | Full --");

        setBusy(true);

        auto env=QProcessEnvironment::systemEnvironment();

        env.insert("PYTHONIOENCODING","utf-8");

        process.setProcessEnvironment(env);

        process.setWorkingDirectory(QFileInfo(script->text()).absolutePath());

        process.start(python->text().trimmed(),{

                                                    QFileInfo(script->text()).absoluteFilePath(),

                                                    "--request",runDir+"/request.json"

                                                });

        poll.start();

        append("启动 "+mode->currentText()+"；证据目录："+runDir);

    };

    auto requestStop=[&]() {

        if (process.state()==QProcess::NotRunning || stopping) return;

        stopping=true;

        setBusy(true);

        status->setText("停止中 · 等待当前帧返回和 Golden 汇总");

        if (mode->currentText()=="Live") {

            QFile f(runDir+"/stop.request");

            if (!f.open(QIODevice::WriteOnly)) {

                append("无法写停止标记，终止本地进程");

                process.terminate();

            }

        } else {

            process.terminate();

        }

        killDeadline.start(15000);

    };

    QObject::connect(start,&QPushButton::clicked,launch);

    QObject::connect(stop,&QPushButton::clicked,requestStop);

    QObject::connect(restart,&QPushButton::clicked,[&]() {

        pendingRestart=true;

        requestStop();

    });

    QObject::connect(&killDeadline,&QTimer::timeout,[&]() {

        if (process.state()!=QProcess::NotRunning) {

            append("正常停止超过 15 秒，强制结束本机后端；本轮汇总可能不完整");

            process.kill();

        }

    });

    QObject::connect(&process,&QProcess::readyReadStandardOutput,[&]() {

        output+=process.readAllStandardOutput();

    });

    QObject::connect(&process,&QProcess::readyReadStandardError,[&]() {

        append(QString::fromUtf8(process.readAllStandardError()).trimmed());

    });

    QObject::connect(&process,&QProcess::errorOccurred,

                     [&](QProcess::ProcessError e) {

                         if (e==QProcess::FailedToStart) {

                             poll.stop();

                             killDeadline.stop();

                             stopping=false;

                             pendingRestart=false;

                             setBusy(false);

                             status->setText("CAMERA OFFLINE | FPGA 未确认 | DMA 未确认");

                             append("Python 启动失败："+process.errorString());

                         }

                     });

    QObject::connect(&poll,&QTimer::timeout,[&]() {

        if (mode->currentText()!="Live") return;

        QFile file(runDir+"/live_status.json");

        if (!file.open(QIODevice::ReadOnly)) return;

        const QByteArray raw=file.readAll();

        file.close();

        QJsonParseError error;

        const auto document=QJsonDocument::fromJson(raw,&error);

        if (error.error!=QJsonParseError::NoError || !document.isObject()) return;

        if (raw!=lastStatus) {

            lastStatus=raw;

            freshness.restart();

        }

        const auto s=document.object();

        if (freshness.elapsed()>12000 && !stopping) {

            status->setText("状态超过 12 秒未更新 · CAMERA/FPGA/DMA 状态未知");

            return;

        }

        const auto counters=s["counters"].toObject();

        status->setText(QString(

                            "CAMERA %1 | FPGA 服务 %2 | DMA %3 | 成功 %4 | 超时 %5 | DMA错误 %6")

                            .arg(s["camera"].toString("未确认"))

                            .arg(s["fpga"].toString("未确认"))

                            .arg(s["dma"].toString("未确认"))

                            .arg(counters["ok"].toInt())

                            .arg(counters["timeout"].toInt())

                            .arg(counters["dma_err"].toInt()));

        const auto fps=s["fps"].toObject();

        const auto latency=s["latency"].toObject();

        metrics->setText(QString(

                             "Camera FPS %1 | Backend Live FPS %2 | RTT %3 ms | Full %4 ms"

                             " | PL DMA：板端 CSV | 相机丢帧 %5 | 网络丢帧 %6")

                             .arg(number(fps["camera"]))

                             .arg(number(fps["live"]))

                             .arg(number(latency["rtt_ms"]))

                             .arg(number(latency["full_ms"]))

                             .arg(s["cam_dropped"].toInt())

                             .arg(s["net_dropped"].toInt()));

        const auto preview=s["preview"].toObject();

        load(0,preview["cam"].toString());

        load(1,preview["board"].toString());

        redraw();

        if (s.contains("error")) append(s["error"].toString());

        if (s["dma"].toString()=="ERROR" && !reportedDMA) {

            reportedDMA=true;

            append("检测到 DMA 错误，本轮停止；请检查板端日志");

            pendingRestart=false;

            requestStop();

        }

    });

    QObject::connect(&process,

                     QOverload<int,QProcess::ExitStatus>::of(&QProcess::finished),

                     [&](int code,QProcess::ExitStatus exitStatus) {

                         output+=process.readAllStandardOutput();

                         poll.stop();

                         killDeadline.stop();

                         const bool wasStopped=stopping;

                         const bool again=pendingRestart;

                         stopping=false;

                         pendingRestart=false;

                         QJsonObject result;

                         const auto lines=output.trimmed().split('\n');

                         if (!lines.isEmpty())

                             result=QJsonDocument::fromJson(lines.last()).object();

                         QSaveFile f(runDir+"/final_response.json");

                         if (f.open(QIODevice::WriteOnly)) {

                             f.write(QJsonDocument(result).toJson());

                             f.commit();

                         }

                         if (exitStatus==QProcess::NormalExit && code==0

                             && result["ok"].toBool()) {

                             const auto image=result["image"].toObject();

                             load(0,image["input"].toString());

                             load(1,image["output"].toString());

                             redraw();

                             append(QString::fromUtf8(

                                 QJsonDocument(result).toJson(QJsonDocument::Compact)));

                             status->setText("本轮结束 · CAMERA OFFLINE | FPGA/DMA 历史结果见日志");

                             if (mode->currentText()!="Live")

                                 metrics->setText(

                                     "单图/批量已结束；网络、Golden 和统计见下方 JSON");

                         } else {

                             status->setText("已停止或失败 · CAMERA OFFLINE | FPGA/DMA 未确认");

                             append(result["error"].toString(

                                 QString::fromUtf8(output).left(2000)));

                         }

                         setBusy(false);

                         if (again) {

                             if (exitStatus==QProcess::NormalExit && code==0)

                                 QTimer::singleShot(500,&win,launch);

                             else

                                 append("前一轮异常结束，取消自动 Restart，请先核对板端状态");

                         } else if (wasStopped) {

                             append("停止完成");

                         }

                     });

    QObject::connect(folder,&QPushButton::clicked,[&]() {

        if (!runDir.isEmpty())

            QDesktopServices::openUrl(QUrl::fromLocalFile(runDir));

    });

    auto toggleFull=[&]() {

        if (win.isFullScreen()) win.showNormal();

        else win.showFullScreen();

    };

    QObject::connect(full,&QPushButton::clicked,toggleFull);

    auto *shortcut=new QShortcut(QKeySequence(Qt::Key_F11),&win);

    QObject::connect(shortcut,&QShortcut::activated,toggleFull);

    QTimer resizeRefresh;

    QObject::connect(&resizeRefresh,&QTimer::timeout,redraw);

    resizeRefresh.start(500);

    append("Live 使用本机摄像头；板端 TCP 9000；灰度转换在 PC。");

    append("Annotated 暂未接入；PL DMA 实测值在板端 CSV，不用 RTT 代替。");

    win.show();

    const int rc=app.exec();

    if (process.state()!=QProcess::NotRunning) {

        QObject::disconnect(&process,nullptr,nullptr,nullptr);

        QFile stopMarker(runDir+"/stop.request");

        if (stopMarker.open(QIODevice::WriteOnly)) stopMarker.close();

        if (!process.waitForFinished(15000)) {

            process.kill();

            process.waitForFinished(3000);

        }

    }

    return rc;

}