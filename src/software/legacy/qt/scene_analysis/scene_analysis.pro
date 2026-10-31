QT += core gui widgets

CONFIG += c++17

TEMPLATE = app

TARGET = agri_scene_gui

SOURCES += scene_analysis_main.cpp

HEADERS += scene_analysis_ui.h

msvc:QMAKE_CXXFLAGS += /utf-8

