QT += core gui widgets



CONFIG += c++17

TEMPLATE = app

TARGET = agrivision_gui



SOURCES += main.cpp
HEADERS += scene_controls.h



msvc:QMAKE_CXXFLAGS += /utf-8

