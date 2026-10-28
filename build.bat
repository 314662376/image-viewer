@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/2] 打包单文件 exe ...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name ImageViewer ^
  --icon icon.ico ^
  --add-data "icon.ico;." ^
  --collect-all pillow_heif ^
  image_viewer.py
if errorlevel 1 (
  echo 打包失败，请检查上面的报错。
  pause
  exit /b 1
)

echo [2/2] 输出中文名副本 ...
copy /y "dist\ImageViewer.exe" "光影相册.exe" >nul

echo.
echo 完成：dist\ImageViewer.exe  ^(同时复制为 光影相册.exe^)
echo 直接双击即可运行，无需安装 Python 或任何依赖。
pause
