@echo off
chcp 65001 >nul
echo ========================================
echo   Amazon 価格追跡アプリ 起動中...
echo ========================================
echo.

cd /d "%~dp0"

:: Python パスを検索（複数候補）
set PYTHON_EXE=
if exist "C:\Users\%USERNAME%\AppData\Local\Python\bin\python.exe" (
    set PYTHON_EXE=C:\Users\%USERNAME%\AppData\Local\Python\bin\python.exe
)
if "%PYTHON_EXE%"=="" where python >nul 2>&1 && set PYTHON_EXE=python
if "%PYTHON_EXE%"=="" (
    echo [エラー] Python が見つかりません。
    echo 以下のURLからインストールしてください: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: パッケージが未インストールなら install
"%PYTHON_EXE%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [1/2] 初回セットアップ中...パッケージをインストールしています
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    echo.
    echo セットアップ完了！
    echo.
)

echo [2/2] サーバーを起動します (http://localhost:5000)
echo       ブラウザで上記URLを開いてください
echo       終了するには Ctrl+C を押してください
echo.

:: ブラウザを自動で開く（2秒後）
ping -n 3 127.0.0.1 >nul
start http://localhost:5000

"%PYTHON_EXE%" server.py
pause
