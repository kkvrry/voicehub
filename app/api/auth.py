"""访问令牌：让"开放到局域网"不等于"网段里谁都能操作"。

**默认不启用**（``config.json`` 的 ``access_token`` 为空）：本机个人工具，行为与以前
完全一致——绑回环时根本用不到，绑通配时靠"可信网络"这条假设。配了令牌之后：

- **本机（回环）请求照旧放行**：托盘、本机浏览器、脚本都不该被自己的登录页挡住；
- 其它来源要带令牌：``X-VoiceHub-Token`` 头 / ``Authorization: Bearer`` / 登录页
  设下的 cookie；
- 浏览器第一次打开界面会被领到 ``/login``：一个**自带样式、无外部依赖**的单页，
  输对了种 cookie 再回原地址。这样前端不用改一行（它永远只在已授权的页面里跑）。

令牌从哪来：环境变量 ``VOICEHUB_TOKEN`` 优先，其次 ``config.json`` 的
``access_token``。与 ``host`` / ``port`` 一样，**改完要重启**（进程启动时读一次）。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.api.clientaddr import is_local_client
from app.api.errors import token_denied
from app.infra.config import load_config

__all__ = ["COOKIE_NAME", "install_token_guard", "login_page", "resolve_token", "token_ok"]

#: 登录后种下的 cookie 名。HttpOnly：页面脚本读不到它（XSS 也偷不走）。
COOKIE_NAME = "voicehub_token"

#: cookie 有效期：30 天。这是"这台设备信任我"的粒度，不是账号体系。
_COOKIE_MAX_AGE = 30 * 24 * 3600


def resolve_token() -> str:
    """这次进程使用的令牌；空串 = 不校验。"""
    return os.environ.get("VOICEHUB_TOKEN", "").strip() or load_config().access_token.strip()


def _header_token(request: Request) -> str:
    """请求头带来的令牌：``X-VoiceHub-Token`` → ``Authorization: Bearer``。

    **不支持 query**：``?token=`` 会进访问日志、浏览器历史与代理记录。
    """
    header = request.headers.get("X-VoiceHub-Token", "").strip()
    if header:
        return header
    authorization = request.headers.get("Authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _same(left: str, right: str) -> bool:
    """定长比较。

    ``hmac.compare_digest`` 对 ``str`` 只接受 ASCII——口令可能是中文（用户会这么设），
    那样它直接抛 TypeError。统一先编码成字节再比。
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _cookie_value(token: str) -> str:
    """cookie 里放**派生值**，不是令牌本身。

    两个理由：令牌可能含 ``;`` / 空格 / 非 ASCII（那样 ``Set-Cookie`` 直接坏掉），
    而派生值是纯十六进制；顺带也不会把用户可能复用到别处的口令原样交给浏览器。
    """
    return hmac.new(token.encode("utf-8"), b"voicehub-cookie-v1", hashlib.sha256).hexdigest()


def token_ok(request: Request, token: str) -> bool:
    """这个请求的凭证对不对（定长比较，别用 ``==`` 泄时间）。"""
    given = _header_token(request)
    if given:
        return _same(given, token)
    cookie = request.cookies.get(COOKIE_NAME, "").strip()
    return bool(cookie) and _same(cookie, _cookie_value(token))


def _safe_next(value: str) -> str:
    """登录后回哪去：只认本站路径，别变成开放重定向。"""
    target = (value or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


def login_page(*, next_path: str = "/", error: str = "") -> str:
    """登录页。自带样式：不依赖 web/dist，没有前端包也能登录。"""
    message = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoiceHub · 访问令牌</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font: 15px/1.6 "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
    background: #f5f6f8; color: #1c1d21;
  }}
  form {{
    width: min(360px, calc(100vw - 32px)); padding: 28px 26px 22px;
    background: #fff; border: 1px solid #e3e5ea; border-radius: 14px;
    box-shadow: 0 12px 32px rgb(20 22 30 / 8%);
  }}
  h1 {{ margin: 0 0 6px; font-size: 18px; letter-spacing: .01em; }}
  .hint {{ margin: 0 0 18px; color: #6b7280; font-size: 13px; }}
  input[type=password] {{
    width: 100%; padding: 10px 12px; font: inherit; color: inherit;
    background: #fbfbfc; border: 1px solid #d8dbe2; border-radius: 9px;
  }}
  input[type=password]:focus {{ outline: 2px solid #9aa4ff; outline-offset: 1px; }}
  button {{
    width: 100%; margin-top: 12px; padding: 10px 12px; font: inherit; font-weight: 600;
    color: #fff; background: #2f3b52; border: 0; border-radius: 9px; cursor: pointer;
  }}
  button:hover {{ background: #3b4a66; }}
  .error {{ margin: 10px 0 0; color: #b42318; font-size: 13px; }}
  .foot {{ margin: 16px 0 0; color: #8b929e; font-size: 12px; line-height: 1.5; }}
  code {{ font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14161b; color: #e8eaee; }}
    form {{ background: #1c1f26; border-color: #2b303a; box-shadow: none; }}
    .hint {{ color: #98a0ad; }}
    input[type=password] {{ background: #14161b; border-color: #333a46; }}
    button {{ background: #4b5b7a; }}
    button:hover {{ background: #586a8c; }}
    .error {{ color: #ff8f85; }}
    .foot {{ color: #7d8592; }}
  }}
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>VoiceHub</h1>
  <p class="hint">这个服务开放给了局域网，需要访问令牌。</p>
  <input type="password" name="token" placeholder="访问令牌" autofocus
         autocomplete="current-password" required>
  <input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
  {message}
  <button type="submit">进入</button>
  <p class="foot">令牌在服务端 <code>config.json</code> 的 <code>access_token</code>，
  或环境变量 <code>VOICEHUB_TOKEN</code>。</p>
</form>
</body>
</html>"""


def install_token_guard(app: FastAPI, *, token: str | None = None) -> None:
    """装上令牌闸门（``token`` 缺省取 :func:`resolve_token` 的结果）。

    位置有讲究：它是**最内层**的中间件，跑在请求上下文之后——401 也要带
    ``X-Request-Id`` 与错误体，否则"被挡住"这件事在日志里查不到。
    """
    required = resolve_token() if token is None else token.strip()

    @app.middleware("http")
    async def _token_guard(request: Request, call_next):
        path = request.url.path
        # 登录页与状态端点本身要放行，否则会自己把自己重定向成死循环。
        if not required or path in ("/login", "/auth") or is_local_client(request):
            return await call_next(request)
        if token_ok(request, required):
            return await call_next(request)
        if path.startswith("/api"):
            return token_denied(request)
        # 界面：给一个能输入令牌的页面，输对了回原地址。
        if request.method in ("GET", "HEAD"):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
        return token_denied(request)


def login_routes(app: FastAPI) -> None:
    """``/login`` 的读与写。不进 OpenAPI：它不是给程序调的接口。"""

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_form(next: str = "/") -> Response:
        return HTMLResponse(login_page(next_path=_safe_next(next)))

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request) -> Response:
        required = resolve_token()
        form = await request.form()
        target = _safe_next(str(form.get("next", "/")))
        given = str(form.get("token", "")).strip()
        if not required:
            # 没配令牌（本机个人工具）：登录页没有存在的意义，直接回去。
            return RedirectResponse(target, status_code=303)
        if not given or not _same(given, required):
            return HTMLResponse(
                login_page(next_path=target, error="令牌不对，再试一次。"),
                status_code=401,
            )
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            _cookie_value(required),
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            # https 下顺手标 secure；换回 http 后这枚 cookie 会被忽略，需要重登一次。
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/auth", include_in_schema=False)
    def auth_state(request: Request) -> JSONResponse:
        """当前这台设备的授权状态（登录页与脚本自检用；本身不敏感）。"""
        required = resolve_token()
        return JSONResponse(
            {
                "object": "auth",
                "required": bool(required),
                "ok": (not required) or is_local_client(request) or token_ok(request, required),
            }
        )
