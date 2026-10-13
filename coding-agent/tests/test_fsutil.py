"""Step 3: atomic replace leaves the previous inode untouched."""

from __future__ import annotations

import os
from pathlib import Path

from daemon.fsutil import atomic_write, link_copy_tree


def test_atomic_write_replaces_bytes_and_cleans_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    atomic_write(target, "one")
    atomic_write(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_hardlink_copy_shares_inodes_until_an_atomic_write(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_text("base", encoding="utf-8")
    dst = tmp_path / "dst"
    link_copy_tree(src, dst)
    assert os.stat(src / "keep.txt").st_ino == os.stat(dst / "keep.txt").st_ino
    atomic_write(dst / "keep.txt", "changed")
    assert (src / "keep.txt").read_text(encoding="utf-8") == "base"
    assert (dst / "keep.txt").read_text(encoding="utf-8") == "changed"
    assert os.stat(src / "keep.txt").st_ino != os.stat(dst / "keep.txt").st_ino
