"""File tools confined to one session workspace."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from daemon.fsutil import atomic_write

MAX_HITS = 200
MAX_READ_LINES = 4000


class ToolError(Exception):
    pass


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_dir",
            "description": "List files and directories under a workspace path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory. Defaults to the workspace root."},
                    "depth": {"type": "integer", "description": "How many levels to descend. Defaults to 1."},
                },
            },
        },
        {
            "name": "read_file",
            "description": "Read a UTF-8 text file. Line numbers are 1-based.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "search",
            "description": "Regex search across text files. Results are capped at 200 hits.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "write_file",
            "description": "Create or replace a file atomically. Parent directories are created.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
        {
            "name": "replace_in_file",
            "description": "Replace the single occurrence of old with new. If old is already gone and new is present, reports already_applied.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
        {
            "name": "mkdir",
            "description": "Create a directory. Existing directories are left as they are.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "delete_path",
            "description": "Delete a file or directory. A path that is already absent is success.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    ]


_FUNCS: dict[str, Callable[[Path, dict[str, Any]], str]] = {}


def run_tool(workspace: Path, name: str, arguments: dict[str, Any]) -> str:
    func = _FUNCS.get(name)
    if func is None:
        raise ToolError("unknown tool: %s" % name)
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    return func(workspace, arguments)


def confine(workspace: Path, raw: object) -> Path:
    if not isinstance(raw, str) or raw == "":
        raise ToolError("path must be a non-empty string")
    if raw.startswith("/") or raw.startswith("~"):
        raise ToolError("absolute paths are not allowed")
    root = workspace.resolve()
    candidate = (root / raw).resolve()
    if candidate != root and not _inside(candidate, root):
        raise ToolError("path escapes workspace")
    return candidate


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _list_dir(workspace: Path, arguments: dict[str, Any]) -> str:
    raw = arguments.get("path", ".")
    depth = arguments.get("depth", 1)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1 or depth > 8:
        raise ToolError("depth must be an integer from 1 to 8")
    root = confine(workspace, raw if isinstance(raw, str) else ".")
    if not root.is_dir():
        raise ToolError("not a directory")
    lines: list[str] = []

    def walk(directory: Path, levels: int, prefix: str) -> None:
        if levels < 1:
            return
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if child.name == ".git":
                continue
            rel = "%s%s" % (prefix, child.name)
            kind = "dir" if child.is_dir() and not child.is_symlink() else "file"
            lines.append("%s\t%s" % (kind, rel))
            if kind == "dir" and levels > 1:
                walk(child, levels - 1, rel + "/")

    walk(root, depth, "" if root == workspace.resolve() else "")
    return "\n".join(lines)


def _read_file(workspace: Path, arguments: dict[str, Any]) -> str:
    path = confine(workspace, arguments.get("path"))
    if not path.is_file():
        raise ToolError("not a file")
    text = _read_text(path)
    lines = text.splitlines()
    start = arguments.get("start_line", 1)
    end = arguments.get("end_line", len(lines))
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        raise ToolError("start_line must be >= 1")
    if not isinstance(end, int) or isinstance(end, bool) or end < start:
        raise ToolError("end_line must be >= start_line")
    selected = lines[start - 1 : end]
    truncated = False
    if len(selected) > MAX_READ_LINES:
        selected = selected[:MAX_READ_LINES]
        truncated = True
    body = "\n".join("%s|%s" % (start + offset, line) for offset, line in enumerate(selected))
    if truncated:
        body += "\n... truncated ..."
    return body


def _search(workspace: Path, arguments: dict[str, Any]) -> str:
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        raise ToolError("pattern is required")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ToolError("invalid regex: %s" % exc) from exc
    raw_path = arguments.get("path", ".")
    root = confine(workspace, raw_path if isinstance(raw_path, str) else ".")
    glob = arguments.get("glob")
    if glob is not None and not isinstance(glob, str):
        raise ToolError("glob must be a string")
    hits: list[str] = []
    files = [root] if root.is_file() else _files(root)
    workspace_root = workspace.resolve()
    for file_path in files:
        relative = file_path.resolve().relative_to(workspace_root).as_posix()
        if glob and not (fnmatch.fnmatch(file_path.name, glob) or fnmatch.fnmatch(relative, glob)):
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                hits.append("%s:%s:%s" % (relative, number, line))
                if len(hits) >= MAX_HITS:
                    return "\n".join(hits)
    return "\n".join(hits)


def _write_file(workspace: Path, arguments: dict[str, Any]) -> str:
    path = confine(workspace, arguments.get("path"))
    content = arguments.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if path.exists() and path.is_dir():
        raise ToolError("path is a directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content)
    return "wrote %s (%s bytes)" % (path.name, len(content.encode("utf-8")))


def _replace_in_file(workspace: Path, arguments: dict[str, Any]) -> str:
    path = confine(workspace, arguments.get("path"))
    old = arguments.get("old")
    new = arguments.get("new")
    if not isinstance(old, str) or old == "":
        raise ToolError("old must be a non-empty string")
    if not isinstance(new, str):
        raise ToolError("new must be a string")
    if not path.is_file():
        raise ToolError("not a file")
    text = _read_text(path)
    count = text.count(old)
    if count == 1:
        atomic_write(path, text.replace(old, new, 1))
        return "replaced 1 occurrence in %s" % path.name
    if count == 0 and new in text:
        return "already_applied in %s" % path.name
    if count == 0:
        raise ToolError("old text was not found")
    raise ToolError("old text appears %s times; expected 1" % count)


def _mkdir(workspace: Path, arguments: dict[str, Any]) -> str:
    path = confine(workspace, arguments.get("path"))
    if path.is_file():
        raise ToolError("path is a file")
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    return "exists %s" % path.name if existed else "created %s" % path.name


def _delete_path(workspace: Path, arguments: dict[str, Any]) -> str:
    path = confine(workspace, arguments.get("path"))
    if path == workspace.resolve():
        raise ToolError("cannot delete the workspace root")
    if not path.exists() and not path.is_symlink():
        return "already absent"
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return "deleted directory %s" % path.name
    path.unlink()
    return "deleted %s" % path.name


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            found.append(Path(dirpath) / name)
    return found


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ToolError("file is not utf-8") from exc


_FUNCS.update(
    {
        "list_dir": _list_dir,
        "read_file": _read_file,
        "search": _search,
        "write_file": _write_file,
        "replace_in_file": _replace_in_file,
        "mkdir": _mkdir,
        "delete_path": _delete_path,
    }
)
