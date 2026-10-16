@echo off
rem Uruchamia SportCamEvaluator bez okna konsoli (wymaga Pythona 3.9+ z python.org).
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0sportcam_evaluator.py" %* & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0sportcam_evaluator.py" %* & exit /b)
echo Nie znaleziono Pythona. Zainstaluj Python 3 z https://www.python.org/downloads/
echo (zaznacz "Add python.exe to PATH") albo zbuduj EXE: build_exe.bat na komputerze z Pythonem.
pause
