"""Backend-neutral, read-only host tools for agent harnesses.

The model gets only cwd-confined Read/Glob/Grep plus an optional in-memory
task checklist. Tool handlers keep the MCP-shaped content contract used by
``ToolSpec``; individual backends translate those blocks to their SDK's
native tool-result representation.

HARNESS=claude does not use this module: Claude Code ships its own
Read/Glob/Grep and task list under the same names, and its permission
system confines them. The OpenAI and DeepSeek adapters have no such
builtins (dsh's sdk-minimal editor is write-capable even under a read-only
sandbox policy), so the exact requested surface is served from here.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import glob as globlib
import os
import re
import time
from pathlib import Path
from typing import Iterator

from .harness import BUILTIN_CAPABILITIES, ToolSpec

READ_DEFAULT_BYTES = 8 * 1024
READ_MAX_BYTES = 32 * 1024
IMAGE_MAX_BYTES = 20 * 1024 * 1024
SEARCH_MAX_BYTES = 256 * 1024
SEARCH_MAX_RESULTS = 500
GREP_TIMEOUT_SECONDS = 30.0
GREP_LINE_MAX_CHARS = 2000
BINARY_PROBE_BYTES = 8192


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def _err(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}], "is_error": True}


# --------------------------------------------------------------------------
# Path confinement
# --------------------------------------------------------------------------


def _within(root: Path, path: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(path))) == str(root)
    except ValueError:
        return False


def _resolve(root: Path, raw: object) -> Path:
    """Resolve a model-supplied path (symlinks included) and confine it to root."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("path must be non-empty")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    if not _within(root, resolved):
        raise ValueError(f"path is outside the working directory {root}: {value}")
    return resolved


def _display(root: Path, path: Path) -> str:
    return "." if path == root else path.relative_to(root).as_posix()


def _image_type(prefix: bytes) -> str | None:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


# --------------------------------------------------------------------------
# Read / Glob / Grep
# --------------------------------------------------------------------------


def _read(root: Path, args: dict) -> dict:
    try:
        path = _resolve(root, args.get("file_path"))
        if not path.is_file():
            return _err(f"not a regular file: {_display(root, path)}")
        with path.open("rb") as fh:
            before = os.fstat(fh.fileno())
            size = before.st_size
            media_type = _image_type(fh.read(16))
            if media_type is not None:
                if size > IMAGE_MAX_BYTES:
                    return _err(f"image is {size} bytes; maximum is {IMAGE_MAX_BYTES}: {_display(root, path)}")
                fh.seek(0)
                data = fh.read()
                return {"content": [
                    {"type": "text", "text": f"Image file: {_display(root, path)} ({media_type}, {size} bytes)"},
                    {"type": "image", "data": base64.b64encode(data).decode("ascii"),
                     "mimeType": media_type},
                ]}
            offset = args.get("byte_offset", 0)
            maximum = args.get("max_bytes", 0)
            if type(offset) is not int or offset < 0:
                return _err("byte_offset must be a nonnegative integer (0 starts at the beginning)")
            if type(maximum) is not int or maximum < 0:
                return _err("max_bytes must be a nonnegative integer (0 uses the default)")
            maximum = min(maximum or READ_DEFAULT_BYTES, READ_MAX_BYTES)
            if offset > size:
                return _err(f"byte_offset {offset} is past end of file ({size} bytes)")
            fh.seek(offset)
            data = fh.read(maximum)
            after = os.fstat(fh.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return _err("file changed during Read; retry from the beginning")
        if offset < size and not data:
            return _err("file ended unexpectedly during Read; retry from the beginning")
        if offset and data and data[0] & 0xC0 == 0x80:
            return _err("byte_offset is inside a UTF-8 character; use the previous result's next byte_offset")
        if b"\x00" in data:
            return _err(f"binary file is not a supported raster image: {_display(root, path)}")
        # Leave an incomplete UTF-8 character for the next page, rather than
        # corrupting it across byte boundaries. EOF still reports malformed text.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        final = offset + len(data) >= size
        body = decoder.decode(data, final=final)
        consumed = len(data) - len(decoder.getstate()[0])
        if data and consumed == 0:
            return _err("max_bytes is too small for the next UTF-8 character; use at least 4")
        next_offset = offset + consumed
        suffix = (
            f"\n\n[page truncated; next byte_offset={next_offset}, max_bytes={maximum}. "
            "For large cell-level tables, use a targeted query or Grep instead of reading every page.]"
            if next_offset < size else ""
        )
        return _text(f"<path>{_display(root, path)}</path>\n"
                     f"<range>bytes {offset}:{next_offset} of {size}</range>\n<content>\n{body}{suffix}\n</content>")
    except (OSError, ValueError) as exc:
        return _err(str(exc))


def _glob(root: Path, args: dict) -> dict:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return _err("pattern must be non-empty")
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        return _err("glob pattern must be relative to the working directory and cannot contain '..'")
    matches: set[str] = set()
    try:
        for raw in globlib.iglob(str(root / pattern), recursive=True):
            path = Path(raw).resolve(strict=True)
            if not _within(root, path):  # a symlink pointing out of the tree
                continue
            matches.add(_display(root, path) + ("/" if path.is_dir() else ""))
            if len(matches) >= SEARCH_MAX_RESULTS:
                break
    except (OSError, ValueError) as exc:
        return _err(str(exc))
    if not matches:
        return _text("no matches")
    suffix = f"\n[limited to {SEARCH_MAX_RESULTS} results]" if len(matches) >= SEARCH_MAX_RESULTS else ""
    return _text("\n".join(sorted(matches)) + suffix)


def _walk_files(target: Path) -> Iterator[Path]:
    """Regular files under target in a stable order, skipping dot-entries the
    way ripgrep does. Symlinked directories are not followed."""
    if target.is_file():
        yield target
        return
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in sorted(filenames):
            if not name.startswith("."):
                yield Path(dirpath, name)


def _grep_sync(root: Path, regex: re.Pattern[str], target: Path) -> dict:
    """Line-oriented regex search with ripgrep-style ``path:line:text`` output.

    Implemented in Python rather than by shelling out to ``rg``: the cluster
    images this runs on ship no ripgrep, and a Grep tool that always answers
    "unavailable" silently degrades every run. Binary files are skipped and
    the search stops at whichever bound is exhausted first (matches, output
    bytes, or wall time) with an explicit note for the model.
    """
    deadline = time.monotonic() + GREP_TIMEOUT_SECONDS
    lines: list[str] = []
    output_bytes = 0
    note: str | None = None
    for path in _walk_files(target):
        if time.monotonic() > deadline:
            note = f"[stopped after {GREP_TIMEOUT_SECONDS:g} s; narrow the path or pattern]"
            break
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file() or not _within(root, resolved):
                continue
            with path.open("rb") as fh:
                if b"\x00" in fh.read(BINARY_PROBE_BYTES):
                    continue
                fh.seek(0)
                shown = _display(root, path)
                for lineno, raw in enumerate(fh, 1):
                    if lineno % 10000 == 0 and time.monotonic() > deadline:
                        note = f"[stopped after {GREP_TIMEOUT_SECONDS:g} s; narrow the path or pattern]"
                        break
                    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not regex.search(text):
                        continue
                    if len(text) > GREP_LINE_MAX_CHARS:
                        text = text[:GREP_LINE_MAX_CHARS] + " [line truncated]"
                    line = f"{shown}:{lineno}:{text}"
                    lines.append(line)
                    output_bytes += len(line) + 1
                    if len(lines) >= SEARCH_MAX_RESULTS:
                        note = f"[limited to {SEARCH_MAX_RESULTS} matches]"
                        break
                    if output_bytes >= SEARCH_MAX_BYTES:
                        note = f"[truncated after {SEARCH_MAX_BYTES} bytes]"
                        break
        except OSError:
            continue  # vanished or unreadable file: skip it, like rg does
        if note:
            break
    if not lines:
        return _text("no matches")
    return _text("\n".join(lines) + (f"\n{note}" if note else ""))


async def _grep(root: Path, args: dict) -> dict:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return _err("pattern must be non-empty")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return _err(f"invalid regular expression: {exc}")
    try:
        target = _resolve(root, args.get("path") or ".")
    except (OSError, ValueError) as exc:
        return _err(str(exc))
    return await asyncio.to_thread(_grep_sync, root, regex, target)


def readonly_tools(cwd: str, allowed_builtin: tuple[str, ...]) -> list[ToolSpec]:
    """Build the exact cwd-confined exploration surface requested by a call."""
    unknown = sorted(set(allowed_builtin) - BUILTIN_CAPABILITIES)
    if unknown:
        raise ValueError(f"unsupported allowed_builtin capabilities: {unknown}")
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"agent cwd is not a directory: {root}")

    async def read(args):
        return _read(root, args)

    async def glob(args):
        return _glob(root, args)

    async def grep(args):
        return await _grep(root, args)

    tools: list[ToolSpec] = []
    if "read" in allowed_builtin:
        tools.append(ToolSpec(
            "Read",
            f"Read a UTF-8 text file or inspect a PNG/JPEG/WebP/GIF image. Relative paths resolve from "
            f"the working directory {root}; paths outside it are rejected. Text is paginated: "
            f"byte_offset=0 starts at the beginning; max_bytes=0 uses {READ_DEFAULT_BYTES} bytes "
            f"(hard maximum {READ_MAX_BYTES}). Continue only if needed using the next byte_offset "
            "shown in the result. Image reads ignore these two arguments. Prefer a targeted query "
            "or Grep for large per-cell tables, rather than loading all their barcodes.",
            {"file_path": str, "byte_offset": int, "max_bytes": int}, read,
        ))
    if "glob" in allowed_builtin:
        tools.append(ToolSpec(
            "Glob", f"List files matching a recursive glob relative to the working directory {root}.",
            {"pattern": str}, glob,
        ))
    if "grep" in allowed_builtin:
        tools.append(ToolSpec(
            "Grep",
            f"Search text files inside the working directory {root} with a regular expression; each match "
            "is reported as path:line:text. path may be a relative file or directory (use '.' for the "
            "whole working directory).",
            {"pattern": str, "path": str}, grep,
        ))
    return tools


# --------------------------------------------------------------------------
# Session task list
# --------------------------------------------------------------------------


def task_tools() -> list[ToolSpec]:
    """Host-side stand-in for Claude Code's session task list: same tool names
    and the same create/update/list/get shape, kept in memory for one
    ``run_agent`` call. Purely the model's own progress checklist — coverage
    is enforced by each call site's submit validation, never by this list."""
    tasks: dict[str, dict] = {}
    statuses = ("pending", "in_progress", "completed")

    def _render(task):
        return (f"#{task['id']} [{task['status']}] {task['subject']}"
                + (f" — {task['description']}" if task["description"] else ""))

    def _lookup(args) -> tuple[str, dict | None]:
        task_id = str(args.get("taskId") or "").strip().lstrip("#")
        if task_id not in tasks:
            return task_id, _err(f"no task #{task_id}; existing: {sorted(tasks, key=int)}")
        return task_id, None

    async def task_create(args):
        subject = str(args.get("subject") or "").strip()
        if not subject:
            return _err("subject is required")
        task_id = str(len(tasks) + 1)
        tasks[task_id] = {"id": task_id, "subject": subject,
                          "description": str(args.get("description") or "").strip(), "status": "pending"}
        return _text(f"created task #{task_id}: {subject}")

    async def task_update(args):
        task_id, error = _lookup(args)
        if error:
            return error
        status = str(args.get("status") or "").strip()
        if status not in statuses:
            return _err(f"status must be one of {statuses}")
        tasks[task_id]["status"] = status
        return _text(f"updated {_render(tasks[task_id])}")

    async def task_list(_args):
        if not tasks:
            return _text("no tasks yet")
        done = sum(task["status"] == "completed" for task in tasks.values())
        return _text("\n".join(_render(tasks[key]) for key in sorted(tasks, key=int))
                     + f"\n({done}/{len(tasks)} completed)")

    async def task_get(args):
        task_id, error = _lookup(args)
        return error or _text(_render(tasks[task_id]))

    return [
        ToolSpec("TaskCreate", "Create a task on your session task list (a progress checklist). Returns its id.",
                 {"subject": str, "description": str}, task_create),
        ToolSpec("TaskUpdate", "Set a task's status: pending | in_progress | completed.",
                 {"taskId": str, "status": str}, task_update),
        ToolSpec("TaskList", "List every task on your session task list with its status.", {}, task_list),
        ToolSpec("TaskGet", "Show one task by id.", {"taskId": str}, task_get),
    ]


# --------------------------------------------------------------------------
# Shared assembly for the host-served backends
# --------------------------------------------------------------------------


def served_tools(tools: list[ToolSpec], cwd: str, allowed_builtin: tuple[str, ...]) -> list[ToolSpec]:
    """The application's tools plus the requested host builtins, as the one
    flat table a host-served backend registers with its SDK."""
    served = list(tools) + readonly_tools(cwd, allowed_builtin)
    if "tasks" in allowed_builtin:
        served += task_tools()
    return served


def with_runtime_instructions(system_prompt: str | None, cwd: str, submit_tool: str) -> str:
    """Append the runtime facts Claude Code would state on its own (working
    directory, completion condition) to the application's system prompt."""
    runtime = (
        f"Harness runtime: the working directory is {cwd!r}. Relative paths in the task resolve from "
        "that directory. Use only the provided tools; do not guess alternate workspace roots. "
        f"The run is complete only after a successful {submit_tool} call."
    )
    return f"{system_prompt.rstrip()}\n\n{runtime}" if system_prompt else runtime
