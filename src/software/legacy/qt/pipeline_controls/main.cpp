#include <QtWidgets>

#include <QtNetwork>

#include <cmath>

int main(int argc, char *argv[])

{

    QApplication app(argc, argv);

    app.setStyle("Fusion");

    app.setFont(QFont("Microsoft YaHei UI", 10));

    QMainWindow window;

    window.setWindowTitle("AgriVision Edge | Day 2 Pipeline");

    window.resize(1380, 880);

    window.setStyleSheet(R"(

        QWidget { font-family:"Microsoft YaHei UI"; color:#244531; }

        QMainWindow { background:#f4f7f0; }

        QFrame#header { background:#153e2d; }

        QFrame#header QLabel { color:white; }

        QLabel#title { font-size:25px; font-weight:700; }

        QFrame#card {

            background:white; border:1px solid #dae5d6;

            border-radius:12px;

        }

        QLabel#caption { font-size:14px; font-weight:700; }

        QLabel#image {

            background:#eef4e9; border:1px dashed #c7d8bf;

            border-radius:8px; color:#829579;

        }

        QPushButton {

            background:#edf4e7; border:1px solid #cdddc3;

            padding:10px; border-radius:7px; font-weight:600;

        }

        QPushButton:hover { background:#dfeccd; }

        QPushButton#run { background:#347443; color:white; }

        QPushButton:disabled { color:#9aa995; background:#e8eee3; }

        QComboBox,QSpinBox,QLineEdit {

            background:white; border:1px solid #cfddc7;

            border-radius:6px; padding:6px; min-height:22px;

        }

        QPlainTextEdit {

            background:#f9fbf6; border:1px solid #dce6d5;

            border-radius:6px;

        }

    )");

    auto *root = new QWidget;

    auto *layout = new QVBoxLayout(root);

    layout->setContentsMargins(22,18,22,18);

    layout->setSpacing(14);

    window.setCentralWidget(root);

    auto *header = new QFrame;

    header->setObjectName("header");

    auto *hl = new QHBoxLayout(header);

    hl->setContentsMargins(20,18,20,18);

    auto *brand = new QLabel("AGRIVISION EDGE");

    brand->setObjectName("title");

    hl->addWidget(brand);

    hl->addStretch();

    hl->addWidget(new QLabel("DAY 2 · RGB2GRAY / GAUSSIAN / MORPHOLOGY"));

    layout->addWidget(header);

    auto *status = new QLabel(

        "待运行 · 导入图像，选择 Seed Preset");

    status->setWordWrap(true);

    layout->addWidget(status);

    auto *middle = new QHBoxLayout;

    layout->addLayout(middle,1);

    auto *controls = new QFrame;

    controls->setObjectName("card");

    controls->setFixedWidth(280);

    auto *cl = new QVBoxLayout(controls);

    cl->setContentsMargins(18,18,18,18);

    cl->setSpacing(10);

    middle->addWidget(controls);

    auto *heading = new QLabel("处理配置 / PIPELINE");

    heading->setObjectName("caption");

    cl->addWidget(heading);

    auto *open = new QPushButton("＋ 导入种子 / 叶片图像");

    auto *fileLabel = new QLabel("尚未选择图像");

    fileLabel->setWordWrap(true);

    cl->addWidget(open);

    cl->addWidget(fileLabel);

    auto *gray = new QCheckBox("RGB2Gray · PL");

    gray->setChecked(true);

    gray->setEnabled(false);

    gray->setToolTip("RGB 上传路径要求启用；灰度旁路需另行定义输入协议");

    auto *gauss = new QCheckBox("Gaussian 3×3 · PL");

    gauss->setChecked(true);

    cl->addWidget(gray);

    cl->addWidget(gauss);

    cl->addWidget(new QLabel("Edge"));

    auto *edge = new QComboBox;

    edge->addItems({"Sobel","Prewitt"});

    cl->addWidget(edge);

    cl->addWidget(new QLabel("Threshold · 0 表示饱和灰度输出"));

    auto *threshold = new QSpinBox;

    threshold->setRange(0,255);

    threshold->setValue(40);

    cl->addWidget(threshold);

    cl->addWidget(new QLabel("Morphology"));

    auto *morph = new QComboBox;

    morph->addItems({"Bypass","Erode","Dilate"});

    morph->setCurrentIndex(2);

    cl->addWidget(morph);

    cl->addWidget(new QLabel("Resolution"));

    auto *resolution = new QComboBox;

    resolution->addItems({"256 × 256","320 × 240","640 × 480"});

    resolution->setCurrentIndex(2);

    cl->addWidget(resolution);

    auto *preset = new QPushButton("Seed Preset");

    cl->addWidget(preset);

    cl->addWidget(new QLabel("板端服务地址"));

    auto *url = new QLineEdit("http://192.168.2.99:8765");

    cl->addWidget(url);

    auto *run = new QPushButton("运行 FPGA 分析 →");

    run->setObjectName("run");

    run->setEnabled(false);

    auto *save = new QPushButton("导出图片 / 请求 / 证据");

    save->setEnabled(false);

    cl->addWidget(run);

    cl->addWidget(save);

    cl->addStretch();

    auto *note = new QLabel(

        "Annotated 为 PS 候选轮廓标注。\n"

        "候选数量不等于真实种子数量。");

    note->setWordWrap(true);

    cl->addWidget(note);

    auto *gallery = new QHBoxLayout;

    middle->addLayout(gallery,1);

    const QStringList titles = {

        "Original","FPGA Output","Annotated"

    };

    QList<QLabel*> views;

    for (const QString &title : titles) {

        auto *card = new QFrame;

        card->setObjectName("card");

        auto *vl = new QVBoxLayout(card);

        vl->setContentsMargins(12,14,12,12);

        auto *caption = new QLabel(title);

        caption->setObjectName("caption");

        vl->addWidget(caption);

        auto *view = new QLabel("等待结果");

        view->setObjectName("image");

        view->setAlignment(Qt::AlignCenter);

        view->setMinimumSize(180,220);

        vl->addWidget(view,1);

        views.append(view);

        gallery->addWidget(card,1);

    }

    auto *log = new QPlainTextEdit;

    log->setReadOnly(true);

    log->setMaximumBlockCount(200);

    log->setFixedHeight(115);

    layout->addWidget(log);

    QNetworkAccessManager network;

    QTimer deadline;

    deadline.setSingleShot(true);

    QNetworkReply *active = nullptr;

    bool busy = false;

    QImage source;

    QList<QImage> display = {QImage(),QImage(),QImage()};

    QMap<QString,QImage> evidenceImages;

    QJsonObject lastRequest, lastResult;

    QString sourceName;

    QElapsedTimer elapsed;

    auto append = [&](const QString &text) {

        log->appendPlainText(

            QTime::currentTime().toString("HH:mm:ss") + "  " + text);

    };

    auto redraw = [&]() {

        for (int i=0;i<views.size();++i) {

            if (display[i].isNull()) {

                views[i]->setPixmap(QPixmap());

                views[i]->setText("等待本次运行结果");

            } else {

                views[i]->setPixmap(

                    QPixmap::fromImage(display[i]).scaled(

                        views[i]->size()-QSize(16,16),

                        Qt::KeepAspectRatio,

                        Qt::SmoothTransformation));

            }

        }

    };

    auto setBusy = [&](bool value) {

        busy = value;

        open->setEnabled(!value);

        gauss->setEnabled(!value);

        edge->setEnabled(!value);

        threshold->setEnabled(!value);

        morph->setEnabled(!value);

        resolution->setEnabled(!value);

        preset->setEnabled(!value);

        url->setEnabled(!value);

        run->setEnabled(!value && !source.isNull());

        run->setText(value ? "正在运行，请等待…" : "运行 FPGA 分析 →");

        save->setEnabled(!value && !lastResult.isEmpty());

    };

    auto invalidate = [&]() {

        if (busy) return;

        lastResult = QJsonObject();

        evidenceImages.clear();

        display[1] = QImage();

        display[2] = QImage();

        save->setEnabled(false);

        status->setText("当前参数待运行");

        redraw();

    };

    QObject::connect(preset,&QPushButton::clicked,[&]() {

        gauss->setChecked(true);

        edge->setCurrentText("Sobel");

        threshold->setValue(40);

        morph->setCurrentIndex(2);

        invalidate();

        append("Seed Preset：Gray=1 / Gaussian=1 / Sobel / threshold=40 / Dilate");

    });

    QObject::connect(gauss,&QCheckBox::toggled,[&](bool){invalidate();});

    QObject::connect(edge,QOverload<int>::of(&QComboBox::currentIndexChanged),

                     [&](int){invalidate();});

    QObject::connect(threshold,QOverload<int>::of(&QSpinBox::valueChanged),

                     [&](int){invalidate();});

    QObject::connect(morph,QOverload<int>::of(&QComboBox::currentIndexChanged),

                     [&](int){invalidate();});

    QObject::connect(resolution,QOverload<int>::of(&QComboBox::currentIndexChanged),

                     [&](int){invalidate();});

    QObject::connect(url,&QLineEdit::textChanged,[&](const QString &){invalidate();});

    QObject::connect(open,&QPushButton::clicked,[&]() {

        const QString path = QFileDialog::getOpenFileName(

            &window,"导入农业图像",QString(),

            "Images (*.png *.jpg *.jpeg *.bmp)");

        if (path.isEmpty()) return;

        QImageReader reader(path);

        reader.setAutoTransform(true);

        QImage image = reader.read();

        if (image.isNull()) {

            QMessageBox::warning(&window,"图片错误",reader.errorString());

            return;

        }

        source = image;

        sourceName = QFileInfo(path).fileName();

        display[0] = source;

        invalidate();

        fileLabel->setText(

            sourceName + QString("\n%1 × %2").arg(source.width()).arg(source.height()));

        run->setEnabled(true);

        redraw();

    });

    QObject::connect(&deadline,&QTimer::timeout,[&]() {

        if (active) {

            active->setProperty("client_timeout",true);

            active->abort();

        }

    });

    QObject::connect(run,&QPushButton::clicked,[&]() {

        if (busy || source.isNull()) return;

        const QUrl base(url->text().trimmed());

        if (!base.isValid() || base.scheme()!="http" || base.host().isEmpty()) {

            QMessageBox::warning(&window,"地址错误","填写 http://192.168.2.99:8765");

            return;

        }

        invalidate();

        const QSize sizes[] = {

            QSize(256,256),QSize(320,240),QSize(640,480)

        };

        const QSize size = sizes[resolution->currentIndex()];

        QByteArray png;

        QBuffer buffer(&png);

        buffer.open(QIODevice::WriteOnly);

        if (!source.save(&buffer,"PNG")) {

            QMessageBox::warning(&window,"图片错误","无法编码输入 PNG");

            return;

        }

        if (png.size()>12*1024*1024) {

            QMessageBox::warning(&window,"图片过大","输入 PNG 超过 12 MiB");

            return;

        }

        QJsonObject config{

            {"enable_gray",true},

            {"enable_gauss",gauss->isChecked()},

            {"edge_mode",edge->currentText()},

            {"threshold",threshold->value()},

            {"morph_mode",morph->currentIndex()}

        };

        lastRequest = QJsonObject{

            {"schema_version",2},

            {"width",size.width()},

            {"height",size.height()},

            {"config",config},

            {"image_base64",QString::fromLatin1(png.toBase64())}

        };

        QUrl endpoint=base;

        endpoint.setPath("/run");

        endpoint.setQuery(QString());

        endpoint.setFragment(QString());

        QNetworkRequest request(endpoint);

        request.setHeader(QNetworkRequest::ContentTypeHeader,"application/json");

        request.setRawHeader("Connection","close");

        setBusy(true);

        elapsed.start();

        status->setText("运行中 · 等待板端 DMA、Golden 和 PNG 返回");

        append(QString::fromUtf8(QJsonDocument(config).toJson(QJsonDocument::Compact)));

        active=network.post(

            request,QJsonDocument(lastRequest).toJson(QJsonDocument::Compact));

        deadline.start(60000);

        QNetworkReply *reply=active;

        QObject::connect(reply,&QNetworkReply::finished,[&,reply,size]() {

            deadline.stop();

            const QByteArray body=reply->readAll();

            const bool timedOut=reply->property("client_timeout").toBool();

            const QString networkError=reply->errorString();

            const auto code=reply->error();

            active=nullptr;

            reply->deleteLater();

            QJsonParseError parse;

            const auto doc=QJsonDocument::fromJson(body,&parse);

            const auto result=doc.object();

            auto failure=[&](const QString &message) {

                lastResult=QJsonObject();

                setBusy(false);

                status->setText("运行未完成 · Golden 未验证");

                append(message);

                QMessageBox::warning(&window,"运行失败",message);

            };

            if (timedOut) {

                failure("HTTP 等待超过 60 秒。远端任务可能仍在执行；"

                        "请先检查板端终端，再决定是否重新运行。");

                return;

            }

            if (code!=QNetworkReply::NoError) {

                failure(result["error"].toString(networkError));

                return;

            }

            if (parse.error!=QJsonParseError::NoError || !doc.isObject()

                || result["ok"]!=QJsonValue(true)

                || result["schema_version"].toInt()!=2

                || !result["simulated"].isBool()) {

                failure(result["error"].toString("响应不是有效 Day 2 JSON"));

                return;

            }

            const bool simulated=result["simulated"].toBool();

            const QJsonObject sent=lastRequest["config"].toObject();

            const QJsonObject returned=result["config"].toObject();

            for (auto it=sent.begin();it!=sent.end();++it) {

                if (returned.value(it.key())!=it.value()) {

                    failure("返回配置与本次请求不一致："+it.key());

                    return;

                }

            }

            if (!simulated) {

                const double ms=result["pl_e2e_ms"].toDouble(-1);

                const double fps=result["fps"].toDouble(-1);

                const double diff=result["different_pixel_count"].toDouble(-1);

                const double maxDiff=result["max_abs_diff"].toDouble(-1);

                if (!result["golden_pass"].isBool()

                    || ms<=0 || fps<=0 || !std::isfinite(ms)

                    || !std::isfinite(fps)

                    || diff<0 || diff>size.width()*size.height()

                    || std::floor(diff)!=diff

                    || maxDiff<0 || maxDiff>255

                    || std::floor(maxDiff)!=maxDiff

                    || result["golden_pass"].toBool()!=(diff==0 && maxDiff==0)

                    || (diff==0)!=(maxDiff==0)) {

                    failure("真实响应缺少合法的 Golden / 性能字段");

                    return;

                }

            }

            const auto encoded=result["images_base64"].toObject();

            QMap<QString,QImage> loaded;

            for (const QString &key :

                 QStringList{"original","output","annotated","golden","diff"}) {

                QImage image=QImage::fromData(

                    QByteArray::fromBase64(encoded[key].toString().toLatin1()),"PNG");

                if (image.isNull() || image.size()!=size) {

                    failure("结果 PNG 无效或尺寸不符："+key);

                    return;

                }

                loaded.insert(key,image);

            }

            evidenceImages=loaded;

            display[0]=loaded["original"];

            display[1]=loaded["output"];

            display[2]=loaded["annotated"];

            lastResult=result;

            redraw();

            setBusy(false);

            if (simulated) {

                status->setText(

                    "SIMULATED · 软件联调结果 · FPGA/Golden/PL Latency 尚未验证");

            } else {

                status->setText(QString(

                    "REAL FPGA | %1 | FPGA %2 | DMA %3 | Golden %4"

                    " | %5 ms | %6 FPS | 候选 %7")

                    .arg(result["overlay_version"].toString())

                    .arg(result["fpga_online"].toBool()?"ONLINE":"未确认")

                    .arg(result["dma_ready"].toBool()?"READY":"未确认")

                    .arg(result["golden_pass"].toBool()?"PASS":"FAIL")

                    .arg(result["pl_e2e_ms"].toDouble(),0,'f',3)

                    .arg(result["fps"].toDouble(),0,'f',1)

                    .arg(result["candidate_count"].toInt()));

            }

            append(QString("完成；电脑请求至显示 %1 ms；板端总处理 %2 ms")

                .arg(elapsed.elapsed())

                .arg(result["server_ms"].toDouble(),0,'f',1));

            append("寄存器回读："+QString::fromUtf8(

                QJsonDocument(result["registers"].toObject()).toJson(

                    QJsonDocument::Compact)));

        });

    });

    QObject::connect(save,&QPushButton::clicked,[&]() {

        if (busy || lastResult.isEmpty()) return;

        const QString rootDir=QFileDialog::getExistingDirectory(

            &window,"选择证据保存根目录");

        if (rootDir.isEmpty()) return;

        const QString dir=rootDir+"/pipeline_validation_"

            +QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss_zzz")

            +"_"+QUuid::createUuid().toString(QUuid::WithoutBraces).left(6);

        if (!QDir().mkpath(dir)) {

            QMessageBox::warning(&window,"保存失败","无法创建目录");

            return;

        }

        bool ok=true;

        for (auto it=evidenceImages.begin();it!=evidenceImages.end();++it)

            ok=it.value().save(dir+"/"+it.key()+".png") && ok;

        auto write=[&](const QString &name,const QByteArray &data) {

            QSaveFile file(dir+"/"+name);

            if (!file.open(QIODevice::WriteOnly)) return false;

            if (file.write(data)!=data.size()) return false;

            return file.commit();

        };

        QJsonObject request=lastRequest;

        request.remove("image_base64");

        request["image_file"]="original.png";

        request["source_name"]=sourceName;

        QJsonObject response=lastResult;

        response.remove("images_base64");

        ok=write("request.json",QJsonDocument(request).toJson()) && ok;

        ok=write("response.json",QJsonDocument(response).toJson()) && ok;

        const QByteArray csv=(

            QString("evidence,owner,simulated,overlay,request,response\n")

            +"Qt pipeline_validation run,C,"

            +(response["simulated"].toBool()?"true":"false")+","

            +response["overlay_version"].toString()

            +",request.json,response.json\n").toUtf8();

        ok=write("evidence_index.csv",csv) && ok;

        append((ok?"证据已保存：":"保存不完整，请检查磁盘：")+dir);

        QMessageBox::information(&window,ok?"保存完成":"保存不完整",dir);

    });

    QTimer refresh;

    QObject::connect(&refresh,&QTimer::timeout,redraw);

    refresh.start(300);

    append("就绪。真实服务使用 configurable_pipeline.bit；每次请求输出完整链。");

    window.show();

    const int code=app.exec();

    if (active) {

        QObject::disconnect(active,nullptr,nullptr,nullptr);

        active->abort();

    }

    return code;

}