"""**前端产物够不够新**——不够就地重建。

为什么要有这一步
----------------
``web/dist`` 在 ``.gitignore`` 里（构建产物不进仓库），于是"拉了更新"之后
**界面并不会跟着变**：服务读的是 ``dist``，而 ``dist`` 还是上一次构建的那一份。

从前 ``start.sh`` 只在 ``dist/index.html`` **不存在**时才构建——于是这个坑踩得
很安静：源码换了、后端换了接口形状，界面还是老样子。症状是**说不清的坏**：
左栏要 ``categories``（旧）、后端给的是 ``children``（新），界面渲染直接崩，
而崩在"点扫描之后那一次刷新"里，看起来就是"点了一直没结果、一直在扫描"。

所以判据改成 **``src`` 比 ``dist`` 新就重建**，而不是"没有才建"。

依赖同理：``node_modules`` **存在不等于装全**。``package.json`` 新加了依赖而依赖目录
还是旧的，构建会在 rollup 里报 ``failed to resolve import "pinia"`` —— 症状看着像代码错，
实际只是没装。所以这里也按 mtime 判：``package.json`` / ``package-lock.json`` 比安装戳新
就重装。

口径
----
- 比的是**那一堆源文件里最新的一个**（含 ``index.html`` / ``vite.config.*`` /
  ``package.json`` / ``package-lock.json``）——改了配置或依赖也要重建；
- 用 mtime 而不是内容哈希：拉下来的文件 mtime 就是拉取时刻，够用；
- 构建失败**不静默**：退出码非 0，启动脚本据此停下——带着旧界面跑起来比
  起不来更难查（用户会以为是自己操作错了）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"
DIST = WEB / "dist"
INDEX = DIST / "index.html"
NODE_MODULES = WEB / "node_modules"

#: 国内直连 npm 官方源常超时，装依赖与构建脚本统一走镜像。
_REGISTRY = "https://registry.npmmirror.com"

#: npm 每次安装写的戳，存在即说明装过一次（``npm ci`` 会先清空再写）。
DEPS_STAMP = NODE_MODULES / ".package-lock.json"

#: 依赖声明。任何一个比 ``DEPS_STAMP`` 新，已装的依赖就可能是旧的。
_DEP_MANIFESTS: tuple[str, ...] = ("package.json", "package-lock.json")

#: 参与"够不够新"比较的那些输入（改了任何一个都要重建）。
_WATCHED: tuple[str, ...] = (
    "src",
    "index.html",
    "vite.config.ts",
    "vite.config.js",
    "tsconfig.json",
    "package.json",
    "package-lock.json",
)


def newest_source_mtime() -> float:
    """源文件里最新的那个 mtime（秒）。一个都没有 → 0。"""
    stamps: list[float] = []
    for name in _WATCHED:
        path = WEB / name
        if not path.exists():
            continue
        if path.is_dir():
            stamps.extend(item.stat().st_mtime for item in path.rglob("*") if item.is_file())
        else:
            stamps.append(path.stat().st_mtime)
    return max(stamps, default=0.0)


def deps_stale() -> tuple[bool, str]:
    """（要不要重装依赖，一句可读说明的原因）。"""
    if not NODE_MODULES.is_dir():
        return True, "还没有装依赖"
    # 安装戳由 npm 写；个别版本不写，缺了就退回依赖目录的 mtime —— 两者都是安装那一刻刷的，
    # 只是精度差一点。宁可少判一次旧，也不要每次启动都重装 50 秒。
    stamp = DEPS_STAMP if DEPS_STAMP.exists() else NODE_MODULES
    installed = stamp.stat().st_mtime
    newest = max(
        ((WEB / name).stat().st_mtime for name in _DEP_MANIFESTS if (WEB / name).exists()),
        default=0.0,
    )
    if newest > installed:
        return True, "依赖声明比已装的依赖新"
    return False, "依赖已是最新"


def is_stale() -> tuple[bool, str]:
    """（要不要重建，一句可读说明的原因）。"""
    if not INDEX.exists():
        return True, "还没有构建产物"
    source = newest_source_mtime()
    built = INDEX.stat().st_mtime
    if source > built:
        return True, "源码比产物新（拉过更新，界面还没跟上）"
    return False, f"产物是最新的（源码 {source:.0f} ≤ 产物 {built:.0f}）"


def install() -> int:
    """``npm ci`` 按锁装依赖；装不上多半是锁没跟上 ``package.json``，说清楚怎么补。"""
    if not _has("npm"):
        print("[VoiceHub] 没找到 npm，无法安装界面依赖。", file=sys.stderr, flush=True)
        return 1
    print("[VoiceHub] 安装界面依赖……", flush=True)
    done = subprocess.run(
        ["npm", "ci", f"--registry={_REGISTRY}", "--no-audit", "--no-fund"],
        cwd=str(WEB),
        shell=True,
        check=False,
    )
    if done.returncode != 0:
        print(
            "[VoiceHub] 依赖安装失败：`package-lock.json` 与 `package.json` 可能不同步。"
            f"在 {WEB} 下执行 `npm install`，并提交更新后的锁文件。",
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


def build() -> int:
    """在 ``web/`` 下跑 ``npm run build``；``npm`` 不在就说明白，别静默跳过。"""
    npm = "npm"
    if not _has(npm):
        print("[VoiceHub] 没找到 npm，跳过了界面构建——界面可能还是旧的那份。", file=sys.stderr)
        return 1
    print("[VoiceHub] 构建界面……", flush=True)
    done = subprocess.run([npm, "run", "build"], cwd=str(WEB), shell=True, check=False)
    if done.returncode != 0:
        print("[VoiceHub] 界面构建失败——不带着旧界面启动。", file=sys.stderr)
        return done.returncode
    if not INDEX.exists():
        print("[VoiceHub] 构建完了却没有 web/dist/index.html。", file=sys.stderr)
        return 1
    print("[VoiceHub] 界面已更新。", flush=True)
    return 0


def _has(command: str) -> bool:
    from shutil import which

    return bool(which(command))


def main() -> int:
    need_install, why = deps_stale()
    if need_install:
        print(f"[VoiceHub] 需要装依赖：{why}", flush=True)
        rc = install()
        if rc:
            return rc
    stale, reason = is_stale()
    # 刚装过依赖就必须重建：产物是照旧依赖打的，不重打可能对不上。
    if not (stale or need_install):
        print(f"[VoiceHub] 界面：{reason}")
        return 0
    print(f"[VoiceHub] 界面需要重建：{reason}", flush=True)
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
