#include <QApplication>

#include <QFont>

#include "mainwindow.h"

int main(int argc, char *argv[])

{

    QApplication app(argc, argv);

    app.setStyle("Fusion");

    QFont font("Microsoft YaHei UI", 10);

    app.setFont(font);

    MainWindow window;

    window.show();

    return app.exec();

}