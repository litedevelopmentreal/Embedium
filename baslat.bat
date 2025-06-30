@echo off
setlocal

:: Sanal ortamınızın adını buraya girin (genellikle 'venv' veya 'env' dir)
set VENV_NAME=venv

echo Discord Bot baslatiliyor...

:: Sanal ortamı etkinleştirme
if exist "%VENV_NAME%\Scripts\activate.bat" (
    call "%VENV_NAME%\Scripts\activate.bat"
) else (
    echo Hata: Sanal ortam etkinlestirme betigi bulunamadi. Dogru yol mu?
    echo Beklenen yol: %VENV_NAME%\Scripts\activate.bat
    pause
    exit /b 1
)

:: Python betigini calistirma
python main.py

echo Bot kapandi.
pause