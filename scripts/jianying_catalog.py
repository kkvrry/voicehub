"""Extract the JianYing 文本朗读 tone catalogue from the cached panel response.

Usage: python scripts/jianying_catalog.py [panel_cache_id]
       不传 id 时按面板 URL 找（行号会变，认 URL 更稳）。

Output: F:/ai/语音模型/voice-models/剪映/_catalog/jianying_tones.json (+ .csv)
"""

import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# 分类名里有 🔥 这类 emoji，GBK 控制台会直接崩，先把输出换成 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path("D:/JianyingPro/User Data/Cache/ressdk_db/12690222373077278714/rp.db")
OUT_DIR = Path("F:/ai/语音模型/voice-models/剪映/_catalog")
PANEL_URL = "24A486D98227817A32049E4D52BEFAEF"  # 「文本朗读」面板的固定 hash
#: http_cache 的行号会变（剪映重写缓存就重新编号），所以认 url 不认 id；
#: 命令行给数字时按 id 取，方便临时看别的面板。
PANEL_ID = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None

# tag name -> which metadata slot it fills
GENDER = {"女": "female", "男": "male"}
AGE = {
    "儿童": "child",
    "少年": "teen",
    "青年": "young",
    "中年": "middle",
    "老年": "old",
    "少女": "teen",
    "御姐": "female",
    "大叔": "middle",
    "宝宝": "child",
    "童声": "child",
}
LANG_HINT = {
    "中文": "zh",
    "英文": "en",
    "日语": "ja",
    "韩语": "ko",
    "粤语": "yue",
    "四川话": "sichuan",
    "东北话": "dongbei",
    "上海话": "shanghai",
    "潮汕话": "teochew",
    "湖南话": "hunan",
    "河南话": "henan",
    "台湾话": "taiwan",
    "闽南语": "minnan",
    "西班牙语": "es",
    "法语": "fr",
    "德语": "de",
    "俄语": "ru",
    "泰语": "th",
    "越南语": "vi",
    "印尼语": "id",
    "阿拉伯语": "ar",
    "意大利语": "it",
    "葡萄牙语": "pt",
    "土耳其语": "tr",
    "印度语": "hi",
}


def tone_fields(extra: str, biz: str):
    """Pull voice_type / rate out of the double-encoded tonetype blob."""
    out = {"voice_type": "", "rate": "", "version": "", "tts_voice": ""}
    pending = set(out)
    for blob in (extra, biz):
        if not isinstance(blob, str) or not pending:
            continue
        for key in sorted(pending):
            pattern = rf'\\?"{key}\\?"\s*:\s*\\?"([^"\\]{{1,120}})'
            m = re.search(pattern, blob)
            if m:
                out[key] = m.group(1)
                pending.discard(key)
    return out


conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
if PANEL_ID is None:
    row = conn.execute(
        "select id, response_body from http_cache where url like ? order by id desc limit 1",
        (f"%get_panel_info_{PANEL_URL}%",),
    ).fetchone()
    if row is None:
        raise SystemExit(
            f"缓存里找不到「文本朗读」面板（{PANEL_URL}）。"
            "打开剪映 → 文本朗读，等它把面板拉下来再跑一次。"
        )
    PANEL_ID, body = row
else:
    row = conn.execute("select response_body from http_cache where id=?", (PANEL_ID,)).fetchone()
    if row is None:
        raise SystemExit(f"http_cache 里没有 id={PANEL_ID} 这一行")
    body = row[0]
if isinstance(body, (bytes, bytearray)):
    body = body.decode("utf-8", "replace")
data = json.loads(body)["data"]
cats = {str(c["category_id"]): c["category_name"] for c in data.get("categories", [])}
res = data["category_resources"]

tones: dict[str, dict] = {}
tag_freq: Counter = Counter()
order: list[str] = []

for cat_id, block in res.items():
    cat_name = cats.get(cat_id, cat_id)
    for it in block.get("effect_item_list") or []:
        ca = it.get("common_attr") or {}
        eid = ca.get("effect_id")
        if not eid:
            continue
        tf = tone_fields(ca.get("extra"), ca.get("biz_extra"))
        tags = [t.get("name", "") for t in (ca.get("tag_list") or []) if t.get("name")]
        tag_freq.update(tags)
        biz_str = (ca.get("business_info") or {}).get("json_str") or "{}"
        try:
            is_vip = json.loads(biz_str).get("is_vip")
        except json.JSONDecodeError:
            is_vip = None
        gender = next((GENDER[t] for t in tags if t in GENDER), "")
        age = next((AGE[t] for t in tags if t in AGE), "")
        lang = next((LANG_HINT[t] for t in tags if t in LANG_HINT), "")
        rec = tones.get(eid)
        if rec is None:
            rec = tones[eid] = {
                "effect_id": eid,
                "title": ca.get("title", "").strip(),
                "voice_type": tf["voice_type"],
                "tone_version": tf["version"],
                "tts_voice": tf["tts_voice"],
                "sample_rate": tf["rate"] or str((it.get("timbre") or {}).get("voice_rate") or ""),
                "categories": [],
                "tags": tags,
                "gender": gender,
                "age": age,
                "language": lang,
                "author": (it.get("author") or {}).get("name", ""),
                "is_vip": is_vip,
                "publish_source": ca.get("publish_source", ""),
                "third_resource_id": ca.get("third_resource_id_str") or "",
                "create_time": ca.get("create_time"),
                "cover_url": (ca.get("cover_url") or {}).get("small") or "",
            }
            order.append(eid)
        if cat_name not in rec["categories"]:
            rec["categories"].append(cat_name)
        if not rec["voice_type"] and tf["voice_type"]:
            rec["voice_type"] = tf["voice_type"]
        if not rec["tags"] and tags:
            rec["tags"] = tags

print(
    f"panel #{PANEL_ID}: categories={len(cats)} raw items={sum(len(b.get('effect_item_list') or []) for b in res.values())}"
)
print(f"unique tones = {len(tones)}")
print(f"  with voice_type  : {sum(1 for r in tones.values() if r['voice_type'])}")
print(f"  with gender      : {sum(1 for r in tones.values() if r['gender'])}")
print(f"  with age         : {sum(1 for r in tones.values() if r['age'])}")
print(f"  with language    : {sum(1 for r in tones.values() if r['language'])}")
print(f"  vip              : {sum(1 for r in tones.values() if r['is_vip'])}")
print(f"  distinct speaker : {len({r['voice_type'] for r in tones.values() if r['voice_type']})}")

print("\n== categories (in panel order) ==")
for cid, name in cats.items():
    n = len((res.get(cid) or {}).get("effect_item_list") or [])
    print(f"  {cid:>9}  {name:<10} items={n}")

print("\n== tag vocabulary ==")
for t, c in tag_freq.most_common(60):
    print(f"  {c:5d}  {t}")

OUT_DIR.mkdir(parents=True, exist_ok=True)
records = [tones[e] for e in order]
records.sort(key=lambda r: (r["categories"][0] if r["categories"] else "~", r["title"]))
(OUT_DIR / "jianying_tones.json").write_text(
    json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8"
)
with (OUT_DIR / "jianying_tones.csv").open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=list(records[0].keys()))
    w.writeheader()
    for r in records:
        row = dict(r)
        row["categories"] = "|".join(r["categories"])
        row["tags"] = "|".join(r["tags"])
        w.writerow(row)
print(f"\nwrote {OUT_DIR / 'jianying_tones.json'} / .csv")
print("== sample ==")
for r in records[:6]:
    print(" ", r["title"], r["voice_type"], r["categories"], r["gender"], r["age"], r["language"])
