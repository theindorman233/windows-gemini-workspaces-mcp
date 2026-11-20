import difflib
import json
import os
import re
import shutil
import secrets
import stat
import subprocess
import threading
import time
from bisect import bisect_right
from collections import deque
from contextlib import closing, contextmanager
from pathlib import Path

from synthetic_edits import (
    atomic_write, connect, decode_text, digest, local_action, read_bytes, regular_file, reparse, validate_root,
)
from workspace_files import (
    EDIT_SETTINGS, MAX_TEXT_BYTES, PRIVATE_NAME, PRIVATE_PARTS, TEXT_SUFFIXES, WORKSPACES,
    detect_newline, normalize_newlines, replace_preserving_acl, resolve_workspace_target,
    windows_path_guard, workspace_path,
)


def _resolve_tool(env_var: str, exe_names: tuple[str, ...], fallback: Path) -> Path:
    configured = os.environ.get(env_var, "").strip()
    if configured:
        return Path(configured)
    for name in exe_names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return fallback

_TOOLS_BIN = Path.home() / "Tools" / "bin"
EVERYTHING = _resolve_tool("MCP_ES_PATH", ("es.exe", "es"), _TOOLS_BIN / "es.exe")
RIPGREP = _resolve_tool("MCP_RG_PATH", ("rg.exe", "rg"), _TOOLS_BIN / "rg.exe")
AST_GREP = _resolve_tool("MCP_AST_GREP_PATH", ("ast-grep.exe", "sg.exe", "ast-grep"), _TOOLS_BIN / "ast-grep.exe")
GIT = _resolve_tool("MCP_GIT_PATH", ("git.exe", "git"), Path("C:/Program Files/Git/cmd/git.exe"))
CONTENT_SCAN_LIMIT = 2048
CONTENT_SCAN_SECONDS = 5
GIT_OUTPUT_CHARS = 8000
AST_RESULT_BYTES = 4 * 1024 * 1024
GIT_OPERATIONS = frozenset({"status", "log", "diff"})
AST_LANGUAGES = frozenset(
    "python javascript typescript tsx go rust java kotlin c cpp csharp php ruby bash yaml json html css".split())
EVERYTHING_LIMIT = 512
EVERYTHING_EXCLUDES = ("!\\.", "!\\node_modules\\", "!\\diagnostics\\", "!\\__pycache__\\")
RG_IGNORE_GLOBS = ("!.git", "!node_modules", "!diagnostics", "!__pycache__", "!.venv", "!venv", "!dist", "!build")
LIST_DIR_SKIP_NAMES = frozenset({
    "node_modules", "__pycache__", "diagnostics", "venv",
    "captured_request.txt", "trace.jsonl", "capture_codex_request.py", "id_rsa.txt", "id_ed25519.txt",
})
MAX_DIRECT_BACKUPS = 200
DRIVE_PATH = re.compile(
    r"""(?:"(?:[A-Za-z]:[\\/]|\\\\)[^"]*"|'(?:[A-Za-z]:[\\/]|\\\\)[^']*'|(?:[A-Za-z]:[\\/]|\\\\)[^\n"']*)"""
)
BATCH_SCOPE = "live-workspace-batch-v1"
EDIT_SCOPE = "live-workspace-edit-v1"


def live_target(store: Path, path: str) -> Path:
    target = resolve_workspace_target(store, path)
    parts = path.split("/")[1:]
    if any(part.startswith(".") for part in parts) or any(part.casefold() in {
        "diagnostics", "capture_codex_request.py", "id_rsa.txt", "id_ed25519.txt",
    } for part in parts):
        raise ValueError("protected workspace path")
    return target


def live_edit_target(store: Path, path: str) -> Path:
    target = live_target(store, path)
    if target.is_relative_to(Path(__file__).resolve().parent):
        raise ValueError("the running MCP project is not editable through this server")
    return target


LIVE_SETTINGS = {**EDIT_SETTINGS, "resolve_target": live_edit_target, "scope": EDIT_SCOPE,
                 "block_on_other_intents": False}


_DIRECT_WRITE_LOCK = threading.Lock()


@contextmanager
def local_direct_lock(store: Path):
    import msvcrt

    validate_root(store)
    with _DIRECT_WRITE_LOCK:
        with (store / "live-direct.lock").open("a+b") as lock:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def local_batch_lock(store: Path):
    import msvcrt

    validate_root(store)
    with (store / "live-batch.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("another local batch operation is running") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def valid_limit(limit: int) -> bool:
    return not isinstance(limit, bool) and isinstance(limit, int) and 1 <= limit <= 50


def normalize_rel_dir(directory: str) -> str:
    if not directory:
        return ""
    normalized = directory.replace("\\", "/").strip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:].lstrip("/")
    return "" if normalized == "." else normalized


def read_live(store: Path, path: str) -> bytes | None:
    live_target(store, path)
    with windows_path_guard(store, path, share_delete=False) as target:
        live_target(store, path)
        return read_bytes(target, MAX_TEXT_BYTES)


def _rg_find_candidates(root: Path) -> list[str]:
    arguments = [str(RIPGREP), "--no-config", "--files", "--no-follow", "--color=never"]
    for glob in RG_IGNORE_GLOBS:
        arguments.extend(["-g", glob])
    arguments.extend(["--", str(root)])
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Everything is unavailable or did not finish within five seconds") from None
    if result.returncode not in {0, 1} and not result.stdout:
        raise ValueError("Everything is unavailable or did not finish within five seconds")
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def find_files(store: Path, workspace: str, query: str, limit: int = 20) -> dict:
    if workspace not in WORKSPACES or not valid_limit(limit):
        raise ValueError("choose a workspace and a limit of 1 to 50")
    if not query.strip() or len(query) > 120 or any(ord(char) < 32 for char in query):
        raise ValueError("provide a literal filename fragment of 1 to 120 characters")
    root = WORKSPACES[workspace]
    pattern = "".join(f"\\x{{{ord(char):x}}}" for char in query)
    candidates = None
    truncated_by_backend = False
    try:
        result = subprocess.run(
            [str(EVERYTHING), "-n", str(EVERYTHING_LIMIT), *EVERYTHING_EXCLUDES,
             "-path", f"{root}\\", "/a-d", "nopath:", "-regex", pattern],
            capture_output=True, timeout=5, check=True,
        )
        try:
            candidates = result.stdout.decode("utf-8-sig").splitlines()
        except UnicodeDecodeError:
            raise ValueError("Everything returned non-UTF-8 paths") from None
        truncated_by_backend = len(candidates) == EVERYTHING_LIMIT
    except (OSError, subprocess.SubprocessError):
        candidates = _rg_find_candidates(root)
    hits = []
    for candidate in candidates:
        try:
            target = Path(candidate)
            if not target.is_absolute():
                target = root / target
            key = f"{workspace}/{target.relative_to(root).as_posix()}"
            if query.casefold() not in target.name.casefold():
                continue
            if live_target(store, key) != target or not target.is_file():
                continue
            if not regular_file(target):
                continue
        except (OSError, ValueError):
            continue
        hits.append(key)
        if len(hits) > limit:
            break
    return {"paths": hits[:limit], "truncated": len(hits) > limit or truncated_by_backend}


def scan_content_chunk(documents: list[tuple[str, list[str]]], query: str, limit: int, timeout: float) -> list[dict]:
    starts, lines = [], []
    for _, document in documents:
        starts.append(len(lines))
        lines.extend(document)
    try:
        result = subprocess.run(
            [str(RIPGREP), "--no-config", "--text", "--fixed-strings", "--ignore-case", "--line-number",
             "--no-heading", "--color=never", "--max-count", str(limit), "-e", query, "--", "-"],
            input=("\n".join(lines) + "\n").encode("utf-8"), capture_output=True, timeout=timeout,
        )
    except OSError:
        raise ValueError("ripgrep is unavailable") from None
    if result.returncode not in {0, 1}:
        raise ValueError("ripgrep could not search the requested text")
    hits = []
    for record in result.stdout.decode("utf-8").splitlines():
        number, _, text = record.partition(":")
        index = int(number) - 1
        document = bisect_right(starts, index) - 1
        match = re.search(re.escape(query), text, re.IGNORECASE)
        start = max(0, match.start() - 100) if match else 0
        hits.append({"path": documents[document][0], "line": index - starts[document] + 1,
                     "text": text[start:start + 400]})
    return hits


def _search_content_fallback(store: Path, prefix: str, folder: Path, query: str, limit: int) -> dict:
    deadline = time.monotonic() + CONTENT_SCAN_SECONDS
    hits, documents = [], []
    scanned = chunk_bytes = 0
    truncated = False
    skipped = 0
    pending = [(prefix, folder)]
    use_rg = RIPGREP.is_file()
    needle = query.casefold()

    def flush() -> bool:
        nonlocal chunk_bytes
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if use_rg:
            try:
                hits.extend(scan_content_chunk(documents, query, limit + 1 - len(hits), min(2, remaining)))
            except (subprocess.TimeoutExpired, ValueError):
                documents.clear()
                return False
        else:
            for doc_path, doc_lines in documents:
                for line_no, line_text in enumerate(doc_lines, 1):
                    if needle in line_text.casefold():
                        match = re.search(re.escape(query), line_text, re.IGNORECASE)
                        start = max(0, match.start() - 100) if match else 0
                        hits.append({"path": doc_path, "line": line_no, "text": line_text[start:start + 400]})
                        if len(hits) > limit:
                            break
                if len(hits) > limit:
                    break
        documents.clear()
        chunk_bytes = 0
        return len(hits) <= limit

    while pending and not truncated:
        if time.monotonic() >= deadline or scanned >= CONTENT_SCAN_LIMIT:
            truncated = True
            break
        parent_key, parent = pending.pop()
        try:
            with windows_path_guard(store, f"{parent_key}/search-scope-check.txt", share_delete=False):
                with os.scandir(parent) as entries:
                    for entry in entries:
                        if scanned >= CONTENT_SCAN_LIMIT or time.monotonic() >= deadline:
                            truncated = True
                            break
                        scanned += 1
                        path = f"{parent_key}/{entry.name}"
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.casefold() in LIST_DIR_SKIP_NAMES:
                                    continue
                                live_target(store, f"{path}/search-scope-check.txt")
                                pending.append((path, Path(entry.path)))
                                continue
                            if Path(entry.name).suffix.casefold() not in TEXT_SUFFIXES:
                                continue
                            content = read_live(store, path)
                            if not content:
                                continue
                            lines = decode_text(content).splitlines()
                        except (ValueError, OSError):
                            skipped += 1
                            continue
                        documents.append((path, lines))
                        chunk_bytes += len(content)
                        if (chunk_bytes >= 512 * 1024 or len(documents) >= 64) and not flush():
                            truncated = True
                            break
        except (ValueError, OSError):
            skipped += 1
    if documents and len(hits) <= limit and not flush():
        truncated = True
    return {"hits": hits[:limit], "truncated": truncated or len(hits) > limit, "skipped": skipped}


def search_content(store: Path, workspace: str, query: str, directory: str = "", limit: int = 20) -> dict:
    if workspace not in WORKSPACES or not valid_limit(limit):
        raise ValueError("choose a workspace and a limit of 1 to 50")
    if not query.strip() or len(query) > 120 or len(query.splitlines()) != 1 or any(ord(char) < 32 for char in query):
        raise ValueError("provide a literal single-line query of 1 to 120 characters")
    directory = normalize_rel_dir(directory)
    prefix = f"{workspace}/{directory}" if directory else workspace
    try:
        folder = live_target(store, f"{prefix}/search-scope-check.txt").parent
    except OSError:
        raise ValueError("workspace directory is unavailable") from None
    if not RIPGREP.is_file():
        return _search_content_fallback(store, prefix, folder, query, limit)

    arguments = [
        str(RIPGREP), "--no-config", "--no-follow", "--max-filesize", "256K",
        "--line-number", "--no-heading", "--with-filename", "--null",
        "--color=never", "--fixed-strings", "--ignore-case",
    ]
    for glob in RG_IGNORE_GLOBS:
        arguments.extend(["-g", glob])
    arguments.extend(["-e", query, "--", str(folder)])
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=CONTENT_SCAN_SECONDS)
    except OSError:
        return _search_content_fallback(store, prefix, folder, query, limit)
    except subprocess.TimeoutExpired:
        return {"hits": [], "truncated": True, "skipped": 0}
    if result.returncode not in {0, 1} and not result.stdout:
        raise ValueError("ripgrep could not search the requested text")

    root = WORKSPACES[workspace]
    hits = []
    skipped = 0
    truncated = False
    validated_files: dict[str, str | None] = {}
    for record in result.stdout.decode("utf-8", errors="replace").splitlines():
        raw_path, sep, rest = record.partition("\x00")
        if not sep:
            continue
        num_str, num_sep, line_text = rest.partition(":")
        if not num_sep or not num_str.isdigit():
            continue
        if raw_path not in validated_files:
            try:
                target = Path(raw_path)
                key = f"{workspace}/{target.relative_to(root).as_posix()}"
                if live_target(store, key) != target or not regular_file(target):
                    validated_files[raw_path] = None
                    skipped += 1
                    continue
                validated_files[raw_path] = key
            except (OSError, ValueError):
                validated_files[raw_path] = None
                skipped += 1
                continue
        key = validated_files[raw_path]
        if key is None:
            continue
        if len(hits) >= limit:
            truncated = True
            break
        clean_text = line_text.rstrip("\r\n")
        match = re.search(re.escape(query), clean_text, re.IGNORECASE)
        start = max(0, match.start() - 100) if match else 0
        hits.append({"path": key, "line": int(num_str), "text": clean_text[start:start + 400]})
    if not truncated and CONTENT_SCAN_LIMIT < 2048:
        seen = 0
        try:
            with os.scandir(folder) as entries:
                for _ in entries:
                    seen += 1
                    if seen > CONTENT_SCAN_LIMIT:
                        truncated = True
                        break
        except OSError:
            pass
    return {"hits": hits[:limit], "truncated": truncated, "skipped": skipped}


def list_dir(workspace: str, directory: str = "", depth: int = 2, limit: int = 100) -> dict:
    if workspace not in WORKSPACES:
        raise ValueError("choose a valid workspace")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 5:
        raise ValueError("depth must be an integer between 1 and 5")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    directory = normalize_rel_dir(directory)
    prefix = f"{workspace}/{directory}" if directory else workspace
    try:
        folder = live_target(Path(), f"{prefix}/list-dir-check.txt").parent
    except OSError:
        raise ValueError("workspace directory is unavailable") from None
    entries = []
    truncated = False
    queue: deque[tuple[str, Path, int]] = deque([(prefix, folder, 1)])
    while queue and not truncated:
        parent_key, parent_path, level = queue.popleft()
        try:
            with os.scandir(parent_path) as it:
                items = sorted(it, key=lambda e: e.name.casefold())
        except OSError:
            continue
        for entry in items:
            name = entry.name
            lowered = name.casefold()
            if (name.startswith(".") or lowered in LIST_DIR_SKIP_NAMES
                    or lowered in PRIVATE_PARTS or PRIVATE_NAME.search(name)):
                continue
            try:
                attrs = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if reparse(attrs):
                continue
            entry_key = f"{parent_key}/{name}"
            if stat.S_ISDIR(attrs.st_mode):
                if len(entries) >= limit:
                    truncated = True
                    break
                entries.append({"path": entry_key, "kind": "dir", "size": None})
                if level < depth:
                    queue.append((entry_key, Path(entry.path), level + 1))
            elif stat.S_ISREG(attrs.st_mode):
                if attrs.st_nlink > 1:
                    continue
                if len(entries) >= limit:
                    truncated = True
                    break
                entries.append({"path": entry_key, "kind": "file", "size": attrs.st_size})
    return {
        "workspace": workspace,
        "directory": directory,
        "entries": entries[:limit],
        "truncated": truncated,
    }


def git_query(store: Path, workspace: str, operation: str = "status", directory: str = "",
              path: str = "", limit: int = 20) -> dict:
    if workspace not in WORKSPACES or operation not in GIT_OPERATIONS or not valid_limit(limit):
        raise ValueError("choose a workspace, an operation of status, log or diff, and a limit of 1 to 50")
    if not GIT.is_file():
        raise ValueError("git is unavailable")
    directory = normalize_rel_dir(directory)
    prefix = f"{workspace}/{directory}" if directory else workspace
    try:
        folder = live_target(store, f"{prefix}/git-scope-check.txt").parent
    except OSError:
        raise ValueError("workspace directory is unavailable") from None
    arguments = [str(GIT), "-c", "core.quotepath=false"]
    if operation == "diff":
        if not path or path.partition("/")[0] != workspace:
            raise ValueError("diff requires a file path inside the same workspace")
        try:
            target = live_target(store, path)
            regular_file(target)
        except OSError:
            raise ValueError("diff target is unavailable") from None
        arguments += ["-C", str(target.parent), "diff", "--no-ext-diff", "--no-textconv", "--", str(target)]
    else:
        arguments += ["-C", str(folder)]
        if operation == "status":
            arguments += ["status", "--short", "--branch"]
        else:
            arguments += ["log", "--oneline", f"--max-count={limit}"]
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("git did not finish within five seconds") from None
    if result.returncode != 0:
        detail = DRIVE_PATH.sub("<path>", result.stderr.decode("utf-8", errors="replace").strip())[:200]
        raise ValueError(f"git reported an error: {detail or 'is this a git repository?'}")
    output = result.stdout.decode("utf-8", errors="replace")
    return {"operation": operation, "output": output[:GIT_OUTPUT_CHARS],
            "truncated": len(output) > GIT_OUTPUT_CHARS}


def search_ast(store: Path, workspace: str, pattern: str, language: str, directory: str = "",
               limit: int = 20) -> dict:
    if workspace not in WORKSPACES or language not in AST_LANGUAGES or not valid_limit(limit):
        raise ValueError("choose a workspace, a supported language and a limit of 1 to 50")
    if not pattern.strip() or len(pattern) > 500 or any(ord(char) < 32 and char not in "\n\r\t" for char in pattern):
        raise ValueError("provide an ast-grep pattern of 1 to 500 characters")
    if not AST_GREP.is_file():
        raise ValueError("ast-grep is unavailable")
    directory = normalize_rel_dir(directory)
    prefix = f"{workspace}/{directory}" if directory else workspace
    try:
        folder = live_target(store, f"{prefix}/ast-scope-check.txt").parent
    except OSError:
        raise ValueError("workspace directory is unavailable") from None
    try:
        result = subprocess.run([str(AST_GREP), "run", "--pattern", pattern, "--lang", language,
                                 "--json=compact", "--", str(folder)], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("ast-grep did not finish within five seconds") from None
    if result.returncode not in {0, 1}:
        detail = DRIVE_PATH.sub("<path>", result.stderr.decode("utf-8", errors="replace").strip())[:200]
        raise ValueError(f"ast-grep rejected the query: {detail}")
    if len(result.stdout) > AST_RESULT_BYTES:
        raise ValueError("too many structural matches; narrow the directory")
    try:
        matches = json.loads(result.stdout.decode("utf-8") or "[]")
        if not isinstance(matches, list):
            raise ValueError
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("ast-grep returned unreadable output") from None
    root = WORKSPACES[workspace]
    hits, skipped, truncated = [], 0, False
    for match in matches:
        try:
            target = Path(match["file"])
            key = f"{workspace}/{target.relative_to(root).as_posix()}"
            if live_target(store, key) != target or not regular_file(target):
                skipped += 1
                continue
            line = match["range"]["start"]["line"] + 1
            text = str(match["lines"])[:400]
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            skipped += 1
            continue
        if len(hits) >= limit:
            truncated = True
            continue
        hits.append({"path": key, "line": line, "text": text})
    warning = DRIVE_PATH.sub("<path>", result.stderr.decode("utf-8", errors="replace").strip())[:200]
    return {"hits": hits, "truncated": truncated, "skipped": skipped, "warning": warning}


    return {"freshness": freshness, "columns": columns,
            "rows": [[None if value is None else str(value)[:QUERY_CELL_CHARS] for value in row]
                     for row in rows[:limit]],
            "truncated": len(rows) > limit}


def apply_replacements(original: str, edits: list[dict]) -> str:
    if not 1 <= len(edits) <= 12:
        raise ValueError("provide 1 to 12 replacements per file")
    primary_nl = detect_newline(original)
    spans = []
    for index, edit in enumerate(edits):
        old, new = edit["old_text"], edit["new_text"]
        if not old or len(old) > MAX_TEXT_BYTES or len(new) > MAX_TEXT_BYTES:
            raise ValueError("invalid replacement size")
        matched_old = old
        start = original.find(matched_old)
        if start < 0 and primary_nl == "\r\n" and "\n" in old:
            candidate_old = normalize_newlines(old, "\r\n")
            if candidate_old != old:
                start = original.find(candidate_old)
                if start >= 0:
                    matched_old = candidate_old
        if start < 0:
            raise ValueError(f"edits[{index}]: old_text not found")
        pos = original.find(matched_old, start + 1)
        if pos >= 0:
            count = 1
            while pos >= 0:
                count += 1
                pos = original.find(matched_old, pos + 1)
            raise ValueError(f"edits[{index}]: old_text matched {count} times (must be unique)")
        normalized_new = normalize_newlines(new, primary_nl) if ("\n" in new or "\r\n" in new) else new
        spans.append((start, start + len(matched_old), normalized_new))
    spans.sort()
    revised = original
    next_start = len(original)
    for start, end, new in reversed(spans):
        if end > next_start:
            raise ValueError("replacements overlap")
        revised = revised[:start] + new + revised[end:]
        next_start = start
    return revised


def ensure_live_write_target(store: Path, path: str, *, create_parents: bool = False) -> Path:
    target = workspace_path(store, path)
    parts = path.split("/")[1:]
    if any(part.startswith(".") for part in parts) or any(part.casefold() in {
        "diagnostics", "capture_codex_request.py", "id_rsa.txt", "id_ed25519.txt",
    } for part in parts):
        raise ValueError("protected workspace path")
    if target.is_relative_to(Path(__file__).resolve().parent):
        raise ValueError("the running MCP project is not editable through this server")
    root = WORKSPACES[path.partition("/")[0]]
    for ancestor in (*reversed(root.parents), root):
        attributes = ancestor.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or reparse(attributes):
            raise ValueError("workspace path must use ordinary directories")
    if root.resolve(strict=True) != root:
        raise ValueError("workspace path must use ordinary directories")
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            attributes = current.lstat()
        except FileNotFoundError:
            if not create_parents:
                raise ValueError("workspace directory does not exist") from None
            current.mkdir(exist_ok=True)
            attributes = current.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or reparse(attributes) or current.resolve(strict=False) != current:
            raise ValueError("workspace path must use ordinary directories")
    return live_edit_target(store, path)


def _direct_backup_dir(store: Path) -> Path:
    validate_root(store)
    backup_dir = store / "direct-backups"
    backup_dir.mkdir(exist_ok=True)
    attributes = backup_dir.lstat()
    if not stat.S_ISDIR(attributes.st_mode) or reparse(attributes):
        raise ValueError("invalid backup directory")
    return backup_dir


def save_direct_backup(store: Path, path: str, target: Path, previous_bytes: bytes, new_version: str) -> str:
    backup_dir = _direct_backup_dir(store)
    backup_id = f"{int(time.time())}-{secrets.token_hex(6)}"
    meta = {
        "path": path,
        "target": str(target),
        "previous_version": digest(previous_bytes),
        "new_version": new_version,
        "created_at": int(time.time()),
        "restored": False,
    }
    atomic_write(backup_dir / f"{backup_id}.bak", previous_bytes, create=True)
    atomic_write(backup_dir / f"{backup_id}.json", json.dumps(meta, indent=2).encode("utf-8"), create=True)
    _prune_direct_backups(backup_dir, MAX_DIRECT_BACKUPS)
    return backup_id


def _prune_direct_backups(backup_dir: Path, keep: int = MAX_DIRECT_BACKUPS) -> None:
    if keep < 0:
        return
    records = []
    try:
        for meta_file in backup_dir.glob("*.json"):
            try:
                mtime = meta_file.lstat().st_mtime_ns
            except OSError:
                continue
            records.append((mtime, meta_file.stem))
    except OSError:
        return
    if len(records) <= keep:
        return
    records.sort(reverse=True)
    for _, stem in records[keep:]:
        for ext in (".bak", ".json"):
            try:
                (backup_dir / f"{stem}{ext}").unlink(missing_ok=True)
            except OSError:
                pass


def _discard_direct_backup(store: Path, backup_id: str | None) -> None:
    if not backup_id:
        return
    backup_dir = _direct_backup_dir(store)
    (backup_dir / f"{backup_id}.bak").unlink(missing_ok=True)
    (backup_dir / f"{backup_id}.json").unlink(missing_ok=True)


def restore_direct_backup(store: Path, backup_id: str) -> dict:
    if not re.fullmatch(r"[0-9]+-[0-9a-f]{12}", backup_id):
        raise ValueError("invalid backup ID")
    with local_direct_lock(store):
        backup_dir = _direct_backup_dir(store)
        meta_path = backup_dir / f"{backup_id}.json"
        bak_path = backup_dir / f"{backup_id}.bak"
        if not regular_file(meta_path) or not regular_file(bak_path):
            raise ValueError("backup files missing")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("restored"):
            raise ValueError("backup already restored")
        previous = read_bytes(bak_path, MAX_TEXT_BYTES)
        if previous is None or digest(previous) != meta["previous_version"]:
            raise ValueError("backup payload missing or changed")
        with windows_path_guard(store, meta["path"]) as target:
            if str(live_edit_target(store, meta["path"])) != meta["target"]:
                raise ValueError("backup target moved")
            current = read_bytes(target, MAX_TEXT_BYTES)
            if current is None or digest(current) != meta["new_version"]:
                raise ValueError("file changed after direct edit; refusing to overwrite")
            atomic_write(target, previous, create=False, replace=replace_preserving_acl)
        meta["restored"] = True
        atomic_write(meta_path, json.dumps(meta, indent=2).encode("utf-8"), create=False)
        return {"status": "restored", "path": meta["path"], "version": meta["previous_version"]}


def edit_live_file(store: Path, path: str, edits: list[dict], version: str = "") -> dict:
    """Apply exact unique replacements directly to an existing workspace file with automatic backup."""
    try:
        with local_direct_lock(store):
            ensure_live_write_target(store, path, create_parents=False)
            with windows_path_guard(store, path) as target:
                live_edit_target(store, path)
                current = read_bytes(target, MAX_TEXT_BYTES)
                if current is None:
                    return {"ok": False, "error": "target file does not exist; use write_workspace_file to create it"}
                previous_version = digest(current)
                if version and previous_version != version:
                    return {"ok": False, "error": "file version changed; read it again"}
                revised = apply_replacements(decode_text(current), edits)
                data = revised.encode("utf-8")
                if not data or b"\0" in data or len(data) > MAX_TEXT_BYTES or data == current:
                    return {"ok": False, "error": "replacement must be changed, nonempty UTF-8 text of at most 256 KiB"}
                new_version = digest(data)
                backup_id = save_direct_backup(store, path, target, current, new_version)
                try:
                    atomic_write(target, data, create=False, replace=replace_preserving_acl)
                except BaseException:
                    _discard_direct_backup(store, backup_id)
                    raise
                return {
                    "ok": True,
                    "path": path,
                    "version": new_version,
                    "previous_version": previous_version,
                    "backup_id": backup_id,
                }
    except (ValueError, UnicodeDecodeError, OSError) as error:
        return {"ok": False, "error": DRIVE_PATH.sub("<path>", str(error))[:300]}


def write_live_file(store: Path, path: str, content: str, version: str = "") -> dict:
    """Create or overwrite a UTF-8 workspace file directly, saving a backup before overwriting."""
    try:
        data = content.encode("utf-8")
        if b"\0" in data or len(data) > MAX_TEXT_BYTES:
            return {"ok": False, "error": f"content must be UTF-8 text of at most {MAX_TEXT_BYTES} bytes without NUL"}
        with local_direct_lock(store):
            create_parents = (not version) or (version == "missing")
            ensure_live_write_target(store, path, create_parents=create_parents)
            with windows_path_guard(store, path) as target:
                live_edit_target(store, path)
                current = read_bytes(target, MAX_TEXT_BYTES)
                previous_version = digest(current) if current is not None else "missing"
                if version and previous_version != version:
                    return {"ok": False, "error": "file version changed; read it again"}
                if current is not None and "\r\n" not in content and "\n" in content:
                    try:
                        if detect_newline(decode_text(current)) == "\r\n":
                            content = normalize_newlines(content, "\r\n")
                            data = content.encode("utf-8")
                            if len(data) > MAX_TEXT_BYTES:
                                return {"ok": False, "error": f"content must be UTF-8 text of at most {MAX_TEXT_BYTES} bytes without NUL"}
                    except (ValueError, UnicodeDecodeError):
                        pass
                new_version = digest(data)
                if current is not None and current == data:
                    return {
                        "ok": True,
                        "path": path,
                        "version": new_version,
                        "previous_version": previous_version,
                        "backup_id": None,
                        "unchanged": True,
                    }
                backup_id = save_direct_backup(store, path, target, current, new_version) if current is not None else None
                try:
                    atomic_write(target, data, create=current is None, replace=replace_preserving_acl)
                except BaseException:
                    _discard_direct_backup(store, backup_id)
                    raise
                return {
                    "ok": True,
                    "path": path,
                    "version": new_version,
                    "previous_version": previous_version,
                    "backup_id": backup_id,
                }
    except (ValueError, UnicodeEncodeError, OSError) as error:
        return {"ok": False, "error": DRIVE_PATH.sub("<path>", str(error))[:300]}


def build_edit(store: Path, item: dict) -> dict:
    path = item["path"]
    version = item["version"]
    target = live_edit_target(store, path)
    current = read_live(store, path)
    if current is None or digest(current) != version:
        raise ValueError("file version changed; read it again")
    revised = apply_replacements(decode_text(current), item["edits"])
    data = revised.encode()
    if not data or b"\0" in data or len(data) > MAX_TEXT_BYTES or data == current:
        raise ValueError("replacement must be changed, nonempty UTF-8 text of at most 256 KiB")
    return {"path": path, "version": version, "replacement": revised,
            "scope": EDIT_SCOPE, "target": str(target)}


def serialize(value: dict) -> tuple[str, str]:
    body = json.dumps(value, sort_keys=True)
    return digest(body.encode()), body


def propose_batch(store: Path, files: list[dict]) -> dict:
    if not 1 <= len(files) <= 8:
        raise ValueError("provide 1 to 8 distinct files per batch")
    items = [build_edit(store, item) for item in files]
    if len({item["target"].casefold() for item in items}) != len(items):
        raise ValueError("batch contains a duplicate file")
    request, _ = serialize({"scope": BATCH_SCOPE, "files": items})
    batch, body = serialize({"scope": BATCH_SCOPE, "files": items, "request": request, "attempt": secrets.token_hex(16)})
    with closing(connect(store)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT id, state FROM proposals WHERE state IN ('pending', 'batch-applying') "
                                      "AND json_extract(body, '$.request')=?", (request,)).fetchone()
        if existing:
            return {"proposal_id": existing[0], "status": existing[1], "files": len(items)}
        if connection.execute("SELECT COUNT(*) FROM proposals WHERE state IN ('pending', 'batch-applying') "
                              "AND json_extract(body, '$.scope')=?", (BATCH_SCOPE,)).fetchone()[0] >= 32:
            raise ValueError("pending batch limit reached; apply or cancel outstanding batches locally")
        connection.execute("INSERT INTO proposals VALUES (?, ?, 'pending', NULL)", (batch, body))
    return {"proposal_id": batch, "status": "pending", "files": len(items)}


def batch_action(store: Path, command: str, batch: str, approval: str = "") -> dict:
    if command == "batch-status":
        return _batch_action(store, command, batch, approval)
    with local_batch_lock(store):
        return _batch_action(store, command, batch, approval)


def restore_batch_file(store: Path, child: str) -> dict:
    with local_batch_lock(store):
        with closing(connect(store)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT body, state FROM proposals WHERE id=?", (child,)).fetchone()
            if row is None or digest(row[0].encode()) != child:
                raise ValueError("batch file proposal missing or changed")
            item = json.loads(row[0])
            parent = item.get("batch")
            if connection.execute("SELECT 1 FROM proposals WHERE id=? AND state IN ('applied', 'cancelled')",
                                  (parent,)).fetchone() is None:
                raise ValueError("finish or cancel the batch before restoring files")
            if connection.execute("SELECT 1 FROM proposals WHERE state='batch-applying'").fetchone():
                raise ValueError("resume or cancel the interrupted batch first")
            if row[1] == "restore-pending":
                if str(live_edit_target(store, item["path"])) != item["target"]:
                    raise ValueError("batch target moved")
                current = read_live(store, item["path"])
                version = digest(current) if current is not None else "missing"
                if version == item["version"]:
                    connection.execute("UPDATE proposals SET state='restored' WHERE id=?", (child,))
                    return {"status": "restored", "path": item["target"]}
                if version != digest(item["replacement"].encode()):
                    raise ValueError("file version changed; no third-party changes were overwritten")
                connection.execute("UPDATE proposals SET state='applied' WHERE id=?", (child,))
        return local_action(store, "restore", child, **LIVE_SETTINGS)


def _batch_action(store: Path, command: str, batch: str, approval: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", batch):
        raise ValueError("invalid batch ID")
    with closing(connect(store)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT body, state, approval FROM proposals WHERE id=?", (batch,)).fetchone()
        if row is None or digest(row[0].encode()) != batch:
            raise ValueError("batch missing or changed")
        manifest = json.loads(row[0])
        if manifest.get("scope") != BATCH_SCOPE:
            raise ValueError("not a live workspace batch")
        items = manifest["files"]
        state = row[1]
        if command == "batch-status":
            statuses = []
            for position, item in enumerate(items):
                child, _ = serialize({**item, "batch": batch, "position": position})
                child_row = connection.execute("SELECT state FROM proposals WHERE id=?", (child,)).fetchone()
                child_state = child_row[0] if child_row else ("cancelled" if state == "cancelled" else "pending")
                statuses.append({"path": item["path"], "restore_id": child, "status": child_state})
            return {"status": state, "files": statuses}
        if command == "cancel-batch":
            if state not in {"pending", "batch-applying"}:
                raise ValueError("batch is not pending or interrupted")
            for position, item in enumerate(items):
                child, _ = serialize({**item, "batch": batch, "position": position})
                child_row = connection.execute("SELECT state FROM proposals WHERE id=?", (child,)).fetchone()
                if child_row is None or child_row[0] == "applied":
                    continue
                child_state = "restore-pending" if child_row[0] == "applying" else "cancelled"
                connection.execute("UPDATE proposals SET state=?, approval=NULL WHERE id=?", (child_state, child))
            connection.execute("UPDATE proposals SET state='cancelled', approval=NULL WHERE id=?", (batch,))
            return {"status": "cancelled"}
        if command not in {"show-batch", "apply-batch"}:
            raise ValueError("unknown batch command")
        if command == "show-batch" and state != "pending":
            raise ValueError("batch is not pending")
        if command == "apply-batch" and (state not in {"pending", "batch-applying"} or not approval or approval != row[2]):
            raise ValueError("apply requires the local batch approval")
        if command == "apply-batch" and connection.execute(
            "SELECT 1 FROM proposals WHERE id<>? AND state='batch-applying'", (batch,)
        ).fetchone():
            raise ValueError("resume or cancel the interrupted batch first")
        children = []
        previews = []
        for position, item in enumerate(items):
            target = live_edit_target(store, item["path"])
            if str(target) != item["target"]:
                raise ValueError("batch target moved")
            current = read_live(store, item["path"])
            version = digest(current) if current is not None else "missing"
            child, body = serialize({**item, "batch": batch, "position": position})
            child_token = digest(f"{approval}:{child}".encode())
            child_row = connection.execute("SELECT state FROM proposals WHERE id=?", (child,)).fetchone()
            child_state = child_row[0] if child_row else "pending"
            revised = digest(item["replacement"].encode())
            allowed = {"pending": {item["version"]}, "applying": {item["version"], revised}, "applied": {revised}}
            if version not in allowed.get(child_state, set()):
                raise ValueError(f"file version changed: {item['path']}")
            if command == "show-batch":
                diff = list(difflib.unified_diff(decode_text(current or b"").splitlines(keepends=True),
                                                item["replacement"].splitlines(keepends=True),
                                                fromfile=str(target), tofile=str(target)))
                previews.append({"path": str(target), "diff": diff})
            children.append((child, body, child_token, child_state))
        if command == "show-batch":
            token = secrets.token_hex(32)
            connection.execute("UPDATE proposals SET approval=? WHERE id=?", (token, batch))
            return {"proposal_id": batch, "approval_sha256": token, "files": previews}
        for child, body, token, child_state in children:
            if child_state == "pending":
                connection.execute("INSERT OR IGNORE INTO proposals VALUES (?, ?, 'pending', ?)", (child, body, token))
        connection.execute("UPDATE proposals SET state='batch-applying' WHERE id=?", (batch,))
    try:
        for child, _, token, child_state in children:
            if child_state != "applied":
                local_action(store, "apply", child, token, **LIVE_SETTINGS)
    except (ValueError, OSError) as error:
        raise ValueError(f"batch stopped; some files may have changed. Retry the same approval after resolving: {error}") from error
    with closing(connect(store)) as connection, connection:
        connection.execute("UPDATE proposals SET state='applied' WHERE id=?", (batch,))
    return {"status": "applied", "files": [{"path": item["path"], "restore_id": child}
                                          for item, (child, *_) in zip(items, children)]}
