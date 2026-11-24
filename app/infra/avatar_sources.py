"""头像图片来源：作品 Wiki、萌娘百科、AniList 与 Bing 图片搜索。

每个来源只做「名字 → 候选图片 URL + 出处」；下载、裁剪与落盘在
:mod:`app.usecase.avatar`。来源由用户在界面上指定，一次只查一个：不按分类猜来源，
也不在来源之间兜底。站点拦截（PRTS、biligame 常见）会抛 :class:`SourceError`，
调用方如实回报；被拦截过的站点短时间内不再请求，避免批量配头像时每张都撞一次墙。
"""

from __future__ import annotations

import html
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

__all__ = [
    "HEADERS",
    "SOURCE_KEYS",
    "Candidate",
    "SourceError",
    "search",
    "source_label",
    "source_rows",
]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HEADERS = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"}

#: 站点拦截后冷却多久。批量任务通常几分钟跑完，足够避开第一轮风控。
_BLOCK_SECONDS = 600
_BLOCKED: dict[str, float] = {}

#: 界面上的来源清单：``label`` 是站点名，``hint`` 说明它收哪类角色。顺序即界面顺序。
#: 新增来源 = 这里加一行 + :func:`search` 加一路。
_SOURCE_ROWS = (
    {"key": "prts", "label": "PRTS Wiki", "hint": "明日方舟"},
    {"key": "biligame_sgs", "label": "三国杀 Wiki", "hint": "三国杀"},
    {"key": "biligame_ys", "label": "原神 Wiki", "hint": "原神"},
    {"key": "moegirl", "label": "萌娘百科", "hint": "动漫与游戏角色条目配图"},
    {"key": "anilist", "label": "AniList", "hint": "动画与游戏角色立绘"},
    {"key": "bing", "label": "Bing 图片", "hint": "真人、主播与其它来源没收录的角色"},
)

#: 合法来源 key，顺序同上。
SOURCE_KEYS: tuple[str, ...] = tuple(str(row["key"]) for row in _SOURCE_ROWS)

_REAL_PERSON_WORDS = ("网红", "明星", "演员", "歌手", "主持", "导演", "真人", "名人", "博主", "主播")


class SourceError(Exception):
    """这个来源暂时用不了：被拦截、超时或接口改版。"""


@dataclass(frozen=True, slots=True)
class Candidate:
    """一张候选图片。``page`` 是出处页；``photo`` 表示是实拍照片（人脸肤色判定的要求可以放宽）。"""

    url: str
    source: str
    page: str
    referer: str = ""
    photo: bool = False


def source_rows() -> list[dict[str, str]]:
    return [dict(row) for row in _SOURCE_ROWS]


def source_label(key: str) -> str:
    """来源 key → 站点名。未知 key 返回空串，调用方据此拒绝请求。"""
    return next((str(row["label"]) for row in _SOURCE_ROWS if row["key"] == key), "")


#: 作品 → 认它的词。**只放作品名，不放「普通话」「方言」这类内容分类**：
#: 那是这个库给声音分的组，不是作品，别的数据集也可能叫这个名字。
#: 同一个作品的各种叫法算一组（「明日方舟」与 Arknights 是同一件事）。
_SERIES_WORDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("arknights", "明日方舟", ("明日方舟", "arknights", "ark nights")),
    ("genshin", "原神", ("原神", "genshin")),
    ("sanguosha", "三国杀", ("三国杀", "sanguosha")),
)


def series(category: str, folder: str = "") -> str:
    """这个音色属于哪个作品：``arknights`` / ``genshin`` / ``sanguosha`` / ``""``。

    做法就是拿**文件路径**去和词表做包含比对：`D:\\Arknights\\普通话\\陈 [char_010_chen]`
    逐段比，命中 ``arknights`` 就算明日方舟。所以分类叫什么无所谓——「普通话」「方言」
    是内容分组，作品名在上一级目录里。

    ``folder`` 拿不到（平台音色没有目录）时，循环会自然落到分类名上。
    """
    for hay in (folder or "", category or ""):
        # 从叶往根找：越靠近音色的目录越具体。
        for part in reversed([p for p in re.split(r"[\\/]+", hay) if p]):
            low = part.casefold()
            for name, _title, words in _SERIES_WORDS:
                if any(word in low for word in words):
                    return name
    return ""


def series_title(name: str) -> str:
    """作品标识 → 中文名（拼搜索关键词用）。"""
    return next((title for key, title, _w in _SERIES_WORDS if key == name), "")


async def search(
    key: str, client: httpx.AsyncClient, name: str, category: str, folder: str = ""
) -> list[Candidate]:
    """跑一个来源。名字为空返回空表；未知来源抛 :class:`ValueError`（调用方先校验 key）。

    ``folder`` 参与作品判定（见 :func:`series`），拼搜索关键词时用作品名而不是
    内容分类名——「陈 普通话」搜不出东西，「陈 明日方舟」才对。
    """
    name = (name or "").strip()
    if not name:
        return []
    found = series(category, folder)
    if key == "prts":
        return await _prts(client, name)
    if key == "biligame_sgs":
        return await _biligame(client, name, "sgs", "三国杀 Wiki")
    if key == "biligame_ys":
        return await _biligame(client, name, "ys", "原神 Wiki")
    if key == "moegirl":
        return await _moegirl(client, name, category, found)
    if key == "anilist":
        return await _anilist(client, name)
    if key == "bing":
        return await _bing(client, name, category, found)
    raise ValueError(f"未知头像来源：{key}")


# --------------------------------------------------------------------------- #
# 接口请求
# --------------------------------------------------------------------------- #


def _host(url: str) -> str:
    return urlsplit(url).netloc


def _blocked(host: str) -> bool:
    return _BLOCKED.get(host, 0.0) > time.monotonic()


def _block(host: str) -> None:
    _BLOCKED[host] = time.monotonic() + _BLOCK_SECONDS


async def _get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> Any:
    host = _host(url)
    if _blocked(host):
        raise SourceError(f"{host} 刚才触发了站点拦截，这次先跳过。")
    response = await client.get(url, params=params)
    if response.status_code in (403, 412, 429, 567):
        _block(host)
        raise SourceError(f"{host} 拒绝访问（HTTP {response.status_code}）。")
    try:
        return response.json()
    except ValueError as exc:
        _block(host)
        raise SourceError(f"{host} 返回的不是接口数据，可能触发了站点拦截。") from exc


async def _post_json(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any]
) -> Any:
    host = _host(url)
    if _blocked(host):
        raise SourceError(f"{host} 刚才触发了站点拦截，这次先跳过。")
    response = await client.post(url, json=payload)
    if response.status_code in (403, 412, 429, 567):
        _block(host)
        raise SourceError(f"{host} 拒绝访问（HTTP {response.status_code}）。")
    try:
        data = response.json()
    except ValueError as exc:
        _block(host)
        raise SourceError(f"{host} 返回的不是接口数据。") from exc
    if data.get("errors"):
        raise SourceError(f"{host} 接口报错：{data['errors'][0].get('message', '未知错误')}")
    return data


def _https(url: str) -> str:
    return f"https:{url}" if url.startswith("//") else url


def _name_key(value: str) -> str:
    """比较名字用：全半角、大小写、空格与标点都不参与。"""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", text)


def _contains_name(title: str, name: str) -> bool:
    """标题里认不认得出这个名字（重定向到梗条目时用来拒绝配图）。"""
    key = _name_key(name)
    return bool(key) and key in _name_key(title)


# --------------------------------------------------------------------------- #
# 作品 Wiki
# --------------------------------------------------------------------------- #


async def _prts(client: httpx.AsyncClient, name: str) -> list[Candidate]:
    """明日方舟 PRTS Wiki。头像是现成的方形头像，立绘作为高分辨率备选。"""
    titles = [
        f"File:头像 {name}.png",
        f"File:立绘 {name} 1.png",
        f"File:立绘 {name} 2.png",
        f"File:立绘 {name}.png",
    ]
    data = await _get_json(
        client,
        "https://prts.wiki/api.php",
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "imageinfo",
            "iiprop": "url|size",
            "titles": "|".join(titles),
        },
    )
    pages = {page.get("title"): page for page in (data.get("query") or {}).get("pages", [])}
    normalized = {
        str(item.get("from")): str(item.get("to"))
        for item in (data.get("query") or {}).get("normalized", [])
    }
    found: list[Candidate] = []
    for title in titles:
        info = ((pages.get(normalized.get(title, title)) or {}).get("imageinfo") or [{}])[0]
        url = info.get("url")
        if url:
            found.append(
                Candidate(
                    url=_https(url),
                    source="PRTS Wiki",
                    page=f"https://prts.wiki/w/{quote(title)}",
                    referer="https://prts.wiki/",
                )
            )
    return found


def _rank_biligame(
    site: str, name: str, files: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """优先现成的方形头像图标；没有图标再用高分辨率立绘裁头。"""

    def area(item: dict[str, Any]) -> int:
        return int(item.get("width") or 0) * int(item.get("height") or 0)

    if site == "sgs":
        def score(item: dict[str, Any]) -> tuple[int, int]:
            title = str(item.get("name") or "")
            if title == f"{name}-头像.png":
                rank = 0
            elif title == f"{name}-经典形象.png":
                rank = 1
            elif "经典形象" in title:
                rank = 2
            elif "头像" in title:
                rank = 3
            else:
                rank = 4
            return rank, -area(item)

        return sorted(files, key=score)

    def score_ys(item: dict[str, Any]) -> tuple[int, int]:
        title = str(item.get("name") or "")
        if title == f"{name}.png":
            rank = 0
        elif "头像" in title:
            rank = 1
        elif "立绘" in title or "角色" in title:
            rank = 2
        else:
            rank = 3
        return rank, -area(item)

    return sorted(files, key=score_ys)


async def _biligame(
    client: httpx.AsyncClient, name: str, site: str, label: str
) -> list[Candidate]:
    data = await _get_json(
        client,
        f"https://wiki.biligame.com/{site}/api.php",
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "allimages",
            "aiprefix": name,
            "ailimit": "50",
            "aiprop": "url|size",
        },
    )
    files = [
        item
        for item in (data.get("query") or {}).get("allimages", [])
        if item.get("url") and int(item.get("width") or 0) >= 80
    ]
    found: list[Candidate] = []
    for item in _rank_biligame(site, name, files)[:4]:
        title = str(item.get("name") or "")
        found.append(
            Candidate(
                url=_https(str(item["url"])),
                source=label,
                page=f"https://wiki.biligame.com/{site}/文件:{quote(title)}",
                referer=f"https://wiki.biligame.com/{site}/",
            )
        )
    return found


# --------------------------------------------------------------------------- #
# 萌娘百科
# --------------------------------------------------------------------------- #


async def _moegirl_page(
    client: httpx.AsyncClient, titles: list[str]
) -> dict[str, Any] | None:
    data = await _get_json(
        client,
        "https://zh.moegirl.org.cn/api.php",
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
            "titles": "|".join(titles),
            "prop": "pageimages",
            "piprop": "original|thumbnail",
            "pithumbsize": "800",
        },
    )
    pages = (data.get("query") or {}).get("pages") or []
    by_title = {str(page.get("title") or ""): page for page in pages}
    normalized = {
        str(item.get("from")): str(item.get("to"))
        for item in (data.get("query") or {}).get("normalized", [])
    }
    for title in titles:
        page = by_title.get(normalized.get(title, title)) or by_title.get(title)
        if page and not page.get("missing"):
            return page
    # 重定向后的标题可能变过，按顺序取第一个存在的页。
    return next((page for page in pages if not page.get("missing")), None)


async def _moegirl(
    client: httpx.AsyncClient, name: str, category: str, found: str = ""
) -> list[Candidate]:
    titles: list[str] = []
    segments = [part for part in (category or "").split("/") if part.strip()]
    # 「原神/角色」这类子目录：先试当前层级，再试上层，最后试不带括号的名字。
    # 分类是内容分组（「普通话」「方言」）时这些标题都不会命中，代价只是一次批量查询。
    for segment in reversed(segments):
        title = f"{name}({segment.strip()})"
        if title not in titles:
            titles.append(title)
    titles.append(name)
    page = await _moegirl_page(client, titles)

    # 重定向可能落到同人 / 梗条目上（「李云龙」在萌百指向「李Saber」），
    # 条目配图就不是这个人了。标题里必须还认得出请求的名字，否则换搜索。
    if page is not None and not _contains_name(str(page.get("title") or ""), name):
        page = None

    if page is None:
        # 搜索词用作品名（「陈 明日方舟」）；内容分类（「陈 普通话」）搜不出东西。
        query = " ".join(part for part in (name, series_title(found) or category) if part)
        data = await _get_json(
            client,
            "https://zh.moegirl.org.cn/api.php",
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "list": "search",
                "srsearch": query,
                "srlimit": "3",
                "srnamespace": "0",
            },
        )
        key = _name_key(name)
        hit = next(
            (
                item
                for item in (data.get("query") or {}).get("search", [])
                if key and key in _name_key(str(item.get("title") or ""))
            ),
            None,
        )
        if hit is not None:
            page = await _moegirl_page(client, [str(hit["title"])])

    if page is None:
        return []
    image = page.get("original") or page.get("thumbnail") or {}
    url = image.get("source")
    if not url:
        return []
    title = str(page.get("title") or name)
    return [
        Candidate(
            url=_https(str(url)),
            source="萌娘百科",
            page=f"https://zh.moegirl.org.cn/{quote(title.replace(' ', '_'))}",
            referer="https://zh.moegirl.org.cn/",
        )
    ]


# --------------------------------------------------------------------------- #
# AniList
# --------------------------------------------------------------------------- #

_ANILIST_QUERY = """
query ($s: String) {
  Page(perPage: 10) {
    characters(search: $s) {
      id
      name { full native alternative }
      image { large }
    }
  }
}
"""


def _names_for(candidate: dict[str, Any]) -> list[str]:
    data = candidate.get("name") or {}
    names = [str(data.get("full") or ""), str(data.get("native") or "")]
    for alias in data.get("alternative") or []:
        text = str(alias)
        names.append(text)
        # 「胡桃 (Hu Tao)」这类别名的括号前也单独比一次。
        names.extend(re.split(r"[()（）]", text))
    return names


async def _anilist(client: httpx.AsyncClient, name: str) -> list[Candidate]:
    data = await _post_json(
        client, "https://graphql.anilist.co", {"query": _ANILIST_QUERY, "variables": {"s": name}}
    )
    characters = (data.get("data") or {}).get("Page", {}).get("characters") or []
    key = _name_key(name)
    found: list[Candidate] = []
    for character in characters:
        if not any(_name_key(alias) == key for alias in _names_for(character)):
            continue
        image = (character.get("image") or {}).get("large")
        if not image:
            continue
        character_id = character.get("id")
        found.append(
            Candidate(
                url=_https(str(image)),
                source="AniList",
                page=f"https://anilist.co/character/{character_id}" if character_id else "https://anilist.co/",
                referer="https://anilist.co/",
            )
        )
    return found


# --------------------------------------------------------------------------- #
# Bing 图片
# --------------------------------------------------------------------------- #


def _bing_query(name: str, category: str, found: str = "") -> str:
    """拼 Bing 关键词。作品名走 :func:`series` 认出来的那个，不看分类字面。"""
    title = series_title(found)
    if title:
        return f"{name} {title}"
    cat = (category or "").casefold()
    if "三国杀" in cat or "sanguosha" in cat:
        return f"{name} 三国杀"
    if "原神" in cat or "genshin" in cat:
        return f"{name} 原神"
    return name


async def _bing(
    client: httpx.AsyncClient, name: str, category: str, found: str = ""
) -> list[Candidate]:
    query = _bing_query(name, category, found)
    params: dict[str, str] = {"q": query, "form": "HDRSC2", "first": "1"}
    real_person = any(word in (category or "") for word in _REAL_PERSON_WORDS)
    if real_person:
        params["qft"] = "+filterui:face"
    response = await client.get("https://cn.bing.com/images/search", params=params)
    if response.status_code >= 400:
        raise SourceError(f"Bing 图片搜索失败（HTTP {response.status_code}）。")

    raw = re.findall(r"murl&quot;:&quot;(.*?)&quot;", response.text)
    raw += re.findall(r'"murl":"(.*?)"', response.text)
    urls: list[str] = []
    for value in raw:
        url = html.unescape(value).replace("\\/", "/")
        url = _https(url.strip())
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    page = str(response.url)
    return [
        Candidate(
            url=url, source="Bing 图片", page=page, referer="https://cn.bing.com/", photo=True
        )
        for url in urls[:10]
    ]
