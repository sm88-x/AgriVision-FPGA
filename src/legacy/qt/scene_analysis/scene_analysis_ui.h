// scene_FINISH_V1


#pragma once





#include <QtWidgets>


#include <QJsonDocument>


#include <QJsonObject>


#include <QJsonArray>


#include <QSaveFile>


#include <QUuid>


#include <cmath>


#include <functional>





class sceneUi : public QWidget


{


public:


    QWidget *modePanel = nullptr;


    QComboBox *mode = nullptr;


    QLabel *receiptLabel = nullptr;





    std::function<QImage()> capture;


    std::function<QJsonObject()> currentFrame;


    std::function<QJsonArray()> allFrames;


    std::function<void()> changed;





    QJsonObject frame;





    void setModeForTest(const QString &value)


    {


        const int index = mode->findData(value);


        if (index >= 0) mode->setCurrentIndex(index);


    }





    void confirmSetupForTest()


    {


        confirmed->setChecked(true);


    }





    void enableMmForTest()


    {


        mm->setChecked(true);


    }





    explicit sceneUi(const QString &projectRoot, QWidget *parent = nullptr)


        : QWidget(parent), root(projectRoot)


    {


        modePanel = new QWidget;


        auto *ml = new QVBoxLayout(modePanel);


        ml->setContentsMargins(0, 0, 0, 0);





        mode = new QComboBox;


        mode->addItem("Manual · 固定 Seed 配置", "manual");


        mode->addItem("Auto · A 的场景策略", "auto");





        receiptLabel = new QLabel("FPGA：尚无实际回执");


        receiptLabel->setWordWrap(true);





        ml->addWidget(new QLabel("scene FPGA 配置模式"));


        ml->addWidget(mode);


        ml->addWidget(receiptLabel);





        auto *layout = new QVBoxLayout(this);


        auto *form = new QFormLayout;


        layout->addLayout(form);





        setup = new QLineEdit;


        setup->setPlaceholderText("例如 camera0_fixed_640x480_v1");





        confirmed = new QCheckBox(


            "确认当前相机、焦距、分辨率和样品平面与标定一致");


        mm = new QCheckBox("详情卡追加毫米结果");


        mm->setEnabled(false);





        form->addRow("机位标识", setup);


        form->addRow(confirmed);


        form->addRow(mm);





        auto *buttons = new QHBoxLayout;


        auto *loadImage = new QPushButton("加载标尺图");


        auto *captureImage = new QPushButton("采集当前原图");


        buttons->addWidget(loadImage);


        buttons->addWidget(captureImage);


        layout->addLayout(buttons);





        imageLabel = new QLabel("加载标尺图后，以原图像素坐标输入两个刻度端点");


        imageLabel->setAlignment(Qt::AlignCenter);


        imageLabel->setMinimumHeight(160);


        imageLabel->setWordWrap(true);


        layout->addWidget(imageLabel, 1);





        auto *coordinates = new QFormLayout;


        for (int i = 0; i < 4; ++i) {


            endpoint[i] = new QDoubleSpinBox;


            endpoint[i]->setDecimals(2);


            endpoint[i]->setRange(0, 100000);


            coordinates->addRow(


                QStringList{"x1(px)", "y1(px)", "x2(px)", "y2(px)"}[i],


                endpoint[i]);


        }





        knownLength = new QDoubleSpinBox;


        knownLength->setRange(0.001, 10000);


        knownLength->setDecimals(3);


        knownLength->setValue(10.0);


        coordinates->addRow("两端点真实距离 mm", knownLength);


        layout->addLayout(coordinates);





        auto *scopeText = new QLabel(


            "本版采用中心 ROI [0.25,0.25,0.75,0.75] 的平面比例标定。"


            "标尺与种子必须同平面；明显透视时应先由 B 校正。"


            "超出 ROI、分辨率不符或机位未确认时退回 px。");


        scopeText->setWordWrap(true);


        layout->addWidget(scopeText);





        auto *actions = new QHBoxLayout;


        auto *saveCalibration = new QPushButton("计算并保存标定");


        auto *loadCalibration = new QPushButton("加载标定");


        auto *invalidate = new QPushButton("机位已改变 / 标定失效");


        actions->addWidget(saveCalibration);


        actions->addWidget(loadCalibration);


        actions->addWidget(invalidate);


        layout->addLayout(actions);





        calibrationLabel = new QLabel;


        calibrationLabel->setWordWrap(true);


        layout->addWidget(calibrationLabel);





        auto *exportButton = new QPushButton(


            "导出当前帧 CSV / JSON 到 demo");


        layout->addWidget(exportButton);


        auto *exportAllButton =


            new QPushButton("批次导出全部已载入帧 CSV / JSON");


        layout->addWidget(exportAllButton);


        connect(exportAllButton, &QPushButton::clicked,


                this, [this]() { exportCurrent(true); });





        auto update = [this]() {


            refreshValidity();


            if (changed) changed();


        };





        connect(mode,


                QOverload<int>::of(&QComboBox::currentIndexChanged),


                this, [this](int) {


            saveControl();


            receiptLabel->setText(


                "模式请求已更新；等待后续帧回执，尚未确认实际生效");


        });





        connect(setup, &QLineEdit::textEdited, this, [this]() {


            confirmed->setChecked(false);


            saveControl();


            refreshValidity();


            if (changed) changed();


        });





        connect(confirmed, &QCheckBox::toggled, this,


                [update](bool) { update(); });


        connect(mm, &QCheckBox::toggled, this,


                [update](bool) { update(); });





        connect(loadImage, &QPushButton::clicked, this, [this]() {


            QString path = QFileDialog::getOpenFileName(


                this, "加载原始标尺图", root,


                "Images (*.png *.jpg *.jpeg *.bmp)");


            if (path.isEmpty()) return;


            QImage image(path);


            if (image.isNull()) {


                fail("无法读取标尺图");


                return;


            }


            setRuler(image);


        });





        connect(captureImage, &QPushButton::clicked, this, [this]() {


            QImage image = capture ? capture() : QImage();


            if (image.isNull()) {


                fail("当前没有原图。请先运行一次采集或加载标尺图片。");


                return;


            }


            setRuler(image);


        });





        connect(saveCalibration, &QPushButton::clicked,


                this, [this]() { createCalibration(); });





        connect(loadCalibration, &QPushButton::clicked,


                this, [this]() {


            QString path = QFileDialog::getOpenFileName(


                this, "加载 B 或 C 的标定参数",


                root + "/calibration/scene", "JSON (*.json)");


            if (path.isEmpty()) return;


            tryLoadCalibration(path);


        });





        connect(invalidate, &QPushButton::clicked, this, [this]() {


            confirmed->setChecked(false);


            calibration.insert("valid", false);


            calibration.insert("reason", "用户确认机位或样品平面已改变");


            writeJson(root + "/data/calibration/scale_calibration/camera_calibration.json",


                      calibration);


            mm->setChecked(false);


            refreshValidity();


            if (changed) changed();


        });





        connect(exportButton, &QPushButton::clicked,


                this, [this]() { exportCurrent(); });





        QFile controlFile(root + "/data/evidence/demo/scene_control.json");


        if (controlFile.open(QIODevice::ReadOnly)) {


            QJsonObject c =


                QJsonDocument::fromJson(controlFile.readAll()).object();


            setup->setText(c.value("setup_id").toString());


            int index = mode->findData(c.value("mode").toString());


            if (index >= 0) {


                QSignalBlocker block(mode);


                mode->setCurrentIndex(index);


            }


        }





        QString own = root + "/data/calibration/scale_calibration/camera_calibration.json";


        QString original = root + "/data/calibration/scale_calibration/calibration.json";


        tryLoadCalibration(QFileInfo::exists(own) ? own : original, false);


        refreshValidity();


    }





    void receipt(const QJsonObject &r)


    {


        if (r.isEmpty()) {


            receiptLabel->setText("FPGA：该结果未包含 scene 回执");


            return;


        }





        const bool verified =


            r.value("register_verified").toBool(false)


            && r.value("applied_frame").isDouble()


            && !r.value("actual_registers").isNull();





        QString text = QString(


            "结果模式：%1\n场景：%2 · config_id：%3\n处理帧：%4\n")


            .arg(r.value("mode").toString("N/A"))


            .arg(r.value("scene").toString("N/A"))


            .arg(r.value("config_id").isDouble()


                 ? QString::number(r.value("config_id").toInt()) : "N/A")


            .arg(r.value("frame_id").isDouble()


                 ? QString::number(r.value("frame_id").toInt()) : "N/A");





        if (verified) {


            text += QString("实际生效帧：%1\n实际寄存器：%2")


                .arg(r.value("applied_frame").toInt())


                .arg(QString::fromUtf8(QJsonDocument(


                    QJsonObject{


                        {"registers", r.value("actual_registers")}


                    }).toJson(QJsonDocument::Compact)));


        } else {


            text += "实际寄存器：N/A · 尚无完整板端读回\n";


            text += r.value("receipt_note").toString();


        }


        receiptLabel->setText(text);


    }





    QString number(double value, int power, const QRectF &bbox,


                   int decimals = 2) const


    {


        const QString pxUnit = power == 2 ? "px²" : "px";


        QString text = QString::number(value, 'f', decimals)


                       + " " + pxUnit;





        if (mm->isChecked() && validFor(frame, bbox)) {


            double scale = calibration.value("px_per_mm").toDouble();


            double denominator = power == 2 ? scale * scale : scale;


            text += " / " + QString::number(


                value / denominator, 'f', decimals)


                + (power == 2 ? " mm²" : " mm");


        }


        return text;


    }





    bool saveControl()


    {


        QJsonObject c{


            {"mode", mode->currentData().toString()},


            {"setup_id", setup->text().trimmed()},


            {"revision", QUuid::createUuid().toString(


                 QUuid::WithoutBraces)}


        };


        return writeJson(root + "/data/evidence/demo/scene_control.json", c);


    }





private:


    QString root;


    QLineEdit *setup = nullptr;


    QCheckBox *confirmed = nullptr;


    QCheckBox *mm = nullptr;


    QLabel *imageLabel = nullptr;


    QLabel *calibrationLabel = nullptr;


    QDoubleSpinBox *endpoint[4]{};


    QDoubleSpinBox *knownLength = nullptr;


    QImage ruler;


    QJsonObject calibration;





    void fail(const QString &message)


    {


        QMessageBox::warning(this, "scene", message);


    }





    bool writeJson(const QString &path, const QJsonObject &object)


    {


        if (!QFileInfo(QFileInfo(path).absolutePath()).isDir()) {


            fail("目标目录不存在，不自动新建目录：" + path);


            return false;


        }


        QSaveFile file(path);


        QByteArray data = QJsonDocument(object).toJson();


        if (!file.open(QIODevice::WriteOnly)


            || file.write(data) != data.size()


            || !file.commit()) {


            fail("写入失败：" + path);


            return false;


        }


        return true;


    }





    void setRuler(const QImage &image)


    {


        ruler = image;


        confirmed->setChecked(false);


        mm->setChecked(false);


        imageLabel->setPixmap(QPixmap::fromImage(image).scaled(


            640, 240, Qt::KeepAspectRatio, Qt::SmoothTransformation));


        imageLabel->setToolTip(


            QString("原图 %1 × %2；坐标必须按原图输入")


                .arg(image.width()).arg(image.height()));


        for (int i = 0; i < 4; ++i)


            endpoint[i]->setMaximum(


                i % 2 == 0 ? image.width() - 1 : image.height() - 1);


    }





    bool basicValid() const


    {


        double scale = calibration.value("px_per_mm").toDouble();


        return calibration.value("valid").toBool(false)


            && std::isfinite(scale) && scale > 0


            && confirmed->isChecked()


            && !setup->text().trimmed().isEmpty();


    }





    bool validFor(const QJsonObject &f, const QRectF &bbox) const


    {


        if (!basicValid()) return false;





        QJsonArray size = calibration.value("scope").toObject()


                              .value("image_size").toArray();


        QJsonArray roi = calibration.value("scope").toObject()


                             .value("working_roi_normalized").toArray();


        if (size.size() != 2 || roi.size() != 4) return false;





        int width = f.value("width").toInt();


        int height = f.value("height").toInt();


        if (width <= 0 || height <= 0


            || width != size[0].toInt()


            || height != size[1].toInt()) return false;





        QString currentSetup = setup->text().trimmed();


        QString savedSetup = calibration.value("setup_id").toString();





        // B 的旧格式可能没有 setup_id，仍要求本次明确确认。


        if (!savedSetup.isEmpty() && savedSetup != currentSetup)


            return false;


        if (f.value("setup_id").toString() != currentSetup)


            return false;





        QRectF allowed(


            roi[0].toDouble() * width,


            roi[1].toDouble() * height,


            (roi[2].toDouble() - roi[0].toDouble()) * width,


            (roi[3].toDouble() - roi[1].toDouble()) * height);





        return bbox.isValid() && allowed.contains(bbox);


    }





    void refreshValidity()


    {


        bool ok = basicValid();


        mm->setEnabled(ok);


        if (!ok && mm->isChecked()) {


            QSignalBlocker blocker(mm);


            mm->setChecked(false);


        }





        calibrationLabel->setText(ok


            ? "标定参数可用；每个目标仍需通过尺寸、机位及 ROI 检查。"


            : "毫米结果已禁用：请完成标定并确认当前机位。"


              "标定文件本身不能自动检测相机是否被移动。");


    }





    void createCalibration()


    {


        if (ruler.isNull()) {


            fail("请先加载或采集标尺图");


            return;


        }


        if (setup->text().trimmed().isEmpty()) {


            fail("请填写机位标识");


            return;


        }





        QPointF p1(endpoint[0]->value(), endpoint[1]->value());


        QPointF p2(endpoint[2]->value(), endpoint[3]->value());


        QRectF roi(ruler.width() * .25, ruler.height() * .25,


                   ruler.width() * .5, ruler.height() * .5);


        if (!roi.contains(p1) || !roi.contains(p2)) {


            fail("两个参考端点都必须在画面中心 ROI 内");


            return;


        }





        double pixels = QLineF(p1, p2).length();


        if (pixels < 10) {


            fail("参考线段过短，请选择至少 10 像素的刻度跨度");


            return;


        }





        if (QMessageBox::question(


                this, "确认标定条件",


                "确认标尺与种子同平面，且中心 ROI 内透视可忽略？"


                "此确认不等于已通过卡尺精度核验。")


            != QMessageBox::Yes) return;





        calibration = QJsonObject{


            {"schema", "agrivision.calibration.v1"},


            {"calibration_id",


             QUuid::createUuid().toString(QUuid::WithoutBraces)},


            {"valid", true},


            {"setup_id", setup->text().trimmed()},


            {"method", "manual_ruler_center_roi"},


            {"px_per_mm", pixels / knownLength->value()},


            {"reference", QJsonObject{


                {"known_length_mm", knownLength->value()},


                {"measured_px", pixels},


                {"p1", QJsonArray{p1.x(), p1.y()}},


                {"p2", QJsonArray{p2.x(), p2.y()}}


            }},


            {"scope", QJsonObject{


                {"image_size",


                 QJsonArray{ruler.width(), ruler.height()}},


                {"working_roi_normalized",


                 QJsonArray{.25, .25, .75, .75}}


            }}


        };





        QString path = root + "/data/calibration/scale_calibration/camera_calibration.json";


        if (!writeJson(path, calibration)) return;


        if (!ruler.save(root + "/calibration/scene/scene_ruler_C.png")) {


            fail("参数已保存，但标尺图保存失败");


            return;


        }


        confirmed->setChecked(true);


        mm->setChecked(true);


        saveControl();


        refreshValidity();


        if (changed) changed();


    }





    void tryLoadCalibration(const QString &path, bool report = true)


    {


        QFile file(path);


        if (!file.open(QIODevice::ReadOnly)) {


            if (report) fail("无法读取标定文件");


            return;


        }


        QJsonParseError error;


        auto document = QJsonDocument::fromJson(file.readAll(), &error);


        if (error.error != QJsonParseError::NoError || !document.isObject()) {


            if (report) fail("标定 JSON 格式错误");


            return;


        }





        calibration = document.object();


        confirmed->setChecked(false);


        mm->setChecked(false);


        refreshValidity();


        if (changed) changed();


    }





    static QString csvCell(QString value)


    {


        value.replace("\"", "\"\"");


        return "\"" + value + "\"";


    }








    void exportCurrent(bool batch = false)


    {


        QJsonArray input;


        if (batch) {


            if (allFrames) input = allFrames();


        } else if (currentFrame) {


            QJsonObject current = currentFrame();


            if (!current.isEmpty()) input.append(current);


        }





        if (input.isEmpty()) {


            fail("没有可导出的结果");


            return;


        }





        QString base = root + "/demo/scene_example";


        if (batch) {


            base = root + "/demo/scene_batch_"


                + QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss_zzz")


                + "_" + QUuid::createUuid().toString(


                    QUuid::WithoutBraces).left(8);


        } else if ((QFileInfo::exists(base + ".csv")


                    || QFileInfo::exists(base + ".json"))


                   && QMessageBox::question(


                       this, "覆盖样例",


                       "覆盖已有 scene_example.csv/.json？")


                      != QMessageBox::Yes) {


            return;


        }





        const QStringList pixelKeys{


            "length_px", "width_px", "perimeter_px", "area_px"


        };


        const QStringList metricKeys{


            "length_mm", "width_mm", "perimeter_mm", "area_mm2"


        };





        QString csv =


            "session_id,frame_id,species,id,status,calibration_id,"


            "length_px,width_px,perimeter_px,area_px,"


            "length_mm,width_mm,perimeter_mm,area_mm2,"


            "aspect_ratio,circularity\n";





        QJsonArray exportedFrames;





        for (const QJsonValue &fv : input) {


            QJsonObject f = fv.toObject();


            QJsonArray convertedObjects;





            for (const QJsonValue &ov : f.value("objects").toArray()) {


                QJsonObject o = ov.toObject();


                QJsonArray b = o.value("bbox_xyxy_src").toArray();


                if (b.size() != 4) b = o.value("bbox").toArray();





                QRectF box;


                if (b.size() == 4) {


                    box = QRectF(


                        QPointF(b[0].toDouble(), b[1].toDouble()),


                        QPointF(b[2].toDouble(), b[3].toDouble()));


                }





                bool usable = validFor(f, box)


                    && o.value("status").toString() == "OK";


                for (const QString &key : pixelKeys)


                    usable = usable && o.value(key).isDouble();





                QString calId =


                    calibration.value("calibration_id").toString();


                if (usable && calId.isEmpty())


                    calId = "legacy_calibration_snapshot";





                o.insert("mm_valid", usable);


                o.insert("calibration_id",


                    usable ? QJsonValue(calId)


                           : QJsonValue(QJsonValue::Null));





                QStringList row{


                    csvCell(f.value("session_id").toString()),


                    QString::number(f.value("frame_id").toInt(-1)),


                    csvCell(f.value("species").toString()),


                    QString::number(o.value("id").toInt()),


                    csvCell(o.value("status").toString()),


                    csvCell(usable ? calId : QString())


                };





                for (const QString &key : pixelKeys)


                    row << (o.value(key).isDouble()


                        ? QString::number(o.value(key).toDouble(), 'g', 12)


                        : QString());





                double scale =


                    calibration.value("px_per_mm").toDouble();





                for (int k = 0; k < pixelKeys.size(); ++k) {


                    if (usable) {


                        double divisor = k == 3 ? scale * scale : scale;


                        double value =


                            o.value(pixelKeys[k]).toDouble() / divisor;


                        o.insert(metricKeys[k], value);


                        row << QString::number(value, 'g', 12);


                    } else {


                        o.insert(metricKeys[k],


                                 QJsonValue(QJsonValue::Null));


                        row << QString();


                    }


                }





                for (const QString &key :


                     {QString("aspect_ratio"), QString("circularity")})


                    row << (o.value(key).isDouble()


                        ? QString::number(o.value(key).toDouble(), 'g', 12)


                        : QString());





                csv += row.join(",") + "\n";


                convertedObjects.append(o);


            }





            f.insert("objects", convertedObjects);


            exportedFrames.append(f);


        }





        QJsonObject output{


            {"schema", "agrivision.scene.export.v1"},


            {"frames", exportedFrames},


            {"calibration_snapshot", calibration},


            {"setup_confirmed", confirmed->isChecked()},


            {"exported_at",


             QDateTime::currentDateTime().toString(Qt::ISODate)},


            {"units", QJsonObject{


                {"length_px", "px"},


                {"width_px", "px"},


                {"perimeter_px", "px"},


                {"area_px", "px^2"},


                {"length_mm", "mm"},


                {"width_mm", "mm"},


                {"perimeter_mm", "mm"},


                {"area_mm2", "mm^2"},


                {"aspect_ratio", "dimensionless"},


                {"circularity", "dimensionless"}


            }}


        };





        if (!writeJson(base + ".json", output)) return;





        QSaveFile file(base + ".csv");


        QByteArray data = csv.toUtf8();


        if (!file.open(QIODevice::WriteOnly)


            || file.write(data) != data.size()


            || !file.commit()) {


            fail("JSON 已保存，但 CSV 写入失败");


            return;


        }





        QMessageBox::information(


            this, "导出完成",


            QString("已导出 %1 帧：\n%2.csv\n%2.json")


                .arg(exportedFrames.size()).arg(base));


    }





};


