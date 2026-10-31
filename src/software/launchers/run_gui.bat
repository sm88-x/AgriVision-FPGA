@echo off

setlocal

for %%I in ("%~dp0..\..\..") do set "ROOT=%%~fI"

set "APP=%ROOT%\outputs\qt\release\agrivision_gui.exe"

if not exist "%APP%" set "APP=%ROOT%\outputs\qt\agrivision_gui.exe"

if not exist "%APP%" (

 echo [ERROR] GUI is not built. Run python tools/build.py qt --qmake QMAKE_PATH --make MAKE_PATH

 exit /b 2

)

if not defined AGRIVISION_PYTHON set "AGRIVISION_PYTHON=python"

cd /d "%ROOT%"

"%APP%" --backend "%ROOT%\src\software\app\network_backend.py" --python "%AGRIVISION_PYTHON%" %*

