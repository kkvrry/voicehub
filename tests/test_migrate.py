"""目录迁移：改"音色保存目录"之后把旧目录里的东西搬过去。

覆盖四件事：**只描述不动**（plan）、**同盘搬**（apply）、**重名不覆盖**、
**嵌套要拒**（不然越搬越深）。
"""

from pathlib import Path

import pytest

from app.infra.migrate import MoveError, apply_move, plan_move


def _voice(root: Path, name: str, payload: bytes = b"x") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "默认.wav").write_bytes(payload)
    (folder / "voice.json").write_text('{"tags": ["a"]}', encoding="utf-8")
    return folder


class TestPlan:
    def test_plan_only_describes(self, tmp_path: Path):
        source = tmp_path / "old"
        _voice(source, "胡桃")
        _voice(source, "钟离")
        plan = plan_move(source, tmp_path / "new")

        assert plan.entries == 2
        assert plan.audio == 2
        assert plan.bytes > 0
        # **一个字都没动**：目标没被建出来
        assert not (tmp_path / "new").exists()
        assert (source / "胡桃").is_dir()

    def test_missing_source_is_an_empty_plan(self, tmp_path: Path):
        plan = plan_move(tmp_path / "gone", tmp_path / "new")
        assert plan.empty
        assert plan.entries == 0

    def test_same_directory_needs_nothing(self, tmp_path: Path):
        source = tmp_path / "old"
        _voice(source, "胡桃")
        plan = plan_move(source, source)
        assert plan.empty
        assert "同一个" in plan.note

    def test_nested_target_is_refused(self, tmp_path: Path):
        """新目录在旧目录里面 → 必须拒：直接搬会把目标自己一起搬进去。"""
        source = tmp_path / "old"
        _voice(source, "胡桃")
        with pytest.raises(MoveError):
            plan_move(source, source / "inner")

    def test_existing_target_is_flagged(self, tmp_path: Path):
        source = tmp_path / "old"
        _voice(source, "胡桃")
        target = tmp_path / "new"
        _voice(target, "钟离")
        plan = plan_move(source, target)
        assert plan.note  # 有东西，重名会改名保留


class TestApply:
    def test_voices_move_over_intact(self, tmp_path: Path):
        source = tmp_path / "old"
        _voice(source, "胡桃")
        target = tmp_path / "new"
        result = apply_move(plan_move(source, target))

        assert result.ok
        assert result.moved == 1
        assert (target / "胡桃" / "默认.wav").read_bytes() == b"x"
        assert (target / "胡桃" / "voice.json").exists()
        # 源目录**留着**（空的），不删：没搬成的东西还要在这里找
        assert source.is_dir()
        assert list(source.iterdir()) == []

    def test_same_name_different_content_is_kept_twice(self, tmp_path: Path):
        """重名但内容不同 → 改名保留两份，**绝不覆盖**（那份可能是花钱买的）。"""
        source = tmp_path / "old"
        _voice(source, "胡桃", b"old")
        target = tmp_path / "new"
        _voice(target, "胡桃", b"new")
        result = apply_move(plan_move(source, target))

        assert result.renamed == [("胡桃", "胡桃-2")]
        assert (target / "胡桃" / "默认.wav").read_bytes() == b"new"
        assert (target / "胡桃-2" / "默认.wav").read_bytes() == b"old"

    def test_resume_drops_the_already_copied_twin(self, tmp_path: Path):
        """上次搬到一半留下的同一份 → 这次直接清掉源，不产生 ``-2`` 副本。

        可续的关键：中断之后重跑一次就能接着搬完，不会越搬越多。
        """
        source = tmp_path / "old"
        _voice(source, "胡桃", b"same")
        target = tmp_path / "new"
        _voice(target, "胡桃", b"same")
        result = apply_move(plan_move(source, target))

        assert result.renamed == []
        assert sorted(p.name for p in target.iterdir()) == ["胡桃"]

    def test_loose_files_move_too(self, tmp_path: Path):
        source = tmp_path / "old"
        source.mkdir()
        (source / "散件.wav").write_bytes(b"x")
        target = tmp_path / "new"
        apply_move(plan_move(source, target))
        assert (target / "散件.wav").exists()

    def test_unreadable_source_is_reported_not_raised(self, tmp_path: Path, monkeypatch):
        """逐条失败**不中断**：把清单交回去让人看，而不是整个接口报错。"""
        source = tmp_path / "old"
        _voice(source, "胡桃")
        target = tmp_path / "new"
        monkeypatch.setattr("shutil.copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("盘掉了")))
        monkeypatch.setattr(Path, "rename", lambda self, dest: (_ for _ in ()).throw(OSError("盘掉了")))
        result = apply_move(plan_move(source, target))

        assert not result.ok
        assert result.failed and "胡桃" in result.failed[0][0]
        assert (source / "胡桃" / "默认.wav").exists()  # 源还在，没丢
