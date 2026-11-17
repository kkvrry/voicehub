"""端到端冒烟：对着真起的服务把主要端点走一遍，任何一步不对就报出来。

用法：先起服务，再 ``python scripts/smoke.py``（默认打 ``127.0.0.1:17865``，
``--base`` 可改；端口口径与后端同一份——见 ``app.infra.config.DEFAULT_PORT``）。
不依赖 pytest；故意写得直白——它是给人看的体检单，不是回归测试。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 好让下面那行 import 生效

from app.infra.config import DEFAULT_PORT  # 端口只有一处产地

OK = "  ok "
BAD = " FAIL"

_failures: list[str] = []


def _call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """返回 (状态码, 解析后的 JSON 或原始 bytes)。路径里的中文会先做百分号编码。

    ``%`` 必须留在 safe 里：传进来的 ``path`` 有两种。一种是调用点手写的字面路径
    （``/api/v1/voices/胡桃`` 这种，里面的中文要编码）；另一种是**后端已经编码好**的链接
    ——``audio_url`` 就是，文件名是中文，形状是 ``…/%E5%BC%80%E5%BF%83.wav``。
    把后者再编一遍会变成 ``…/%25E5%25BC%2580…``，服务端解出来是字面的 ``%E5…``
    文件名 → 404。从前就是这样，于是「音频流可播」这一项对**任何一个中文名的样例**
    都永远红——而库里几乎每个样例都是中文名。
    """
    safe_path = urllib.parse.quote(path, safe="/?=&%")
    url = base.rstrip("/") + safe_path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if "json" in resp.headers.get("Content-Type", ""):
                payload = json.loads(raw.decode("utf-8"))
                # 集合信封 ``{"object": "list", "data": [...]}``：体检单关心的是
                # "有没有内容"。信封本身由 tests/test_api_contract.py 钉着。
                if isinstance(payload, dict) and payload.get("object") == "list":
                    return resp.status, payload.get("data") or []
                return resp.status, payload
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return exc.code, raw


def check(label: str, cond: bool, detail: str = "") -> None:
    print((OK if cond else BAD) + f" {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def _post_multipart(base: str, path: str, fields: dict[str, str], file_field: tuple) -> tuple:
    """发一个 multipart/form-data 请求（音色克隆要走这条，JSON 发不了文件）。

    ``file_field`` = ``(字段名, 文件名, 内容)``。手搓 multipart 而不是引一个依赖：
    这个脚本的定位是"能拿标准库跑起来的体检单"。
    """
    boundary = "----voicehub-smoke-boundary"
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    field_name, filename, content = file_field
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(base.rstrip("/") + path, data=bytes(body), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return exc.code, raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        default=f"http://127.0.0.1:{os.environ.get('VOICEHUB_PORT') or DEFAULT_PORT}",
    )
    parser.add_argument(
        "--library", default=str(Path(__file__).resolve().parent.parent / "demo_library")
    )
    args = parser.parse_args()
    base = args.base

    print(f"\nVoiceHub 冒烟 @ {base}\n" + "-" * 62)

    status, health = _call(base, "GET", "/api/v1/health")
    check("健康检查", status == 200 and isinstance(health, dict), f"status={status}")

    status, res = _call(base, "PUT", "/api/v1/folders", {"folders": [args.library]})
    check(
        "登记扫描根目录并立刻重扫",
        status == 200 and isinstance(res, dict) and res.get("voices", 0) > 0,
        f"扫到 {res.get('voices') if isinstance(res, dict) else '?'} 个音色",
    )

    status, tree = _call(base, "GET", "/api/v1/tree")
    check(
        "左栏来源树",
        status == 200 and isinstance(tree, dict) and bool(tree.get("roots")),
        f"根 {len(tree.get('roots', []))} 个" if isinstance(tree, dict) else f"status={status}",
    )
    if isinstance(tree, dict) and tree.get("roots"):
        # 分类是**嵌套树**（``children``，每个节点带 ``count`` 与 ``path``）——
        # 前端照它画左栏的那几层。从前这里读的是 ``roots[0]["categories"]``：
        # 那个键服务端从来没有过（前后端都查不到产它的人），于是这一条**永远报红**，
        # 把"树里到底有没有按分类计数"这件事整个盖住了。
        kids = tree["roots"][0].get("children") or []
        cats = {c.get("name"): c.get("count") for c in kids if isinstance(c, dict)}
        check("树按分类计数", bool(cats), ", ".join(f"{k}×{v}" for k, v in cats.items()))

    vocab = tree.get("vocab", {}) if isinstance(tree, dict) else {}
    check(
        "标签候选词表随树下发（中文只在这一处写）",
        ["female", "女声"] in vocab.get("genders", []) and ["young", "青年"] in vocab.get("ages", []),
        f"性别 {len(vocab.get('genders', []))} 档 / 年龄 {len(vocab.get('ages', []))} 档",
    )

    status, voices = _call(base, "GET", "/api/v1/voices?source=local")
    check(
        "本地音色列表",
        status == 200 and isinstance(voices, list) and len(voices) > 0,
        f"{len(voices) if isinstance(voices, list) else '?'} 个",
    )
    if not isinstance(voices, list) or not voices:
        return _report()
    check("列表不带样例（瘦载荷）", "samples" not in voices[0])

    hutao = next((v for v in voices if v["name"] == "胡桃"), None)
    check("按文件夹名识别音色", hutao is not None)
    if hutao is None:
        return _report()
    check("分类来自上一级文件夹", hutao.get("category") == "原神", f"={hutao.get('category')}")

    status, detail = _call(base, "GET", f"/api/v1/voices/{hutao['id']}")
    check("音色详情", status == 200 and isinstance(detail, dict), f"status={status}")
    if not isinstance(detail, dict):
        return _report()
    samples = detail.get("samples", [])
    check("详情带样例列表", len(samples) >= 3, f"{len(samples)} 个样例")
    check("样例带说话内容（一等字段）", all("text" in s for s in samples))
    check("样例带时长", all(s.get("duration") is not None for s in samples))
    check(
        "情绪标签来自文件名",
        any(s.get("emotion") for s in samples),
        ", ".join(sorted({s.get("emotion", "") for s in samples} - {""})),
    )
    check("推导出主样例", bool(detail.get("main_sample")), f"={detail.get('main_sample')!r}")

    if samples:
        first = samples[0]
        status, blob = _call(base, "GET", first["audio_url"])
        check(
            "音频流可播",
            status == 200
            and isinstance(blob, bytes)
            and blob[:4] in (b"RIFF", b"ID3\x03", b"fLaC"),
            f"{len(blob) if isinstance(blob, bytes) else '?'} 字节",
        )

        status, _ = _call(
            base,
            "PUT",
            f"/api/v1/voices/{hutao['id']}/samples/{first['file']}",
            {"text": "冒烟写入的参考文本", "emotion": "平静"},
        )
        check("编辑样例文本/情绪", status == 200, f"status={status}")
        status, d2 = _call(base, "GET", f"/api/v1/voices/{hutao['id']}")
        ok_txt = isinstance(d2, dict) and any(
            s["file"] == first["file"] and s["text"] == "冒烟写入的参考文本" for s in d2["samples"]
        )
        check("样例编辑落库（sidecar）", ok_txt)

    if len(samples) >= 2:
        pick = samples[1]["file"]
        status, _ = _call(base, "POST", f"/api/v1/voices/{hutao['id']}/main-sample", {"file": pick})
        check("设主样例", status == 200, f"status={status}")
        status, d3 = _call(base, "GET", f"/api/v1/voices/{hutao['id']}")
        check(
            "设主样例生效",
            isinstance(d3, dict) and d3.get("main_sample") == pick,
            f"={d3.get('main_sample')!r}" if isinstance(d3, dict) else "",
        )

    status, _ = _call(
        base,
        "PUT",
        f"/api/v1/voices/{hutao['id']}",
        {"tags": ["少女", "活泼"], "gender": "female", "age": "young", "description": "冒烟写入"},
    )
    check("编辑音色元信息（写 sidecar）", status == 200, f"status={status}")
    status, d4 = _call(base, "GET", f"/api/v1/voices/{hutao['id']}")
    check(
        "元信息编辑落库",
        isinstance(d4, dict)
        and "少女" in (d4.get("tags") or [])
        and d4.get("age") == "young"
        and d4.get("description") == "冒烟写入",
        f"tags={d4.get('tags')}, gender={d4.get('gender')}, age={d4.get('age')}"
        if isinstance(d4, dict)
        else "",
    )
    status, by_age = _call(base, "GET", "/api/v1/voices?age=young&source=local")
    check(
        "按年龄感筛选（标签是一等筛选维度）",
        status == 200
        and isinstance(by_age, list)
        and [v["name"] for v in by_age] == ["胡桃"],
        f"{[v['name'] for v in by_age] if isinstance(by_age, list) else '?'}",
    )
    status, none_age = _call(base, "GET", "/api/v1/voices?age=senior&source=local")
    check(
        "筛不到就是空表（不是把所有音色都退回来）",
        status == 200 and none_age == [],
        f"{len(none_age) if isinstance(none_age, list) else '?'} 个",
    )

    status, _ = _call(base, "POST", f"/api/v1/voices/{hutao['id']}/prefs", {"favorite": True})
    check("收藏（服务级偏好）", status == 200, f"status={status}")

    # 参考样例：音频路径 + 参考文本 + 情绪，一个请求拿全（给 audiobook 的接口）
    if samples:
        status, ref = _call(base, "GET", f"/api/v1/voices/{hutao['id']}/reference")
        check(
            "参考样例（路径+文本+情绪）",
            status == 200 and isinstance(ref, dict) and bool(ref.get("sample_path")),
            f"text={str(ref.get('text'))[:16]!r}" if isinstance(ref, dict) else f"status={status}",
        )

    # 平台音色：**只剩「读」与「移走」**
    #
    # 从前这里有一段"POST 登记一条 → 列表里能看到 → 删掉"的流程。登记口删了：
    # 登记出来的条目没有任何引擎认得它的 id，点了也合成不了。现在平台音色由引擎
    # 自己拉清单填进来（见下面 refresh 那一段），所以这里只验"读"和"移走"还通。
    status, pv = _call(base, "GET", "/api/v1/platform-voices")
    check(
        "平台音色列表（只读）",
        status == 200 and isinstance(pv, list),
        f"{len(pv) if isinstance(pv, list) else '?'} 个",
    )
    status, _ = _call(base, "POST", "/api/v1/platform-voices", {"platform": "x", "id": "y"})
    check(
        "手动登记那条路是关着的（登记出来的音色没有引擎认得）",
        status in (404, 405),
        f"status={status}",
    )
    status, res = _call(base, "DELETE", "/api/v1/platform-voices/minimax:SMOKE_NOPE")
    check(
        "移除一条不存在的平台音色也给一句可读说明（不是 500）",
        status == 404 and "不存在" in str(res.get("detail")),
        f"status={status}",
    )

    # 引擎清单：适配清单是"发现到的全部"，工作清单只含"已启用的"。
    status, catalog = _call(base, "GET", "/api/v1/engines/catalog")
    check(
        "适配清单（含未启用，带启用标记）",
        status == 200
        and isinstance(catalog, list)
        and all("enabled" in e for e in catalog),
        f"{len(catalog)} 台" if isinstance(catalog, list) else f"status={status}",
    )
    catalog_keys = {e["key"] for e in catalog} if isinstance(catalog, list) else set()
    check(
        "内置引擎都被发现（目录扫描，不靠清单硬编码）",
        {"minimax", "gpt_sovits"} <= catalog_keys,
        ", ".join(sorted(catalog_keys)) or "（一个都没有）",
    )
    # 后面几段验的是"启用之后"的形状：先把这两台添加进来（全新配置里一台都没有）。
    for key in ("minimax", "gpt_sovits"):
        _call(base, "PUT", f"/api/v1/engines/{key}/enabled", {"enabled": True})
    status, engines = _call(base, "GET", "/api/v1/engines")
    check(
        "已启用引擎的清单（能力 + 合成契约）",
        status == 200
        and isinstance(engines, list)
        and all("synthesis_spec" in e for e in engines),
        ", ".join(e["key"] for e in engines) if isinstance(engines, list) else "",
    )

    # ---------------- 引擎插件：一处装、处处自描述 ----------------
    by_key = {e["key"]: e for e in engines} if isinstance(engines, list) else {}
    minimax = by_key.get("minimax")
    if not minimax:
        return _report()

    props = minimax.get("settings_schema", {}).get("properties", {})
    check(
        "配置字段自带界面提示（label / widget / secret / choices）",
        props.get("api_key", {}).get("label") == "API Key"
        and props.get("api_key", {}).get("widget") == "password"
        and props.get("api_key", {}).get("secret") is True
        and len(props.get("model", {}).get("choices", [])) > 0,
        f"{len(props)} 个字段",
    )
    check(
        "能力带中文说法（前端不认识任何能力 key）",
        any(n["key"] == "voice_cloning" for n in minimax.get("capability_notes", [])),
        "/".join(n["label"] for n in minimax.get("capability_notes", [])),
    )
    check(
        "映射表按项目规范列全，每行带说明，重点说明逐条下发",
        len(minimax.get("mapping", [])) >= 18
        and all(row.get("note") for row in minimax.get("mapping", []))
        and minimax["mapping_constants"].get("stream") is False
        and bool(minimax.get("mapping_cautions"))
        and "defaults" not in minimax,
        f"{len(minimax.get('mapping', []))} 条映射",
    )
    check(
        "读配置时机密字段被掩码",
        minimax.get("values", {}).get("api_key") == "",
        f"secrets_set={minimax.get('secrets_set')}",
    )

    status, icon = _call(base, "GET", "/api/v1/engines/minimax/icon")
    check(
        "插件图标以 SVG 提供",
        status == 200 and isinstance(icon, bytes) and b"<svg" in icon,
        f"{len(icon) if isinstance(icon, bytes) else '?'} 字节",
    )
    status, res = _call(base, "GET", "/api/v1/engines/nope/settings")
    check(
        "问不存在的引擎：404 + 可读说明 + 把已有的列出来",
        status == 404 and "minimax" in str(res.get("detail", "")),
        str(res.get("detail"))[:56] if isinstance(res, dict) else f"status={status}",
    )

    # 配置读写的三条规矩：掩码、空串=不改、明说才清。
    #
    # **自己造状态、自己收尾——而且只收自己造的那一份**：这几条验的是接口的规矩，
    # 不是用户的那把钥匙。密钥读回来**永远是掩码**，所以脚本没有任何办法把一把真
    # 钥匙还回去；而这里从前是照清不误（还先盖一把假的上去），于是每跑一次冒烟，
    # 用户填的钥匙就没了——"填一次没一次"就是这么来的。现在先看一眼有没有：
    # 有就只验弄不坏它的那几条，需要"先清掉再填"的整段跳过、并如实说出来。
    before = dict(minimax.get("values") or {})  # 机密在这里本来就是空的
    had_key = "api_key" in (minimax.get("secrets_set") or [])
    if had_key:
        check(
            "这台引擎已经填过密钥（那是用户的）——跳过会抹掉它的那两步",
            True,
            "只验掩码与「空串 = 不改」",
        )
    else:
        _call(base, "PUT", "/api/v1/engines/minimax/settings", {"_clear": ["api_key"]})
        status, res = _call(
            base, "PUT", "/api/v1/engines/minimax/settings", {"api_key": "sk-smoke-only"}
        )
        check(
            "写入密钥后读回来仍是掩码，但 secrets_set 说已填过",
            status == 200
            and res.get("values", {}).get("api_key") == ""
            and res.get("secrets_set") == ["api_key"],
            f"ready={res.get('ready')}",
        )
    status, res = _call(base, "PUT", "/api/v1/engines/minimax/settings", {"model": "speech-2.6-hd"})
    check(
        "改别的字段不会把密钥抹掉（空串 = 不改）",
        res.get("secrets_set") == ["api_key"] and res.get("values", {}).get("model") == "speech-2.6-hd",
        f"secrets_set={res.get('secrets_set')}",
    )
    status, res = _call(base, "PUT", "/api/v1/engines/minimax/settings", {"api_key": ""})
    check("接着提交空串，仍然是「不改」", res.get("secrets_set") == ["api_key"])
    if had_key:
        check(
            "「明说 _clear 才真的抹掉」这条跳过——抹掉的会是用户的那把钥匙",
            True,
            "自己造不出来，也就还不回去",
        )
    else:
        status, res = _call(base, "PUT", "/api/v1/engines/minimax/settings", {"_clear": ["api_key"]})
        check(
            "明说 _clear 才真的抹掉，并当场变「未就绪」",
            res.get("secrets_set") == [] and res.get("ready") is False and "API Key" in res.get("note", ""),
            f"note={res.get('note')!r}",
        )
    status, res = _call(base, "PUT", "/api/v1/engines/minimax/settings", {"nonsense": 1})
    check(
        "不认识的配置键被丢掉（不写进 config.json）",
        status == 200 and "nonsense" not in res.get("values", {}),
    )

    # 拉清单：**拉不到不是错误**——200 + ok:false + 一句理由。
    # 只在**真的没钥匙**时才打这一下：有钥匙的话它就是一次真调用，还会把平台的
    # 清单写进库——自检不该顺手花用户的额度。
    if had_key:
        check(
            "有密钥时不试拉清单（那会真去调一次平台）",
            True,
            "要验它得先「没钥匙」，而这里那把是你的",
        )
    else:
        status, res = _call(base, "POST", "/api/v1/engines/minimax/refresh")
        check(
            "没填密钥时拉清单：200 + ok:false + 理由（不是 500）",
            status == 200 and res.get("ok") is False and "API Key" in str(res.get("reason")),
            f"status={status} reason={str(res.get('reason'))[:40]!r}",
        )
    status, res = _call(base, "POST", "/api/v1/engines/nope/refresh")
    check(
        "不存在的引擎：404 + 现有引擎名单（不是 500）",
        status == 404 and "minimax" in str(res.get("detail")),
        f"status={status}",
    )

    # 音色克隆：multipart 走通，且缺密钥时给的是可读说明。
    # 同上：有钥匙的时候这一下**真的会在平台上建一条音色**，绝不打。
    if had_key:
        check(
            "有密钥时不试克隆（那会真的在平台上建一条音色）",
            True,
            "要验它得先「没钥匙」，而这里那把是你的",
        )
    else:
        status, res = _post_multipart(
            base,
            "/api/v1/engines/minimax/clone",
            {"name": "冒烟克隆", "voice_id": "", "transcript": "", "preview_text": "", "model": ""},
            ("file", "smoke.wav", b"RIFF" + b"\x00" * 400),
        )
        check(
            "克隆端点可达，且缺密钥时给可读说明（不是堆栈）",
            status == 400 and "API Key" in str(res.get("detail")),
            f"status={status} detail={str(res.get('detail'))[:44]!r}",
        )
    status, res = _post_multipart(base, "/api/v1/engines/nope/clone", {"name": "x"}, ("file", "a.wav", b"RIFF"))
    check(
        "克隆到不存在的引擎：404 + 可读说明",
        status == 404 and "不存在" in str(res.get("detail")),
        f"status={status}",
    )

    # 收尾：把配置还原成刚才的样子。非机密项照原值写回；密钥**只有自己造的那一版
    # 才清掉**——用户本来就有的那把绝不动（读回来是掩码，清了就还不回去）。
    keep = {k: v for k, v in before.items() if k not in (minimax.get("secret_fields") or [])}
    payload = keep if had_key else {**keep, "_clear": ["api_key"]}
    status, res = _call(base, "PUT", "/api/v1/engines/minimax/settings", payload)
    check(
        "冒烟写过的配置已还原（密钥状态与开跑时一致）",
        res.get("values", {}).get("model") == before.get("model")
        and res.get("secrets_set") == (["api_key"] if had_key else []),
        f"model={res.get('values', {}).get('model')} secrets_set={res.get('secrets_set')}",
    )

    # 音色属性：这一家的清单里**没有** language / gender / age 三个字段，它们是从
    # id、界面名与说明里认出来的（见 app/engines/traits.py），各有各的去处——
    # 语言、性别、年龄感落进**它们自己的三个格子**（筛选栏那三个下拉），
    # 音色特点落成**标签**（那一格天生装一串词）。
    # 四个都在**导入那一刻**算出来并落库（不这么做的话筛不了——筛选走的是 SQL），
    # 所以库里那批**旧版本导入的**记录要刷新一次才带上。那不算失败：
    # 说清"点一次 ⟳ 就有了"即可。
    status, mm_voices = _call(base, "GET", "/api/v1/voices?source=minimax")
    mm_voices = mm_voices if isinstance(mm_voices, list) else []

    # 语言：与性别、年龄感并排的第三个筛选维度（**它从前是一条标签，筛不了**）。
    with_lang = [v for v in mm_voices if v.get("language")]
    if with_lang:
        code = str(with_lang[0]["language"])
        status, res = _call(base, "GET", f"/api/v1/voices?source=minimax&language={code}")
        picked = {v["id"] for v in res} if isinstance(res, list) else set()
        check(
            "语言落成第三个筛选维度（不是一条标签），且能按它筛",
            status == 200 and with_lang[0]["id"] in picked,
            f"{len(with_lang)}/{len(mm_voices)} 条有语言；按 language={code} 筛出 {len(picked)} 条",
        )
    else:
        check(
            "语言：这次没得验——不算失败",
            True,
            (
                f"{len(mm_voices)} 条这一格都还空着（点一次 ⟳ 就有了）"
                if mm_voices
                else "库里还没有平台音色（配好密钥点一次 ⟳ 才有）"
            ),
        )

    # 音色特点：落在 tags 里，**点一下就能筛**（界面那排标签用的就是这个端点）。
    status, cloud = _call(base, "GET", "/api/v1/tags?source=minimax")
    cloud = cloud if isinstance(cloud, list) else []
    if cloud:
        tag = str(cloud[0]["tag"])
        # 标签是中文，_call 内部会做百分号编码（别在外面再编一次，会变成 %25）。
        status, res = _call(base, "GET", f"/api/v1/voices?source=minimax&tag={tag}")
        picked = {v["id"] for v in res} if isinstance(res, list) else set()
        check(
            "音色特点落成标签，且按标签筛得动",
            status == 200 and len(picked) == cloud[0]["count"],
            f"{len(cloud)} 种标签；按「{tag}」筛出 {len(picked)} 条（云里写的是 {cloud[0]['count']}）",
        )
    else:
        check("音色特点：这次没得验——不算失败", True, "库里还没有带标签的音色")

    # 性别与年龄感走的是**另外两个列**（不是标签那一列），所以单独验一次：
    # 库里那批是识别功能之前导入的，两格都空着，要刷新一次才填上。
    with_gender = [v for v in mm_voices if v.get("gender")]
    with_age = [v for v in mm_voices if v.get("age")]
    if with_gender:
        gender = str(with_gender[0]["gender"])
        status, res = _call(base, "GET", f"/api/v1/voices?source=minimax&gender={gender}")
        picked = {v["id"] for v in res} if isinstance(res, list) else set()
        check(
            "性别与年龄感落到了那两个字段上（筛选栏就靠它）",
            status == 200 and with_gender[0]["id"] in picked,
            f"{len(with_gender)} 条有性别、{len(with_age)} 条有年龄；"
            f"按 gender={gender} 筛出 {len(picked)} 条",
        )
    else:
        check(
            "性别与年龄感：这次没得验——不算失败",
            True,
            (
                f"{len(mm_voices)} 条两格都还空着（点一次 ⟳ 就有了）"
                if mm_voices
                else "库里还没有平台音色"
            ),
        )

    # 中转站：**"给可读说明"那几条**。
    #
    # 这里刻意**不去调真引擎**：有密钥的话"给 minimax 发一句"就是一次真生成
    # （按量计费），自检不该顺手动用户的钱。改用两条一定在本地就断掉的路径——
    # 不认识的引擎、不存在的音色——它们验的是同一件事：**说得出为什么不行**。
    if samples:
        status, res = _call(
            base,
            "POST",
            "/api/v1/synthesize",
            {"engine": "nope", "voice_id": hutao["id"], "text": "冒烟一句话"},
        )
        check(
            "网关路由（不认识的引擎也要给可读说明）",
            status == 400 and isinstance(res, dict) and bool(res.get("detail")),
            f"status={status} detail={str(res.get('detail'))[:44]!r}" if isinstance(res, dict) else "",
        )
        status, res = _call(
            base,
            "POST",
            "/api/v1/synthesize",
            {"engine": "minimax", "voice_id": "根本不存在的音色", "text": "冒烟一句话"},
        )
        check(
            "网关路由（音色不存在也要给可读说明，且不会走到那台引擎）",
            status == 400 and isinstance(res, dict) and bool(res.get("detail")),
            f"status={status} detail={str(res.get('detail'))[:44]!r}" if isinstance(res, dict) else "",
        )
        if not had_key:
            status, res = _call(
                base,
                "POST",
                "/api/v1/synthesize",
                {"engine": "minimax", "voice_id": hutao["id"], "text": "冒烟一句话"},
            )
            check(
                "网关路由（没配密钥时也要给可读说明，不是 500）",
                status == 400 and isinstance(res, dict) and bool(res.get("detail")),
                f"status={status} detail={str(res.get('detail'))[:44]!r}" if isinstance(res, dict) else "",
            )

    # ---------------- 本机引擎（GPT-SoVITS）：形状与云端那家完全不同 ----------------
    #
    # 它没有密钥、没有清单、没有克隆，**音色就是你自己的本地音色**。
    # 这一段验的正是这几条"与云端不一样"的地方：声明得对不对、不能做的事
    # 是不是**如实说不能**，以及"服务没起"时那句话指不指向正确的方向。
    gsv = by_key.get("gpt_sovits", {})
    gsv_platforms = {
        p["source"]: p for p in (tree.get("engines") or []) if isinstance(tree, dict)
    }
    entry = gsv_platforms.get("gpt_sovits", {})
    check(
        "本机引擎也出现在左栏（装了就有一条）",
        bool(entry),
        f"{len(gsv_platforms)} 条引擎",
    )
    check(
        "形态由插件声明（本地，不写死名字）",
        entry.get("kind") == "local" and entry.get("kind_label") == "本地",
        f"kind={entry.get('kind')} label={entry.get('kind_label')!r}",
    )
    check(
        "左栏条目带上「合成要参考音频」这条能力（界面拿它决定空列表时说哪句话）",
        entry.get("uses_reference_audio") is True,
        f"uses_reference_audio={entry.get('uses_reference_audio')}",
    )
    check(
        "本机引擎不需要凭据 → 直接就绪",
        entry.get("ready") is True and not entry.get("note"),
        f"ready={entry.get('ready')} note={entry.get('note')!r}",
    )
    status, res = _call(base, "POST", "/api/v1/engines/gpt_sovits/refresh")
    check(
        "点 ⟳ 时如实说「不提供清单」，而不是报一个网络错误",
        status == 200 and res.get("ok") is False and "不提供音色清单" in str(res.get("reason")),
        f"status={status} reason={str(res.get('reason'))[:36]!r}",
    )
    status, res = _post_multipart(
        base,
        "/api/v1/engines/gpt_sovits/clone",
        {"name": "冒烟", "voice_id": "", "transcript": "", "preview_text": "", "model": ""},
        ("file", "smoke.wav", b"RIFF" + b"\x00" * 400),
    )
    check(
        "克隆：如实说这台不支持（不是 500，也不是一颗点了没反应的按钮）",
        status == 400 and "不支持音色克隆" in str(res.get("detail")),
        f"status={status}",
    )
    check(
        "配置里没有密钥这一格（本机服务不需要 Key）",
        (gsv.get("secret_fields") or []) == [],
        f"secret_fields={gsv.get('secret_fields')}",
    )
    check(
        "契约里 reference_audio 是必填项，无落点的参数逐行写明原因",
        gsv.get("synthesis_spec", {}).get("requires") == ["reference_audio"]
        and any(
            row.get("source") == "emotion" and not row.get("target") and row.get("note")
            for row in gsv.get("mapping", [])
        ),
        f"requires={gsv.get('synthesis_spec', {}).get('requires')}",
    )

    # 把地址**指到一个一定没人监听的端口**，验"服务没起"那句话。
    # 这条必须是确定的：9880 上真起了服务的话，这个断言会测出别的东西。
    _, gsv_before = _call(base, "GET", "/api/v1/engines/gpt_sovits/settings")
    _call(base, "PUT", "/api/v1/engines/gpt_sovits/settings", {"base_url": "http://127.0.0.1:1"})
    if samples:
        status, res = _call(
            base,
            "POST",
            "/api/v1/synthesize",
            {"engine": "gpt_sovits", "voice_id": hutao["id"], "text": "冒烟一句话"},
        )
        detail = str(res.get("detail")) if isinstance(res, dict) else ""
        check(
            "本机服务没起时：说「连不上」并指出要去起哪个服务（不是堆栈、也不是代理的话）",
            status == 400 and "连不上" in detail and "api_v2" in detail,
            f"status={status} detail={detail[:60]!r}",
        )
    # 照原值写回——**不能统一写空串**：这一格的默认值由插件给，空串会把
    # "用户没改过"改成"用户明确要求空"，两者在界面上长得不一样。
    status, res = _call(
        base,
        "PUT",
        "/api/v1/engines/gpt_sovits/settings",
        {"base_url": (gsv_before.get("values") or {}).get("base_url", "")},
    )
    check(
        "冒烟改过的本机引擎地址已还原",
        status == 200
        and res.get("values", {}).get("base_url")
        == (gsv_before.get("values") or {}).get("base_url"),
        f"base_url={res.get('values', {}).get('base_url')!r}",
    )

    # ------------------------------ 语音识别 ------------------------------ #
    #
    # ASR **不在这份引擎表里**（那些是"文字→音频"，它是反方向的）。这一段只走
    # "读状态"和"不知道就报可读说明"那几条；不探网络、不触发真的识别。
    status, asr = _call(base, "GET", "/api/v1/asr")
    rows = asr.get("engines", []) if isinstance(asr, dict) else []
    check(
        "识别引擎带自描述（名称 / 就绪 / 输入格式与流式声明）",
        status == 200
        and bool(rows)
        and all(
            row.get("key")
            and row.get("label")
            and "ready" in row
            and isinstance(row.get("input", {}).get("formats"), list)
            and isinstance(row.get("streaming", {}).get("supported"), bool)
            for row in rows
        ),
        f"status={status} " + "/".join(f"{r.get('key')}:{r.get('label')}" for r in rows),
    )
    check(
        "识别引擎带能力全表（支持与不支持都列出来）",
        status == 200
        and bool(rows)
        and all(
            isinstance(row.get("returns"), list)
            and any(
                item.get("key") == "text" and item.get("has") is True
                for item in row["returns"]
            )
            and all("has" in item for item in row["returns"])
            for row in rows
        ),
        f"returns={[len(r.get('returns') or []) for r in rows]}",
    )
    languages = [
        row.get("language_groups") or [] for row in rows if row.get("language_groups")
    ]
    check(
        "识别引擎的语言覆盖逐项登记（组 + 代码 + 名称）",
        bool(languages)
        and all(
            group.get("label") and isinstance(group.get("items"), list) and group["items"]
            and all("name" in item for item in group["items"])
            for groups in languages
            for group in groups
        ),
        f"groups={[len(g) for g in languages]}",
    )
    options = asr.get("options") if isinstance(asr, dict) else None
    check(
        "应用层选项齐全（自动识别 / 写回 / 录音转写 / 语种写回）",
        isinstance(options, dict)
        and all(
            isinstance(options.get(name), bool)
            for name in (
                "auto_transcript",
                "writeback",
                "transcribe_recordings",
                "language_writeback",
            )
        ),
        f"options={options!r}",
    )
    status, res = _call(base, "PUT", "/api/v1/asr/current", {"key": "whisper"})
    check(
        "选一个不存在的识别引擎 → 400 + 可读说明",
        status == 400 and "whisper" in str(res.get("detail")),
        f"status={status} detail={str(res.get('detail'))[:40]!r}",
    )
    # 这条**不假定配没配**：没配引擎时是"还没选识别引擎"，配了时是"没有这个音色"，
    # 两种都是 400/404 + 一句可读说明。要钉的是"不认识的输入不许变成 500"。
    status, res = _call(
        base, "POST", "/api/v1/asr/transcribe", {"voice_id": "没有这条音色", "file": "x.wav"}
    )
    detail = str(res.get("detail")) if isinstance(res, dict) else ""
    check(
        "听一条不存在的样例 → 400/404 + 一句可读说明（不是 500）",
        status in {400, 404} and bool(detail),
        f"status={status} detail={detail[:40]!r}",
    )

    return _report()


def _report() -> int:
    print("-" * 62)
    if _failures:
        print(f"冒烟未通过：{len(_failures)} 项 —— " + "; ".join(_failures))
        return 1
    print("冒烟全通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
