#!/usr/bin/env bash
# VoiceHub 启动脚本（Unix/Git Bash）。
#
# 依赖由 **uv** 管：环境不在它自己建（顺带把 Python 也下了），在就是现成的。
# 不再用 python -m venv / pip：那套没有锁，换台机器装出来的版本不一样，
# 而"版本不一样"的症状是跑起来才报错，比装不上更难查。
cd "$(dirname "$0")"

# 数据目录就放在项目里（与 start.bat 同口径）：config.json、音色库、缓存、日志都在这。
# 不设的话会落到 ~/.voicehub，换台机器就"库还在、界面是空的"。
export VOICEHUB_DATA_DIR="$PWD/data"

# uv 的官方安装器默认把它放在 ~/.local/bin，而那个目录**常常不在 PATH 上**
# （本机就是：用户级 PATH 里只有 WindowsApps）。所以先找 PATH，再回退找这个目录。
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
fi
if [ -z "$UV" ]; then
    echo "[x] 没找到 uv。安装：https://docs.astral.sh/uv/getting-started/installation/"
    echo "    装完如果还找不到，把 ~/.local/bin 加进 PATH，或直接改这个脚本里的 UV=。"
    exit 1
fi

# --frozen：严格照 uv.lock 装，不重新解析。锁是提交进仓库的，于是每台机器装出来一样。
#
# --inexact：**别删环境里"多余"的包**。uv sync 默认会把不在锁里的包卸掉，而界面上的
# "语音识别"装进来的那几个（funasr / torch / torchaudio / soundfile / modelscope，
# 见 app/infra/asr.py）本来就不在锁里——不带这个参数，装完它们活不过下一次启动：
# 账本还写着"装了"，而 import torch 已经失败。这不是"同步得不够严"：
# --frozen 的锁定性一个字都没变，只是不再顺手清掉别人装进来的东西。
"$UV" sync --frozen --inexact --quiet || exit 1

# **不是"没有才建"，是"源码比产物新就建"**：web/dist 在 .gitignore 里，拉了更新
# 之后界面不会自己变，而"界面是旧的那份"的症状是说不清的坏（左栏要 categories、
# 后端给的是 children，渲染直接崩，看起来像"点扫描一直没结果"）。
# 依赖装没装全、产物够不够新，都由 scripts/ensure_web.py 一处判、一处装、一处建。
"$UV" run --frozen python scripts/ensure_web.py || exit 1

# 不在这里报地址：端口可能来自环境变量或 config.json，由 app.main 打印真正生效的那个。
echo "[VoiceHub] 启动中……"
"$UV" run --frozen python -m app.main "$@"
