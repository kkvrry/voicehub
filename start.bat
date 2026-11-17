@echo off
rem VoiceHub 启动脚本 —— 双击即用。
rem
rem 依赖由 uv 管：环境不在它自己建（顺带把 Python 也下了），在就是现成的。
rem 不再用 python -m venv / pip：那套没有锁，换台机器装出来的版本不一样。
rem
rem 本文件是 GBK + CRLF，故意的，别改 —— 见仓库根的 .gitattributes。
cd /d "%~dp0"

rem 数据目录放进工作区：服务若被沙箱启动，%LOCALAPPDATA% 是只读的。
set "VOICEHUB_DATA_DIR=%~dp0data"

rem uv 的官方安装器默认把它放在 %USERPROFILE%\.local\bin，而那个目录**常常不在
rem PATH 上**（本机就是这样）。所以先找 PATH，再回退找这个目录。
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
"%UV%" --version >nul 2>nul
if errorlevel 1 (
    echo [VoiceHub] 没找到 uv。安装：https://docs.astral.sh/uv/getting-started/installation/
    echo            装完如果还找不到，把 %%USERPROFILE%%\.local\bin 加进 PATH。
    echo.
    pause
    exit /b 1
)

rem --frozen：严格照 uv.lock 装。锁是提交进仓库的，于是每台机器装出来一样。
rem --inexact：不要删掉环境里"多余"的包——界面上装的语音识别依赖不在锁里，
rem 不带它那几个包活不过下一次启动（账本还写着"装了"，而 import torch 已经失败）。
"%UV%" sync --frozen --inexact --quiet
if errorlevel 1 (
    echo [VoiceHub] 依赖安装失败：请检查网络后重试。
    echo.
    pause
    exit /b 1
)

rem **不是"没有才建"，是"源码比产物新就建"**：web\dist 在 .gitignore 里面，
rem 拉了更新之后界面不会自己变；而"界面还是旧的那份"的症状是说不清的坏 ——
rem 左栏要 categories（旧）、后端给的是 children（新），渲染直接崩，看起来就是
rem "点了扫描一直没结果、一直在扫描"。判据与 Unix 那份同一个脚本。
rem 依赖装没装全、产物够不够新，统一由 scripts\ensure_web.py 判断并处理。
"%UV%" run --frozen python scripts\ensure_web.py
if errorlevel 1 (
    echo [VoiceHub] 界面准备失败（依赖或构建）—— 不带着旧界面启动。
    echo.
    pause
    exit /b 1
)


echo [VoiceHub] 正在启动服务……
"%UV%" run --frozen python -m app.main %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [VoiceHub] 启动失败（退出码 %RC%）。常见原因：
    echo   - 端口已被占用：服务可能已经开着了（地址见上面那行）
    echo   - 依赖异常：删掉 .venv 目录后重新双击本脚本
    echo.
    pause
)
exit /b %RC%
