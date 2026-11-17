"""domain 扫描规则（纯函数，零 IO）。"""

from pathlib import Path

from app.domain.scanner import (
    audio_files_of,
    emotion_of,
    is_voice_folder,
    main_sample_of,
    relative_category,
)


class TestVoiceFolder:
    def test_a_folder_with_audio_directly_inside_is_a_voice(self):
        assert is_voice_folder(["开心.wav", "voice.json"]) is True

    def test_a_folder_without_audio_is_not_a_voice(self):
        assert is_voice_folder(["voice.json", "cover.png", "notes.txt"]) is False

    def test_audio_detection_is_case_insensitive(self):
        assert is_voice_folder(["A.WAV"]) is True

    def test_audio_files_come_back_sorted(self):
        assert audio_files_of(["b.wav", "a.mp3", "c.txt"]) == ("a.mp3", "b.wav")


class TestCategory:
    def test_category_is_the_path_relative_to_root(self):
        root = Path("D:/库")
        assert relative_category(root / "三国杀" / "魏国" / "曹操", root) == "三国杀/魏国"

    def test_directly_under_root_means_uncategorized(self):
        assert relative_category(Path("D:/库") / "胡桃", Path("D:/库")) == ""


class TestEmotion:
    def test_plain_emotion_word_is_recognized(self):
        assert emotion_of("开心.wav") == "开心"

    def test_trailing_index_is_dropped(self):
        assert emotion_of("开心_01.wav") == "开心"

    def test_voice_name_prefix_is_dropped(self):
        assert emotion_of("胡桃_开心.wav", voice_name="胡桃") == "开心"

    def test_a_title_like_filename_is_not_an_emotion(self):
        """正文式文件名（audiobook 分段、生成结果）不该被整段当成情绪。

        从前"去壳剩下的就是情绪"，``01_以义传魂，以武入圣！`` 这种念的是标题，
        整段被塞进情绪栏——情绪是有边界的词域，词表认不出就空着。
        """
        assert emotion_of("01_以义传魂，以武入圣！.wav") == ""
        assert emotion_of("20260101-120000-01.mp3") == ""
        assert emotion_of("旁白.wav") == ""

    def test_emotion_word_in_a_segment_is_found(self):
        assert emotion_of("胡桃-开心_02.wav", voice_name="胡桃") == "开心"

    def test_unknown_shape_gives_empty_rather_than_a_guess(self):
        assert emotion_of(".wav") == ""

    def test_a_default_mark_is_a_main_sample_mark_not_an_emotion(self):
        """「默认」是**主样例的磁盘标记**（见 `main_sample_of`），不是情绪。

        主样例靠 sidecar 里的指定 → 文件名标记 → 字母序三路登记，**不靠给样例打
        标签**；所以这里的正确结果是"没认出情绪"，而不是一个叫「默认」的情绪。
        """
        assert emotion_of("默认.wav") == ""
        assert emotion_of("default.wav") == ""
        assert emotion_of("主.wav") == ""
        # 去前缀之后才判：`胡桃_默认.wav` 在胡桃的目录里，剩下来那半段就是标记。
        assert emotion_of("胡桃_默认.wav", voice_name="胡桃") == ""


class TestMainSample:
    def test_configured_file_wins(self):
        assert main_sample_of(["a.wav", "b.wav"], "b.wav") == "b.wav"

    def test_default_mark_beats_alphabetical(self):
        assert main_sample_of(["a.wav", "默认.wav"]) == "默认.wav"

    def test_alphabetical_first_is_the_fallback(self):
        assert main_sample_of(["b.wav", "a.wav"]) == "a.wav"

    def test_a_deleted_configured_file_falls_back(self):
        assert main_sample_of(["a.wav"], "gone.wav") == "a.wav"

    def test_no_audio_means_no_main_sample(self):
        assert main_sample_of(["readme.txt"]) == ""
