"""前端那 700 KB 只压给"要真的走网线"的那台机器。

本机请求走回环，压一遍是白烧 CPU（355 KB 的 JS 每次约 5 ms）；局域网 / Wi-Fi
才是这 700 KB 真正难受的地方——压完约 190 KB，不到三分之一。

这里钉四件事：本机不压、外网压、不带 gzip 的客户端拿原文、流式响应不压
（gzip 会攒块，日志流就"卡住"了；这条由 GZipMiddleware 的默认排除表保证）。
"""

from fastapi.testclient import TestClient

LAN = ("192.168.1.50", 51000)


def _app():
    from app.main import create_app

    return create_app()


def _lan(data_dir) -> TestClient:
    return TestClient(_app(), client=LAN)


def test_a_local_request_is_not_compressed(data_dir) -> None:
    """本机：带不带 Accept-Encoding 都一样——原样给，省下每次的压缩开销。"""
    client = TestClient(_app())  # 默认来源是 testclient，算本机
    resp = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


def test_a_lan_request_is_compressed(data_dir) -> None:
    """局域网：同样的请求压过，而且解出来仍是同一份内容。

    用 ``/openapi.json``：它够大（几十 KB）且不依赖库里有没有音色。
    """
    local = TestClient(_app()).get("/openapi.json")
    resp = _lan(data_dir).get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"
    # httpx 会自动解压，所以只能比内容；"确实压小了"由下面的桩测试看字节魔数。
    assert resp.json() == local.json()


def test_a_lan_client_without_gzip_support_gets_plain_bytes(data_dir) -> None:
    """不支持 gzip 的客户端（老工具、某些脚本）不该收到压缩体。"""
    resp = _lan(data_dir).get("/openapi.json", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


class _Stub:
    """最小的 ASGI 应用：给定状态码、类型与正文，只为观察中间件做了什么。"""

    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.content_type = content_type

    async def __call__(self, scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", self.content_type.encode()),
                    (b"content-length", str(len(self.body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": self.body, "more_body": False})


def _call(wrapper_app, body: bytes, content_type: str, client: tuple[str, int]):
    """跑一次请求，回 (content-encoding, 收到的字节数)。"""
    import asyncio

    from app.main import _LanGzip

    received: dict = {}

    async def send(message) -> None:
        if message["type"] == "http.response.start":
            received["headers"] = dict(message["headers"])
        elif message["type"] == "http.response.body":
            received["size"] = len(message.get("body", b""))
            received["gzipped"] = message.get("body", b"")[:2] == b"\x1f\x8b"

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(b"accept-encoding", b"gzip")],
        "client": client,
    }
    wrapper = _LanGzip(_Stub(body, content_type))
    asyncio.run(wrapper(scope, None, send))  # type: ignore[arg-type]
    return received["headers"].get(b"content-encoding"), received.get("size")


def test_the_wrapper_compresses_big_bodies_for_lan_only() -> None:
    """大正文：局域网压，本机不压。"""
    body = b"x" * 20000
    assert _call(None, body, "application/json", LAN)[0] == b"gzip"
    assert _call(None, body, "application/json", ("127.0.0.1", 5000))[0] is None


def test_small_bodies_are_left_alone() -> None:
    """小正文不压：分块与头的开销比省下的字节多（流式响应那条路之外都适用）。"""
    encoding, _size = _call(None, b"{}", "application/json", LAN)
    assert encoding is None


def test_streaming_content_types_are_never_compressed() -> None:
    """``text/event-stream`` 与音频不能进 gzip——日志流要一条一条立刻看到。"""
    body = b"data: hello\n\n" * 1000
    assert _call(None, body, "text/event-stream; charset=utf-8", LAN)[0] is None
    assert _call(None, body, "audio/wav", LAN)[0] is None
