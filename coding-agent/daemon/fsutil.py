"""Atomic writes and hardlink copies. These are what make session snapshots copy-on-write."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: str | bytes) -> None:
    """Write ``path`` by replacing it. An existing inode is left unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = data.encode("utf-8") if isinstance(data, str) else data
    tmp = path.with_name("%s.tmp-%s" % (path.name, uuid4().hex))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, blob)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(path.parent)


def fsync_tree(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        for name in filenames:
            fd = os.open(directory / name, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(directory)


def link_copy_tree(src: Path, dst: Path) -> None:
    """Copy a tree. Files are hardlinked so later atomic writes detach only that path."""
    dst.mkdir(parents=True, exist_ok=False)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            link_copy_tree(entry, target)
        else:
            try:
                os.link(entry, target)
            except OSError:
                shutil.copy2(entry, target)
