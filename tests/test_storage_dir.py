"""存储目录的输入校验：绝对路径，且不能是盘根 / 系统目录 / 用户主目录。"""

from __future__ import annotations

import os
from pathlib import Path


def test_a_drive_root_is_refused(client) -> None:
    root = "C:\\" if os.name == "nt" else "/"
    resp = client.put("/api/v1/storage", json={"cache_dir": root})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsafe_path"


def test_a_system_directory_is_refused(client) -> None:
    target = "C:\\Windows" if os.name == "nt" else "/etc"
    resp = client.put("/api/v1/storage", json={"log_dir": target})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsafe_path"


def test_the_user_home_root_is_refused(client) -> None:
    resp = client.put("/api/v1/storage", json={"cache_dir": str(Path.home())})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsafe_path"


def test_an_ordinary_folder_is_accepted(client, tmp_path) -> None:
    target = tmp_path / "缓存"
    resp = client.put("/api/v1/storage", json={"cache_dir": str(target)})
    assert resp.status_code == 200
    assert target.is_dir()


def test_the_output_dir_is_checked_too(client) -> None:
    root = "C:\\" if os.name == "nt" else "/"
    resp = client.put("/api/v1/output", json={"dir": root})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsafe_path"
