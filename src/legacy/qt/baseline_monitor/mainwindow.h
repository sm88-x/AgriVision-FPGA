#ifndef MAINWINDOW_H

#define MAINWINDOW_H



#include <QMainWindow>

#include <QLabel>

#include <QComboBox>

#include <QSlider>

#include <QPushButton>

#include <QPixmap>

#include <QProcess>



class MainWindow : public QMainWindow

{

    Q_OBJECT



public:

    explicit MainWindow(QWidget *parent = nullptr);



private:

    QLabel *originalView;

    QLabel *edgeView;

    QLabel *morphView;

    QLabel *annotatedView;



    QLabel *fpgaStatus;

    QLabel *dmaStatus;

    QLabel *goldenStatus;

    QLabel *latencyValue;

    QLabel *fpsValue;

    QLabel *thresholdValue;

    QLabel *fileLabel;



    QComboBox *edgeCombo;

    QComboBox *morphCombo;

    QComboBox *resolutionCombo;

    QSlider *thresholdSlider;



    QPixmap originalPixmap;

    QString inputPath;

    QString runDirectory;

    QProcess *backendProcess;

    QByteArray backendOutput;



    QWidget *createSidebar();

    QWidget *createDashboard();

    QWidget *createImageCard(

        const QString &title,

        const QString &subtitle,

        QLabel *&imageLabel

        );

    QWidget *createMetric(

        const QString &title,

        QLabel *&valueLabel,

        const QString &initialValue

        );



    void applyStyle();

    void importImage();

    void runProcessing();

    void processFinished(int exitCode, QProcess::ExitStatus exitStatus);

    void processError(QProcess::ProcessError error);

    void resetResults();

    void displayImage(QLabel *label, const QPixmap &pixmap);



protected:

    void resizeEvent(QResizeEvent *event) override;

};



#endif

