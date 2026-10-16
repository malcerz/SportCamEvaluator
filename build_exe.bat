@echo off
rem Buduje samodzielny SportCamEvaluator.exe (PyInstaller) w folderze dist\SportCamEvaluator
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller || goto :err
py -3 -m PyInstaller --noconfirm --clean --windowed --onefile --name SportCamEvaluator ^
    --collect-submodules core --collect-submodules gui sportcam_evaluator.py || goto :err
if not exist dist\SportCamEvaluator mkdir dist\SportCamEvaluator
move /y dist\SportCamEvaluator.exe dist\SportCamEvaluator\ >nul
if exist ffmpeg xcopy /e /i /y ffmpeg dist\SportCamEvaluator\ffmpeg >nul
if exist scoring.json copy /y scoring.json dist\SportCamEvaluator\ >nul
echo.
echo Gotowe: dist\SportCamEvaluator\SportCamEvaluator.exe
echo (ffmpeg.exe i ffprobe.exe musza lezec w podfolderze ffmpeg obok EXE)
pause
exit /b 0
:err
echo Blad budowania.
pause
exit /b 1
