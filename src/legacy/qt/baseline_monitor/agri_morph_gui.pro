QT += core gui widgets



CONFIG += c++17

TEMPLATE = app

TARGET = agri_morph_gui



SOURCES += \

    main.cpp \

    mainwindow.cpp



HEADERS += \

    mainwindow.h



win32:CONFIG += windows

DEFINES += AGRIVISION_SOURCE_DIR=\"$$PWD\"

