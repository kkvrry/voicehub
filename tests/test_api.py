"""API 冒烟：健康、目录、扫描、详情、编辑、音频流、平台音色、引擎清单。"""

from pathlib import Path

from app.domain.marks import mark_names as _mark_names

#: 大库里的全部规范名：认任意标签的引擎支持面按全集算（见 ``domain.marks``）。
_MARK_NAMES = set(_mark_names())


def _items(resp):
    """集合端点统一信封：取 data 段（兼容仍返回裸数组的旧形状）。"""
    payload = resp.json()
    return payload["data"] if isinstance(payload, dict) and "data" in payload else payload



def test_health(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"


def test_folders_round_trip_and_scan(client, demo_library: Path):
    resp = client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    assert resp.status_code == 200
    assert resp.json()["voices"] == 4
    assert client.get("/api/v1/folders").json()["folders"] == [str(demo_library)]


def test_tree_groups_by_root_and_category(client, demo_library: Path):
    """左栏的节点**只有磁盘上真实存在的目录**。

    演示库里的「散件.wav」直接放在根下，它上面没有目录，所以**不出现节点**——
    那一条由根那一行的 ``total`` 算进来（点根就看到），不另造一个「（未分类）」。
    """
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    tree = client.get("/api/v1/tree").json()
    root = tree["roots"][0]
    children = {node["name"]: node for node in root["children"]}
    assert children["原神"]["count"] == 2
    assert children["明日方舟"]["count"] == 1
    assert "（未分类）" not in children  # 那不是磁盘上的目录，不该是个节点
    assert root["total"] == 4  # 直接放在根下的「散件」照样数得进来


def test_category_filter_includes_sub_levels(client, demo_library: Path):
    """点一个分类节点，它的子层也一起收进来（前缀筛，不只等值）。"""
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    by_cat = _items(client.get("/api/v1/voices", params={"category": "原神"}))
    assert {v["name"] for v in by_cat} == {"胡桃", "钟离"}


def test_uncategorized_filter_folds_back_to_empty(client, demo_library: Path):
    """旧界面那个「（未分类）」仍然认——折回库里的空串（直接放在根下的音色）。

    现在的左栏不造这个节点了，但浏览器里可能还留着上一份前端包，而它传上来的是
    这个词。折一下总比"点进去 0 条、计数却在"好（库里从来没存过这个词）。
    """
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    found = _items(client.get("/api/v1/voices", params={"category": "（未分类）"}))
    assert {v["name"] for v in found} == {"散件"}


def test_voice_list_and_detail(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    voices = _items(client.get("/api/v1/voices", params={"source": "local"}))
    assert len(voices) == 4
    assert "samples" not in voices[0]  # 列表不带样例
    hutao = next(v for v in voices if v["name"] == "胡桃")
    detail = _items(client.get(f"/api/v1/voices/{hutao['id']}"))
    assert len(detail["samples"]) == 3
    mains = [s for s in detail["samples"] if s["is_main"]]
    assert mains[0]["file"] == "默认.wav"
    happy = next(s for s in detail["samples"] if s["file"] == "开心.wav")
    assert happy["text"] == "吃饱喝饱，一路走好！"
    assert happy["duration"] > 0


def test_voice_filtering(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    voices = _items(client.get("/api/v1/voices", params={"q": "桃"}))
    assert [v["name"] for v in voices] == ["胡桃"]
    by_cat = _items(client.get("/api/v1/voices", params={"category": "原神"}))
    assert {v["name"] for v in by_cat} == {"胡桃", "钟离"}


def test_update_voice_and_main_sample(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")
    resp = client.put(
        f"/api/v1/voices/{hutao['id']}", json={"gender": "female", "tags": ["活泼"]}
    )
    assert resp.json()["gender"] == "female"
    bad = client.put(f"/api/v1/voices/{hutao['id']}", json={"gender": "robot"})
    assert bad.status_code == 400
    main = client.post(
        f"/api/v1/voices/{hutao['id']}/main-sample", json={"file": "开心.wav"}
    ).json()
    assert main["main_sample"] == "开心.wav"
    gone = client.post(
        f"/api/v1/voices/{hutao['id']}/main-sample", json={"file": "不存在.wav"}
    )
    assert gone.status_code == 400


def test_reveal_opens_the_voice_folder(client, demo_library: Path, monkeypatch):
    """「打开文件夹」：本地音色交给系统打开它自己的目录；没有目录的明说，不开空窗口。

    测试里把打开目录的实现换掉——不换的话每跑一次冒烟就在桌面弹一堆资源管理器。
    """
    opened: list[str] = []
    monkeypatch.setattr("app.api.sections.library.open_folder", lambda path: opened.append(str(path)))
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")
    resp = client.post(f"/api/v1/voices/{hutao['id']}/reveal")
    assert resp.status_code == 200
    assert opened and opened[0].endswith("胡桃")
    assert Path(opened[0]).is_dir()


def test_reveal_is_refused_for_a_lan_client(client, demo_library: Path, monkeypatch):
    """别人机器上点「打开目录」不该落到宿主机的桌面。"""
    from starlette.testclient import TestClient

    called: list[str] = []
    monkeypatch.setattr("app.api.sections.library.open_folder", lambda path: called.append(str(path)))
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")

    lan = TestClient(client.app, client=("192.168.1.50", 50000))
    resp = lan.post(f"/api/v1/voices/{hutao['id']}/reveal")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "reveal_local_only"
    assert called == []  # 一个窗口都没弹


def test_sample_patch_and_prefs(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")
    resp = client.put(
        f"/api/v1/voices/{hutao['id']}/samples/难过.wav",
        json={"text": "呜……", "emotion": "难过"},
    )
    sad = next(s for s in resp.json()["samples"] if s["file"] == "难过.wav")
    assert sad["text"] == "呜……"
    prefs = client.post(f"/api/v1/voices/{hutao['id']}/prefs", json={"favorite": True}).json()
    assert prefs["favorite"] is True


def test_audio_streaming(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")
    resp = client.get(f"/api/v1/audio/{hutao['id']}/默认.wav")
    assert resp.status_code == 200
    assert resp.content[:4] == b"RIFF"
    missing = client.get(f"/api/v1/audio/{hutao['id']}/没有.wav")
    assert missing.status_code == 404


def test_a_hash_in_the_file_name_does_not_break_playback(client, demo_library: Path):
    """文件名里的 ``#`` 必须被编码——不然那颗 ▶ 点了**一点声音都没有，也不报错**。

    素材是用户自己命名的，而这类命名里带 ``#`` 的很常见（``平静#ja#…wav``，
    ``#`` 后面是语言标记）。``#`` 在 URL 里是**片段分隔符**：后端给的地址不编码的话，
    浏览器在它那里就把路径切断了，服务端收到的是半截 → 404。

    这条钉的是"后端下发的 `audio_url` **原样**交给浏览器也能取到那段音频"——
    所以直接把那个地址喂回 client 解一次（这正是浏览器会做的事），而不是断言
    某一个写死的字符串。
    """
    from conftest import make_wav

    name = "平静#ja#何も変わらない.wav"
    make_wav(demo_library / "原神" / "胡桃" / name)
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")

    # 卡片上那颗 ▶ 用的是主样例的地址（列表不带 samples，所以它是单独一格）。
    client.post(f"/api/v1/voices/{hutao['id']}/main-sample", json={"file": name})
    listed = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")
    assert "#" not in listed["main_audio_url"]
    assert client.get(listed["main_audio_url"]).status_code == 200

    # 详情里每一条样例的地址同样要是能直接用的。
    detail = _items(client.get(f"/api/v1/voices/{hutao['id']}"))
    sample = next(s for s in detail["samples"] if s["file"] == name)
    assert "#" not in sample["audio_url"]
    assert client.get(sample["audio_url"]).content[:4] == b"RIFF"


def test_platform_voice_removal(client, seed_platform_voice):
    """平台音色只剩「读」与「移走」。

    手动登记（POST）已经删掉——登记出来的条目没有任何引擎认得它的 id，
    点了也合成不了（能拉清单的引擎自己拉，不能拉的用本地音色）。
    """
    seed_platform_voice(voice_id="MX_001", name="林晚")
    listed = _items(client.get("/api/v1/platform-voices"))
    assert [v["shown_name"] for v in listed] == ["林晚"]
    assert client.delete("/api/v1/platform-voices/minimax:MX_001").status_code == 200
    assert _items(client.get("/api/v1/platform-voices")) == []
    # 登记那条路必须是关着的（405/404 都算关着——别让它哪天悄悄回来）
    assert client.post("/api/v1/platform-voices", json={"platform": "x", "id": "y"}).status_code >= 400


def test_the_language_survives_the_whole_trip_as_a_field(client, monkeypatch):
    """语言走通**全链**：插件的解析 → 入库 → API 输出 → 作为筛选维度。

    单测钉的是"解析出来的东西对不对"（``tests/test_traits.py``），这条钉的是
    **它有没有活到界面上**——入库或序列化那两步丢了它的话，只测解析是看不出来的
    （那两步是通用的，谁都不会想到去怀疑它们）。

    它从前是一条挂在 ``tags`` 里的字；那条路的毛病是筛不了，所以这里最后一步
    验的正是**筛选**。
    """
    from app.engines.base import VoiceList
    from app.engines.builtin.minimax.plugin import MinimaxEngine
    from app.engines.builtin.minimax.voices import parse_voice_list

    # 一份**真实形状**的响应：一条从 id 认得出语言，一条两处都没提（`Arnold`）。
    payload = {
        "system_voice": [
            {"voice_id": "Cantonese_CuteGirl", "voice_name": "Cantonese_CuteGirl"},
            {"voice_id": "Arnold", "voice_name": "Arnold"},
        ],
        "base_resp": {"status_code": 0},
    }

    # **打在类上**：注册表每次都会新建一个实例，打在实例上根本不会被调到。
    async def fake_list(self, cfg):
        return VoiceList(items=parse_voice_list(payload))

    monkeypatch.setattr(MinimaxEngine, "list_voices", fake_list)
    assert client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-test"}).status_code == 200
    assert client.post("/api/v1/engines/minimax/refresh").json()["added"] == 2

    listed = {v["name"]: v for v in _items(client.get("/api/v1/voices?source=minimax"))}
    assert listed["Cantonese_CuteGirl"]["language"] == "yue"
    assert listed["Arnold"]["language"] == ""  # 没提语言的就空着（不猜）

    # 光输出这一格还不够——它是**第三个筛选维度**，下拉的候选得由后端给
    vocab = client.get("/api/v1/tree").json()["vocab"]
    assert ["yue", "粤语"] in [list(pair) for pair in vocab["languages"]]

    only_cantonese = _items(client.get("/api/v1/voices?source=minimax&language=yue"))
    assert [v["name"] for v in only_cantonese] == ["Cantonese_CuteGirl"]


def test_a_characteristic_tag_survives_the_whole_trip(client, monkeypatch):
    """音色特点落成标签，并且**点一下就能筛**——那是它唯一的目的。

    语言走的是自己的字段、 gender/age 也各有格子；特点是真的活在 ``tags`` 里的，
    所以这条验的是 JSON 列那条路还通着（外加 ``tag=`` 筛选这条老路）。
    """
    from app.engines.base import VoiceList
    from app.engines.builtin.minimax.plugin import MinimaxEngine
    from app.engines.builtin.minimax.voices import parse_voice_list

    payload = {
        "system_voice": [
            {
                "voice_id": "Chinese (Mandarin)_Kind-hearted_Antie",
                "voice_name": "热心大婶",
                "description": ["一位温和善良的中年大婶声音，标准普通话，温暖而体贴。"],
            },
            {"voice_id": "Santa_Claus", "voice_name": "Santa Claus"},
        ],
        "base_resp": {"status_code": 0},
    }

    async def fake_list(self, cfg):
        return VoiceList(items=parse_voice_list(payload))

    monkeypatch.setattr(MinimaxEngine, "list_voices", fake_list)
    assert client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-test"}).status_code == 200
    assert client.post("/api/v1/engines/minimax/refresh").json()["added"] == 2

    listed = {v["name"]: v for v in _items(client.get("/api/v1/voices?source=minimax"))}
    assert listed["Chinese (Mandarin)_Kind-hearted_Antie"]["tags"] == ["善良", "温暖", "体贴"]
    assert listed["Santa_Claus"]["tags"] == []  # 说明里没写一个特点 = 不挂（不猜）

    warm = _items(client.get("/api/v1/voices?source=minimax&tag=温暖"))
    assert [v["name"] for v in warm] == ["Chinese (Mandarin)_Kind-hearted_Antie"]


def test_gender_and_age_survive_the_whole_trip(client, monkeypatch):
    """性别与年龄感也走全链：**认出来 → 写进它们那两个字段 → 筛选栏筛得动**。

    与标签那条是两回事：标签走的是 ``tags``（一个 JSON 列），这两项走的是
    ``gender``/``age``（两个列）。写错了的症状很安静——界面上那格空着，
    而"空着"与"识别没认出来"长得一模一样。
    """
    from app.engines.base import VoiceList
    from app.engines.builtin.minimax.plugin import MinimaxEngine
    from app.engines.builtin.minimax.voices import parse_voice_list

    payload = {
        "system_voice": [
            {
                "voice_id": "female-shaonv",
                "voice_name": "少女音色",
                "description": ["一位温暖清脆的少女声音。"],
            },
            {
                "voice_id": "Chinese (Mandarin)_Humorous_Elder",
                "voice_name": "搞笑大爷",
                "description": ["一位爽朗幽默的老年男性大爷声音。"],
            },
            {"voice_id": "Arnold", "voice_name": "Arnold"},
        ],
        "base_resp": {"status_code": 0},
    }

    async def fake_list(self, cfg):
        return VoiceList(items=parse_voice_list(payload))

    monkeypatch.setattr(MinimaxEngine, "list_voices", fake_list)
    assert client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-test"}).status_code == 200
    assert client.post("/api/v1/engines/minimax/refresh").json()["added"] == 3

    listed = {v["name"]: v for v in _items(client.get("/api/v1/voices?source=minimax"))}
    assert (listed["female-shaonv"]["gender"], listed["female-shaonv"]["age"]) == ("female", "teen")
    assert listed["female-shaonv"]["shown_name"] == "少女音色"
    elder = listed["Chinese (Mandarin)_Humorous_Elder"]
    assert (elder["gender"], elder["age"]) == ("male", "senior")
    assert elder["shown_name"] == "搞笑大爷"
    assert (listed["Arnold"]["gender"], listed["Arnold"]["age"]) == ("", "")

    # 筛选栏那两个下拉就靠这两条：认出来了才筛得动
    old_men = _items(client.get("/api/v1/voices?source=minimax&gender=male&age=senior"))
    assert [v["name"] for v in old_men] == ["Chinese (Mandarin)_Humorous_Elder"]
    assert _items(client.get("/api/v1/voices?source=minimax&gender=female&age=senior")) == []


def test_a_refresh_does_not_overwrite_what_the_user_typed(client, monkeypatch):
    """**猜的不许盖掉手改的。** 用户改过年龄感之后点一次刷新，那一格要还在。

    这条缝很难自己发现：刷新是"拉一下新清单"，看着与编辑无关；而症状是用户改的值
    悄悄变回平台那句话里的值，他不会想到是刷新干的。名字/说明/标签与之相反——
    那些是平台的数据，平台改了就该跟着改（下一条断言就是它）。
    """
    from app.engines.base import VoiceList
    from app.engines.builtin.minimax.plugin import MinimaxEngine
    from app.engines.builtin.minimax.voices import parse_voice_list

    payload = {
        "system_voice": [
            {
                "voice_id": "female-shaonv",
                "voice_name": "少女音色",
                "description": ["一位温暖清脆的少年女声。"],
            }
        ],
        "base_resp": {"status_code": 0},
    }

    async def fake_list(self, cfg):
        return VoiceList(items=parse_voice_list(payload))

    monkeypatch.setattr(MinimaxEngine, "list_voices", fake_list)
    client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-test"})
    client.post("/api/v1/engines/minimax/refresh")

    voice = next(v for v in _items(client.get("/api/v1/voices?source=minimax")) if v["name"] == "female-shaonv")
    assert voice["age"] == "teen"  # 识别填的
    assert client.put(f"/api/v1/voices/{voice['id']}", json={"age": "middle"}).status_code == 200

    # 平台那边把说明改了一个字，再刷新一次
    payload["system_voice"][0]["description"] = ["一位温暖清脆的少年女声，标准普通话。"]
    client.post("/api/v1/engines/minimax/refresh")

    again = next(v for v in _items(client.get("/api/v1/voices?source=minimax")) if v["name"] == "female-shaonv")
    assert again["age"] == "middle", "用户手改的年龄感被刷新盖掉了"
    assert again["gender"] == "female"  # 那一格没改过，照样是识别填的
    # 说明是平台的数据，平台改了就该跟着改；语言这一次才被平台写出来（新说明多了
    # "标准普通话"），所以它是个好探针：顺着它就看得出"平台的数据有没有真的跟着动"。
    assert again["description"].endswith("标准普通话。")
    assert again["language"] == "zh"

    # 标签这一轮起**同样适用"猜的不许盖掉手改的"**——它现在是认出来的（音色特点），
    # 不再是从平台原样抄来的。与其在标签里区分"用户打的"与"我们认的"，不如统一保守。
    assert client.put(f"/api/v1/voices/{voice['id']}", json={"tags": ["我自己写的"]}).status_code == 200
    client.post("/api/v1/engines/minimax/refresh")
    third = next(v for v in _items(client.get("/api/v1/voices?source=minimax")) if v["name"] == "female-shaonv")
    assert third["tags"] == ["我自己写的"], "用户手打的标签被刷新盖掉了"


def test_reference_endpoint(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")
    ref = _items(client.get(f"/api/v1/voices/{hutao['id']}/reference"))
    assert ref["sample_path"].endswith("默认.wav")


def test_engines_listing_is_complete_for_every_engine(client):
    """每个引擎的自描述都得**够前端渲染**——不点名某一家，这样加引擎不用改这条。"""
    engines = _items(client.get("/api/v1/engines"))
    assert engines, "一个引擎都没有——发现机制坏了"
    for one in engines:
        assert {"key", "label", "kind", "settings_schema", "synthesis_spec"} <= set(one)
        assert "properties" in one["settings_schema"]


def test_mark_groups_are_computed_across_engines(client):
    """「通用」是**跨引擎**算的：支持语气词的那几台的交集，不支持的不参与。

    这条只钉"算得对"的三件事（交集、谁多出来、参与者是谁），不点名任何一台引擎
    ——加一台新引擎时它自己就该进到这几份里去。
    """
    groups = client.get("/api/v1/marks").json()
    engines = {e["key"]: e for e in _items(client.get("/api/v1/engines"))}

    # 参与者 = 声明了标签（或认任意标签）的那些（没声明的把交集压成空）。
    participants = {p["key"] for p in groups["participants"]}
    assert participants == {
        key for key, e in engines.items() if e["marks"] or e["marks_passthrough"]
    }

    def names_of(key):
        # 认任意标签的引擎支持面是全集（见 domain.marks.mark_groups）。
        marks = {m["name"] for m in engines[key]["marks"]}
        return marks | _MARK_NAMES if engines[key]["marks_passthrough"] else marks

    expected = set(_MARK_NAMES)
    for key in participants:
        expected &= names_of(key)
    assert {m["name"] for m in groups["common"]} == expected

    for group in groups["engines"]:
        assert {m["name"] for m in group["marks"]} == {
            m["name"] for m in engines[group["key"]]["marks"]
        } - expected


def test_tree_carries_the_vocab(client):
    """标签候选随 /api/v1/tree 下发——前端不再自己写一份「女声/男声」。"""
    from app.domain.models import TIMBRES, TONES

    vocab = client.get("/api/v1/tree").json()["vocab"]
    assert ["female", "女声"] in vocab["genders"]
    assert ["young", "青年"] in vocab["ages"]
    # 角色卡的两组固定标签：语调（怎么念）与音色（声音质地）。词表只有一处产地，
    # 这里比对的是**同一份常量**——写死数字只会让每次改词表都得来改这一行。
    assert ["温柔", "温柔"] in vocab["tones"]
    assert ["磁性", "磁性"] in vocab["timbres"]
    assert len(vocab["tones"]) == len(TONES)
    assert len(vocab["timbres"]) == len(TIMBRES)
    # 两组都要有不好听的词：只有夸人的词，这张表就找不回"平淡、絮叨"那些音色。
    for word in ("平淡", "木讷", "絮叨"):
        assert [word, word] in vocab["tones"], word
    for word in ("单薄", "发闷", "含混"):
        assert [word, word] in vocab["timbres"], word


def test_voices_can_be_filtered_by_age(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    hutao = next(v for v in _items(client.get("/api/v1/voices")) if v["name"] == "胡桃")
    assert client.put(f"/api/v1/voices/{hutao['id']}", json={"age": "young"}).status_code == 200
    hit = _items(client.get("/api/v1/voices", params={"age": "young"}))
    assert [v["name"] for v in hit] == ["胡桃"]
    assert _items(client.get("/api/v1/voices", params={"age": "senior"})) == []


class TestEngineEndpoints:
    """引擎插件在 HTTP 上的那一圈：自描述、图标、配置、拉清单、克隆。"""

    def test_listing_is_self_describing(self, client):
        engines = {e["key"]: e for e in _items(client.get("/api/v1/engines"))}
        minimax = engines["minimax"]
        # 界面渲染表单与说明要的每一样都在这一份里，前端不认识任何引擎专有的键
        assert minimax["settings_schema"]["properties"]["api_key"]["label"] == "API Key"
        assert minimax["secret_fields"] == ["api_key"]
        assert minimax["capability_notes"]
        assert minimax["mapping_constants"] == {"stream": False}
        assert minimax["kind_label"] == "云端"

    def test_secrets_are_never_returned(self, client):
        client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-should-not-leak"})
        payload = _items(client.get("/api/v1/engines/minimax/settings"))
        assert payload["values"]["api_key"] == ""
        assert payload["secrets_set"] == ["api_key"]
        assert "sk-should-not-leak" not in client.get("/api/v1/engines").text

    def test_empty_secret_keeps_the_old_one(self, client):
        client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-keep"})
        client.put("/api/v1/engines/minimax/settings", json={"api_key": "", "model": "speech-2.6-hd"})
        payload = _items(client.get("/api/v1/engines/minimax/settings"))
        assert payload["secrets_set"] == ["api_key"]
        assert payload["values"]["model"] == "speech-2.6-hd"
        assert payload["ready"] is True

    def test_clear_really_erases_it(self, client):
        client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-keep"})
        payload = client.put("/api/v1/engines/minimax/settings", json={"_clear": ["api_key"]}).json()
        assert payload["secrets_set"] == []
        assert payload["ready"] is False
        assert "API Key" in payload["note"]

    def test_unknown_fields_are_dropped(self, client):
        payload = client.put(
            "/api/v1/engines/minimax/settings", json={"nonsense": 1, "model": "speech-2.6-hd"}
        ).json()
        assert "nonsense" not in payload["values"]
        assert payload["values"]["model"] == "speech-2.6-hd"

    def test_settings_of_an_unknown_engine_are_404_with_words(self, client):
        resp = client.get("/api/v1/engines/nope/settings")
        assert resp.status_code == 404
        assert "minimax" in resp.json()["detail"]

    def test_icon_is_served_as_svg(self, client):
        resp = client.get("/api/v1/engines/minimax/icon")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in resp.content

    def test_icon_of_an_unknown_engine_is_404(self, client):
        assert client.get("/api/v1/engines/nope/icon").status_code == 404

    def test_refresh_without_credentials_explains_instead_of_failing(self, client):
        """拉不到 → **200 + ok:false + 理由**：空列表与"这次没拉到"必须分得开。"""
        resp = client.post("/api/v1/engines/minimax/refresh")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "API Key" in body["reason"]

    def test_clone_requires_a_source(self, client):
        resp = client.post("/api/v1/engines/minimax/clone", data={"name": "x"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "clone_source_required"

    def test_design_voice_needs_no_source(self, client, library):
        """设计音色只交描述：不传音源也能建，描述作为凭据写进库内音色。"""
        resp = client.post(
            "/api/v1/engines/mimo/clone",
            data={"name": "老者旁白", "design_prompt": "低沉的老者。"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "老者旁白"
        voice = library.get_voice(body["voice_id"])
        assert voice.payload["design_prompt"] == "低沉的老者。"

    def test_design_on_an_engine_without_it_is_a_400(self, client):
        resp = client.post(
            "/api/v1/engines/minimax/clone",
            data={"name": "x", "design_prompt": "低沉男声"},
        )
        assert resp.status_code == 400
        assert "不支持音色设计" in resp.json()["detail"]

    def test_clone_from_a_library_sample_checks_the_file(self, client, seed_platform_voice):
        voice = seed_platform_voice(voice_id="MX_V", name="林晚")
        resp = client.post(
            "/api/v1/engines/minimax/clone",
            data={
                "name": "克隆",
                "source_voice_id": voice.id,
                "source_file": "不在磁盘上.wav",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "clone_source_missing"

    def test_clone_without_credentials_says_so_in_words(self, client):
        resp = client.post(
            "/api/v1/engines/minimax/clone",
            data={"name": "试一下"},
            files={"file": ("a.wav", b"RIFF" + b"\x00" * 400, "audio/wav")},
        )
        assert resp.status_code == 400
        assert "API Key" in resp.json()["detail"]

    def test_clone_on_an_engine_that_cannot_clone_is_a_400(self, client):
        """引擎在、只是不做这件事 → 400（404 会让人去查引擎名拼错了没有）。"""
        resp = client.post(
            "/api/v1/engines/gpt_sovits/clone",
            data={"name": "x"},
            files={"file": ("a.wav", b"RIFF" + b"\x00" * 400, "audio/wav")},
        )
        assert resp.status_code == 400
        assert "不支持音色克隆" in resp.json()["detail"]

    def test_clone_on_an_unknown_engine_is_404(self, client):
        resp = client.post(
            "/api/v1/engines/nope/clone",
            data={"name": "x"},
            files={"file": ("a.wav", b"RIFF", "audio/wav")},
        )
        assert resp.status_code == 404

    def test_tree_lists_installed_engines_even_with_no_voices(self, client):
        """**装了就有一条**——配好密钥却找不到入口是说不通的。"""
        platforms = {p["source"]: p for p in client.get("/api/v1/tree").json()["engines"]}
        assert platforms["minimax"]["total"] == 0
        assert platforms["minimax"]["can_list"] is True
        assert platforms["minimax"]["can_clone"] is True


class TestEngineChoiceAndVoiceEndpoints:
    """「这台引擎认得哪些情绪」与「这台引擎有哪些音色」。

    这两条是给**上游**用的（有声书的 ``engine`` 下拉与音色候选）。清单只有一个
    产地：引擎自己声明什么就是什么，上游不抄一份回家。
    """

    def test_choices_name_every_installed_engine(self, client):
        keys = {item["key"] for item in _items(client.get("/api/v1/engines/choices"))}

        assert "minimax" in keys
        assert keys == {one["key"] for one in _items(client.get("/api/v1/engines"))}

    def test_choices_carry_readiness_so_the_upstream_can_grey_it_out(self, client):
        """能不能用要**带在清单里**——上游不必为了画个灰态再发一次请求。"""
        item = next(
            one for one in _items(client.get("/api/v1/engines/choices")) if one["key"] == "minimax"
        )

        assert item["ready"] is False
        assert "API Key" in item["note"]

    def test_the_emotion_words_the_engine_accepts_come_back(self, client):
        """情绪词表随引擎一起下发，上游照它逐行比对（认不出就标无效）。"""
        engines = {e["key"]: e for e in _items(client.get("/api/v1/engines"))}
        words = engines["minimax"]["value_choices"]["emotion"]

        assert "开心" in words
        assert "兴奋" in words
        assert "激动" in words, "同义词也要算认得（激动 → 兴奋）"
        assert "恍惚" not in words, "词表外的词不下发，上游据此标无效"

    def test_voices_of_an_engine_that_cannot_list_say_why(self, client):
        """不提供清单的引擎**说清为什么**，不给一个空列表就完了。"""
        body = _items(client.get("/api/v1/engines/gpt_sovits/voices"))

        assert body["ok"] is False
        assert body["items"] == []
        assert body["reason"]

    def test_voices_of_an_unknown_engine_is_404(self, client):
        assert client.get("/api/v1/engines/nope/voices").status_code == 404

    def test_synthesize_routes_to_the_plugin_and_reports_its_words(self, client, seed_platform_voice):
        """没密钥时也要是**插件自己的话**，不是中转站编的一句。"""
        seed_platform_voice(voice_id="MX_V", name="林晚")
        resp = client.post(
            "/api/v1/synthesize",
            json={
                "engine": "minimax",
                "voice_id": "minimax:MX_V",
                "text": "你好",
                "extra": {"speed": 1.2},
            },
        )
        assert resp.status_code == 400
        assert "API Key" in resp.json()["detail"]

    def test_synthesize_refuses_an_out_of_range_value_before_any_request(
        self, client, seed_platform_voice
    ):
        """区间在**发出去之前**拦下——这条不需要密钥就能验到（拦在更早的一步）。"""
        client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-not-real"})
        seed_platform_voice(voice_id="MX_V", name="林晚")
        resp = client.post(
            "/api/v1/synthesize",
            json={
                "engine": "minimax",
                "voice_id": "minimax:MX_V",
                "text": "你好",
                "extra": {"speed": 9},
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "没有发出去" in detail
        assert "0.5" in detail


def test_unknown_voice_says_so(client):
    resp = client.get("/api/v1/voices/nope")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]
    assert resp.json()["error"]["code"] == "voice_not_found"


class TestStaticCacheHeaders:
    """**首页不许被缓存住**（``/assets/`` 那批相反，长缓存）。

    ``index.html`` 里写着当前那个 ``index-<hash>.js`` 的名字——它一旦进了浏览器缓存，
    整份旧界面就被钉住：新的 ``dist`` 在磁盘上，浏览器却照着缓存里那份 HTML 去取
    **已经不存在的旧包**。本机工具没人会去清缓存，症状是"更新了没反应"。
    """

    def test_index_is_revalidated_every_time(self, client):
        resp = client.get("/")
        if resp.status_code == 404:  # pragma: no cover - 没构建过就没有静态挂载
            import pytest

            pytest.skip("没有 web/dist——静态挂载没起来")
        assert resp.headers["cache-control"] == "no-cache"

    def test_hashed_assets_are_immutable(self, client):
        from pathlib import Path

        assets = Path(__file__).resolve().parent.parent / "web" / "dist" / "assets"
        bundles = sorted(assets.glob("index-*.js")) if assets.is_dir() else []
        if not bundles:
            import pytest

            pytest.skip("没有 web/dist/assets——静态挂载没起来")
        resp = client.get(f"/assets/{bundles[0].name}")
        assert resp.status_code == 200
        assert "immutable" in resp.headers["cache-control"]

    def test_api_responses_are_not_given_an_html_cache_header(self, client):
        """中间件是照 ``content-type`` 认首页的——**别把接口也一起标上**。

        断言写成"不属于那两类"而不是"必须没有这个头"：将来谁给某个接口加上自己的
        缓存策略（比如时长探测那种不会变的），不该被这条测试拦住。
        """
        header = client.get("/api/v1/health").headers.get("cache-control", "")
        assert "immutable" not in header and header != "no-cache"


def test_tts_endpoint_takes_the_readers_form_post(client, monkeypatch) -> None:
    """「阅读」的朗读引擎发的是表单，``speakSpeed`` 是语速 × 10（10 = 原速）。

    这一层只做形状转换：正文、音色直传，语速换算成倍率，指令与强度原样进
    ``extra``，之后走与 ``/synthesize`` 同一条网关。
    """
    from app.engines.base import SynthesisResult

    seen = {}

    async def fake_synthesize(service, **kwargs):
        seen.update(kwargs)
        return SynthesisResult(audio=b"RIFF-fake", format="wav")

    monkeypatch.setattr("app.api.sections.studio.gateway.synthesize", fake_synthesize)
    resp = client.post(
        "/api/v1/tts",
        data={
            "text": "你好",
            "engine": "minimax",
            "voice_id": "minimax:MX_V",
            "speak_speed": "17",
            "instruction": "用平静的语气",
            "guidance_scale": "0.6",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == b"RIFF-fake"
    assert seen["text"] == "你好"
    assert seen["extra"] == {
        "speed": 1.7,
        "instruction": "用平静的语气",
        "guidance_scale": 0.6,
    }


def test_tts_endpoint_refuses_a_guidance_outside_zero_to_one(client) -> None:
    resp = client.get(
        "/api/v1/tts",
        params={
            "text": "你好",
            "engine": "breeze_tts",
            "voice_id": "voice",
            "guidance_scale": "4",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "tts_guidance_range"


def test_tts_endpoint_refuses_an_empty_text(client) -> None:
    resp = client.get("/api/v1/tts", params={"engine": "minimax", "voice_id": "minimax:MX_V"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "tts_text_required"


def test_missing_key_is_not_repeated_as_a_hint(client) -> None:
    """缺密钥只标 ``needs_key``：界面靠「未就绪」徽标，面板里不再复述一句提示。

    这条盯的是"提示只剩徽标"这个产品决定——``note`` 仍留着给接口调用方，
    但不能再带"点右边的齿轮"这种指向已不存在的按钮的指引。
    """
    before = {row["key"]: row for row in client.get("/api/v1/engines").json()["data"]}["minimax"]
    assert before["ready"] is False
    assert before["needs_key"] is True
    assert "齿轮" not in before["note"]

    client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-test"})
    after = {row["key"]: row for row in client.get("/api/v1/engines").json()["data"]}["minimax"]
    assert after["ready"] is True
    assert after["needs_key"] is False


# ---- 上传边界与路径归属 ----------------------------------------------------

def test_clone_upload_is_capped_before_the_engine_is_called(client, monkeypatch) -> None:
    monkeypatch.setattr("app.api.sections.engines._MAX_CLONE_BYTES", 1024)
    resp = client.post(
        "/api/v1/engines/minimax/clone",
        data={"name": "超限"},
        files={"file": ("a.wav", b"0" * 2048, "audio/wav")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "audio_too_large"


def test_clone_rejects_a_source_outside_the_library(client, seed_platform_voice) -> None:
    voice = seed_platform_voice(voice_id="MX_ESC", name="林晚")
    resp = client.post(
        "/api/v1/engines/minimax/clone",
        data={
            "name": "越界",
            "source_voice_id": voice.id,
            "source_file": "../secret.wav",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "clone_source_missing"


def test_resolve_within_keeps_paths_inside_the_root(tmp_path) -> None:
    from app.infra.archive import resolve_within

    root = tmp_path / "voice"
    root.mkdir()
    (root / "a.wav").write_bytes(b"x")
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"x")
    assert resolve_within(root, "a.wav") == (root / "a.wav").resolve()
    assert resolve_within(root, "../secret.wav") is None
    assert resolve_within(root, str(outside)) is None


def test_read_limited_boundaries() -> None:
    import asyncio

    import pytest

    from app.infra.uploads import UploadTooLarge, read_limited

    class Reader:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        async def read(self, size: int = -1) -> bytes:
            chunk, self.payload = self.payload[:size], self.payload[size:]
            return chunk

    assert asyncio.run(read_limited(Reader(b""), 8)) == b""
    assert asyncio.run(read_limited(Reader(b"12345678"), 8)) == b"12345678"
    with pytest.raises(UploadTooLarge):
        asyncio.run(read_limited(Reader(b"123456789"), 8))
    with pytest.raises(UploadTooLarge):
        asyncio.run(read_limited(Reader(b"x"), 0))


def test_resolve_within_refuses_a_symlink_escaping_the_root(tmp_path) -> None:
    import pytest

    from app.infra.archive import resolve_within

    root = tmp_path / "voice"
    root.mkdir()
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"x")
    try:
        (root / "link.wav").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前环境不能创建符号链接：{exc}")
    assert resolve_within(root, "link.wav") is None


def test_recording_upload_is_capped(client, monkeypatch) -> None:
    monkeypatch.setattr("app.api.sections.studio.recordings.MAX_BYTES", 1024)
    resp = client.post(
        "/api/v1/recordings",
        files={"file": ("录音.wav", b"0" * 2048, "audio/wav")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "audio_too_large"


def test_design_prompt_returns_one_clean_paragraph(client, monkeypatch) -> None:
    """AI 写描述只回一段话：去掉前缀与引号，折行接到一起（多句不截断）。"""

    async def fake_chat(body):
        assert body["messages"][1].role == "user"
        return {
            "text": "音色描述：「六十岁男声，沙哑低沉。」\n「语速偏慢，像在讲一件旧事。」",
            "provider_label": "测试通道",
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    resp = client.post(
        "/api/v1/voices/design-prompt", json={"name": "老者旁白", "brief": "沙哑"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "六十岁男声，沙哑低沉。语速偏慢，像在讲一件旧事。"
    assert body["provider_label"] == "测试通道"


def test_design_prompt_needs_a_name_or_a_draft(client) -> None:
    resp = client.post("/api/v1/voices/design-prompt", json={"name": " ", "brief": ""})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_value"
