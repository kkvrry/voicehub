"""音色描述：提示词要带着平台方给的维度与注意事项，输出收敛成一段话。"""

from app.usecase import voice_design


def test_the_prompt_keeps_the_documented_dimensions() -> None:
    """四个关键维度、可选丰富项与注意事项都是原话：删掉一条，模型就少一个判据。"""
    for word in (
        "性别与年龄",
        "音色/质感",
        "情绪/语气",
        "语速/节奏",
        "角色/人设",
        "说话风格",
        "场景描写",
        "年代参照",
        "1-4 句",
        "避免冲突",
        "避免音质效果词",
        "避免模糊词",
        "中英文均可",
    ):
        assert word in voice_design.TEMPLATE


def test_clean_joins_lines_and_drops_the_wrapping() -> None:
    """整段被引号包起来、还带「音色描述：」前缀时，剥干净并接成一段。"""
    text = "音色描述：“六十岁男声，沙哑低沉。”\n\n“语速偏慢，像在讲一件旧事。”"
    assert voice_design.clean(text) == "六十岁男声，沙哑低沉。语速偏慢，像在讲一件旧事。"


def test_clean_keeps_an_english_paragraph_apart() -> None:
    """英文折行要留空格，不能粘成一个词。"""
    text = "Deep and gravelly.\nSlow and deliberate."
    assert voice_design.clean(text) == "Deep and gravelly. Slow and deliberate."
