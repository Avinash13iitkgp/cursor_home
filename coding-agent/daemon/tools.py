"""Workspace tools confined to one session workspace."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from daemon.fsutil import atomic_write

MAX_HITS = 200
MAX_READ_LINES = 4000
MAX_TEST_OUTPUT = 16000
MAX_SHELL_OUTPUT = 16000
MAX_SHELL_COMMAND = 100000
TEST_TIMEOUT_S = 45
SHELL_TIMEOUT_DEFAULT_S = 30
SHELL_TIMEOUT_MAX_S = 45
_SHELL_DRAIN_S = 5
_TEST_PATTERN = re.compile(r"[A-Za-z0-9_.*?-]+")


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
        {
            "name": "run_tests",
            "description": (
                "Run unittest discover. "
                "target workspace (the default) uses this session's workspace: the base snapshot plus this session's edits. "
                "target baseline uses the unmodified base snapshot, so you can see which failures already exist there. "
                "A failing suite still returns the test output."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["workspace", "baseline"],
                        "description": "workspace runs the session tree. baseline runs the unmodified base snapshot. Defaults to workspace.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Test directory relative to the selected tree. Defaults to tests.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "File glob passed to unittest discover. Defaults to test*.py.",
                    },
                },
            },
        },
        {
            "name": "shell",
            "description": (
                "Run any command via /bin/sh -c in the session workspace inside this container. "
                "cwd defaults to the workspace root. "
                "A non-zero exit is a normal result, not an error. "
                "Output is clipped. "
                "The same op_id is replayed by the journal and does not run again."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command string passed to /bin/sh -c.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Relative working directory inside the workspace. Defaults to .",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Timeout in seconds from 1 to 45. Defaults to 30.",
                    },
                },
                "required": ["command"],
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


def _run_tests(workspace: Path, arguments: dict[str, Any]) -> str:
    root = workspace.resolve()
    target = arguments.get("target", "workspace")
    if not isinstance(target, str) or target not in ("workspace", "baseline"):
        raise ToolError("target must be workspace or baseline")
    if target == "baseline":
        source = _baseline_dir(root)
        with tempfile.TemporaryDirectory(prefix="baseline-tests-") as tmp:
            dest = Path(tmp) / "tree"
            shutil.copytree(source, dest, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            return _execute_suite(dest, arguments, "baseline")
    if _is_baseline(root):
        raise ToolError("pass target baseline to run the base snapshot")
    return _execute_suite(root, arguments, "session workspace")


def _execute_suite(root: Path, arguments: dict[str, Any], label: str) -> str:
    root = root.resolve()
    raw_path = arguments.get("path", "tests")
    if not isinstance(raw_path, str):
        raise ToolError("path must be a string")
    start = confine(root, raw_path)
    if not start.is_dir():
        raise ToolError("test directory not found")
    pattern = arguments.get("pattern", "test*.py")
    if not isinstance(pattern, str) or not _TEST_PATTERN.fullmatch(pattern):
        raise ToolError("pattern must be a file glob")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONPATH", None)
    # The start directory is the top-level directory, so tests/ does not need
    # __init__.py. cwd is the selected tree, so imports resolve inside it.
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        start.relative_to(root).as_posix(),
        "-p",
        pattern,
        "-v",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = "%s%s" % (exc.stdout or "", exc.stderr or "")
        raise ToolError("tests timed out after %ss\n%s" % (TEST_TIMEOUT_S, _clip(partial))) from exc
    output = _clip("%s%s" % (completed.stdout or "", completed.stderr or ""))
    no_tests = "Ran 0 tests" in output or "NO TESTS RAN" in output
    if completed.returncode == 0 and not no_tests:
        status, code = "passed", 0
    else:
        status, code = "failed", completed.returncode or 5
    relative = start.relative_to(root).as_posix()
    return "%s: %s exit=%s path=%s\n%s" % (label, status, code, relative, output)


def _baseline_dir(workspace: Path) -> Path:
    if workspace.name == "workspace" and len(workspace.parents) >= 3 and workspace.parents[1].name == "sessions":
        candidate = workspace.parents[2] / "base" / "dummy-repo"
        if candidate.is_dir():
            return candidate
    raise ToolError("baseline snapshot is not available")


def _is_baseline(path: Path) -> bool:
    if path.name == "dummy-repo" and path.parent.name == "base":
        return True
    dummy = os.environ.get("DUMMY_REPO")
    if not dummy:
        return False
    try:
        return path == Path(dummy).resolve()
    except OSError:
        return False


def _clip(text: str, limit: int = MAX_TEST_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return "... %s characters omitted ...\n%s" % (omitted, text[-limit:])


def _shell(workspace: Path, arguments: dict[str, Any]) -> str:
    command = arguments.get("command")
    if not isinstance(command, str) or command == "":
        raise ToolError("command must be a non-empty string")
    if len(command) > MAX_SHELL_COMMAND:
        raise ToolError("command must be at most %s characters" % MAX_SHELL_COMMAND)
    raw_cwd = arguments.get("cwd", ".")
    if not isinstance(raw_cwd, str):
        raise ToolError("cwd must be a string")
    start = confine(workspace, raw_cwd)
    if not start.is_dir():
        raise ToolError("not a directory")
    timeout_s = arguments.get("timeout_s", SHELL_TIMEOUT_DEFAULT_S)
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, int)
        or timeout_s < 1
        or timeout_s > SHELL_TIMEOUT_MAX_S
    ):
        raise ToolError("timeout_s must be an integer from 1 to %s" % SHELL_TIMEOUT_MAX_S)
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=os.fspath(start),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise ToolError("could not start command: %s" % exc) from exc
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            stdout, stderr = _finish_timed_out(proc, exc)
            partial = _clip(_shell_sections(stdout, stderr), MAX_SHELL_OUTPUT)
            message = "shell timed out after %ss" % timeout_s
            if partial:
                message = "%s\n%s" % (message, partial)
            raise ToolError(message) from exc
        code = proc.returncode
        if not isinstance(code, int):
            raise ToolError("command did not exit")
        return _shell_result(code, stdout or "", stderr or "")
    finally:
        _reap_shell(proc)


def _shell_result(code: int, stdout: str, stderr: str) -> str:
    body = _clip(_shell_sections(stdout, stderr), MAX_SHELL_OUTPUT)
    if not body:
        return "exit=%s" % code
    return "exit=%s\n%s" % (code, body)


def _shell_sections(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append("stdout:\n%s" % stdout)
    if stderr:
        parts.append("stderr:\n%s" % stderr)
    body = ""
    for part in parts:
        if body and not body.endswith("\n"):
            body += "\n"
        body += part
    return body


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill the command's session. start_new_session makes its pid the process group id."""
    pid = proc.pid
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        proc.kill()
        return
    except OSError:
        proc.kill()
        return
    # Only signal a group this child leads. A pid that is still in the daemon's
    # group must not be passed to killpg.
    if pgid != pid:
        proc.kill()
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    except OSError:
        proc.kill()


def _finish_timed_out(proc: subprocess.Popen[str], exc: subprocess.TimeoutExpired) -> tuple[str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=_SHELL_DRAIN_S)
    except subprocess.TimeoutExpired as drained:
        stdout, stderr = _timeout_text(proc, drained)
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        return stdout, stderr
    if stdout or stderr:
        return stdout or "", stderr or ""
    # communicate() can return empty strings after a timeout even when the first
    # read already captured bytes. Fall back to that partial output.
    return _timeout_text(proc, exc)


def _timeout_text(proc: subprocess.Popen[str], exc: subprocess.TimeoutExpired) -> tuple[str, str]:
    encoding = "utf-8"
    errors = "replace"
    stream = proc.stdout or proc.stderr
    if stream is not None:
        encoding = stream.encoding or encoding
        errors = stream.errors or errors
    return _as_text(exc.stdout, encoding, errors), _as_text(exc.stderr, encoding, errors)


def _as_text(value: object, encoding: str, errors: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(encoding, errors)
    if isinstance(value, str):
        return value
    return str(value)


def _reap_shell(proc: subprocess.Popen[str]) -> None:
    try:
        if proc.poll() is None:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=_SHELL_DRAIN_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass


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
        "run_tests": _run_tests,
        "shell": _shell,
    }
)
