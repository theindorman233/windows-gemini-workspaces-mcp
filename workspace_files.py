import ctypes
import ntpath
import json
import os
import re
import stat
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path, PureWindowsPath

from synthetic_edits import atomic_write, connect, decode_text, digest, read_bytes, reparse


def load_workspaces() -> dict[str, Path]:
    raw = os.environ.get("MCP_WORKSPACES", "").strip()
    if raw:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("MCP_WORKSPACES must be a JSON object")
        return {str(k).strip(): Path(str(v)) for k, v in data.items() if str(k).strip()}
    cfg = Path(__file__).resolve().with_name("workspaces.json")
    if cfg.is_file():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k).strip(): Path(str(v)) for k, v in data.items() if str(k).strip()}
    return {"demo": Path.cwd()}

WORKSPACES = load_workspaces()
TEXT_SUFFIXES = frozenset(".md .txt .rst .tex .bib .csv .tsv .json .yaml .yml .toml .ini .py .ps1 .sh .js .mjs .cjs .ts .tsx .jsx .html .css .xml .sql .r .do .rs .go .java .kt .c .h .cpp .hpp .cs .vue .svelte".split())
PRIVATE_PARTS = {
    ".git", ".hg", ".svn", ".ssh", ".gnupg", ".aws", ".azure", ".venv",
    "node_modules", "__pycache__", "captured_request.txt", "trace.jsonl",
}
PRIVATE_NAME = re.compile(r"(?:^|[._-])(?:credential[s]?|secret[s]?|password[s]?|token[s]?)(?:$|[._-])", re.I)
MAX_TEXT_BYTES = 256 * 1024
MAX_SNIPPET_CHARS = 4096
SEARCH_SCAN_LIMIT = 64


def detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > 0 and crlf >= lf else "\n"


def normalize_newlines(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n")
    return normalized.replace("\n", "\r\n") if newline == "\r\n" else normalized


def reserved_windows_name(part: str) -> bool:
    if hasattr(ntpath, "isreserved"):
        return ntpath.isreserved(part)
    return PureWindowsPath(part).is_reserved()


def workspace_path(_store: Path, name: str) -> Path:
    workspace, separator, relative = name.partition("/")
    root = WORKSPACES.get(workspace)
    if root is None or not separator or len(relative) > 1000:
        raise ValueError("choose a workspace and a relative text-file path")
    parts = relative.split("/")
    for part in parts:
        if (not part or part != part.strip() or part.endswith(".") or reserved_windows_name(part)
                or any(character in '<>:"\\|?*' or ord(character) < 32 for character in part)
                or part.casefold() in PRIVATE_PARTS or part.casefold().startswith(".env")
                or PRIVATE_NAME.search(part)):
            raise ValueError("invalid or protected workspace path")
    if PureWindowsPath(parts[-1]).suffix.casefold() not in TEXT_SUFFIXES:
        raise ValueError("unsupported workspace text-file extension")
    return root.joinpath(*parts)


def resolve_workspace_target(store: Path, name: str) -> Path:
    target = workspace_path(store, name)
    for directory in (*reversed(target.parent.parents), target.parent):
        attributes = directory.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or reparse(attributes):
            raise ValueError("workspace path must use ordinary directories")
    if target.resolve(strict=False) != target:
        raise ValueError("workspace path must use its canonical name")
    return target


def read_workspace_text(store: Path, name: str) -> dict:
    with closing(connect(store)) as connection:
        content = exported_content(connection, name)
    if content is None:
        return {"path": name, "version": "missing", "text": ""}
    return {"path": name, "version": digest(content), "text": decode_text(content)}


def search_workspace(store: Path, query: str, workspace: str = "", mode: str = "content",
                     limit: int = 20, after_path: str = "", after_line: int = 0) -> dict:
    if not query or len(query) > 256 or "\n" in query or "\r" in query:
        raise ValueError("provide a literal single-line query of 1 to 256 characters")
    if workspace and workspace not in WORKSPACES:
        raise ValueError("unknown workspace")
    if mode not in {"content", "path"} or not 1 <= limit <= 50 or after_line < 0 or len(after_path) > 1100:
        raise ValueError("invalid search options")
    needle = query.casefold()
    hits = []
    cursor = None
    with closing(connect(store)) as connection:
        columns = "path, target" if mode == "path" else "path, target, content"
        prefix = workspace + "/" if workspace else ""
        upper = workspace + "0" if workspace else "\U0010ffff"
        lower = max(after_path, prefix)
        include_lower = lower != after_path or (mode == "content" and after_line > 0)
        rows = connection.execute(
            f"SELECT {columns} FROM exports WHERE path >= ? AND path < ? "
            "AND (path > ? OR ?) ORDER BY path LIMIT ?",
            (lower, upper, lower, include_lower, SEARCH_SCAN_LIMIT + 1),
        )
        for scanned, row in enumerate(rows):
            if scanned == SEARCH_SCAN_LIMIT:
                return {"hits": hits, "next": cursor}
            path, target = row[:2]
            content = row[2] if mode == "content" else None
            if target != str(workspace_path(store, path)):
                cursor = {"after_path": path, "after_line": 0}
                continue
            start = after_line if path == after_path else 0
            if mode == "path":
                matches = [(0, "")] if path != after_path and needle in path.casefold() else []
            else:
                lines = decode_text(content).splitlines() if content is not None else []
                matches = ((line, text) for line, text in enumerate(lines, 1)
                           if line > start and needle in text.casefold())
            for line, text in matches:
                if len(hits) == limit:
                    return {"hits": hits, "next": cursor}
                hit = {"path": path, "line": line}
                if mode == "content":
                    folded = text.casefold()
                    position = folded.find(needle)
                    if len(folded) != len(text):
                        remaining = position
                        position = 0
                        while remaining > 0:
                            remaining -= len(text[position].casefold())
                            position += 1
                        if remaining < 0:
                            position -= 1
                    hit["text"] = text[max(0, position - 100):position + 400]
                hits.append(hit)
                cursor = {"after_path": path, "after_line": line}
            cursor = {"after_path": path, "after_line": 0}
    return {"hits": hits, "next": None}


def read_workspace_ranges(store: Path, ranges: list[dict], *, reader=None) -> list[dict]:
    if not 1 <= len(ranges) <= 8:
        raise ValueError("request 1 to 8 file ranges")
    results = []
    with ExitStack() as stack:
        connection = None
        if reader is None:
            connection = stack.enter_context(closing(connect(store)))
            stack.enter_context(connection)
            connection.execute("BEGIN")
        for entry in ranges:
            path, start, end = entry["path"], entry["start_line"], entry["end_line"]
            column = entry.get("start_column", 1)
            if not 1 <= start <= end or end - start >= 120 or column < 1:
                raise ValueError("each range must contain 1 to 120 lines, numbered from 1")
            content = reader(store, path) if reader is not None else exported_content(connection, path)
            lines = decode_text(content).splitlines(keepends=True) if content is not None else []
            if column != 1 and (start > len(lines) or column > len(lines[start - 1])):
                raise ValueError("start column is outside the requested line")
            show = []
            remaining = MAX_SNIPPET_CHARS
            last_line = None
            continuation = None
            for number in range(start, min(end, len(lines)) + 1):
                offset = column - 1 if number == start else 0
                part = lines[number - 1][offset:]
                if remaining:
                    show.append(part[:remaining])
                    last_line = number
                if len(part) > remaining:
                    continuation = {"path": path, "start_line": number, "start_column": offset + remaining + 1,
                                    "end_line": end}
                    break
                remaining -= len(part)
            results.append({"path": path, "version": digest(content) if content is not None else "missing",
                            "start_line": start, "start_column": column, "end_line": last_line,
                            "total_lines": len(lines), "text": "".join(show), "next": continuation,
                            "truncated": continuation is not None})
    return results


def propose_workspace_patch(store: Path, name: str, version: str, old_text: str, new_text: str) -> dict:
    from synthetic_edits import propose_edit

    if not old_text or len(old_text) > 16000 or len(new_text) > 16000:
        raise ValueError("provide nonempty old text and at most 16000 characters per fragment")
    snapshot = read_workspace_text(store, name)
    if version == "missing" or snapshot["version"] != version:
        raise ValueError("snapshot version changed or file does not exist")
    text = snapshot["text"]
    primary_nl = detect_newline(text)
    matched_old = old_text
    start = text.find(matched_old)
    if start < 0 and primary_nl == "\r\n" and "\n" in old_text:
        candidate_old = normalize_newlines(old_text, "\r\n")
        if candidate_old != old_text:
            start = text.find(candidate_old)
            if start >= 0:
                matched_old = candidate_old
    if start < 0:
        raise ValueError("edits[0]: old_text not found")
    pos = text.find(matched_old, start + 1)
    if pos >= 0:
        count = 1
        while pos >= 0:
            count += 1
            pos = text.find(matched_old, pos + 1)
        raise ValueError(f"edits[0]: old_text matched {count} times (must be unique; must match exactly once)")
    normalized_new = normalize_newlines(new_text, primary_nl) if ("\n" in new_text or "\r\n" in new_text) else new_text
    revised = text[:start] + normalized_new + text[start + len(matched_old):]
    return propose_edit(store, name, version, revised, **PROPOSAL_SETTINGS)


def exported_content(connection, name: str) -> bytes | None:
    target = workspace_path(Path(), name)
    row = connection.execute("SELECT target, content FROM exports WHERE path=?", (name,)).fetchone()
    if row is None or row[0] != str(target):
        raise ValueError("select this path with the local share command first")
    return row[1]


def snapshot_version(connection, name: str) -> str:
    content = exported_content(connection, name)
    return digest(content) if content is not None else "missing"


@contextmanager
def windows_path_guard(store: Path, name: str, *, share_delete: bool = True):
    if os.name != "nt":
        raise RuntimeError("workspace file actions require Windows")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    target = resolve_workspace_target(store, name)
    with ExitStack() as pins:
        for directory in (*reversed(target.parent.parents), target.parent):
            handle = create_file(str(directory), 1, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            pins.callback(close_handle, handle)
            attributes = directory.lstat()
            if not stat.S_ISDIR(attributes.st_mode) or reparse(attributes):
                raise ValueError("workspace directory changed")
        if target.exists():
            handle = create_file(str(target), 0x80000000, 5 if share_delete else 1, None, 3, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            pins.callback(close_handle, handle)
        yield resolve_workspace_target(store, name)


def share_workspace_text(store: Path, name: str) -> dict:
    with windows_path_guard(store, name) as target:
        data = read_bytes(target, MAX_TEXT_BYTES)
    if data is not None:
        decode_text(data)
    with closing(connect(store)) as connection, connection:
        connection.execute("INSERT INTO exports VALUES (?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                           "target=excluded.target, content=excluded.content", (name, str(target), data))
    return {"path": name, "version": digest(data) if data is not None else "missing", "status": "shared"}


def replace_preserving_acl(staged: Path, target: Path) -> None:
    from ctypes import wintypes

    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                             wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
    replace_file.restype = wintypes.BOOL
    if not replace_file(str(target), str(staged), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def guarded_write(store: Path, name: str, data: bytes, expected: str) -> None:
    with windows_path_guard(store, name) as target:
        current = read_bytes(target, MAX_TEXT_BYTES)
        if (digest(current) if current is not None else "missing") != expected:
            raise ValueError("workspace file changed before writing")
        atomic_write(target, data, create=current is None, replace=replace_preserving_acl)


EDIT_SETTINGS = {"resolve_target": resolve_workspace_target, "limit": MAX_TEXT_BYTES,
                 "scope": "workspaces-v1", "guarded_write": guarded_write}
PROPOSAL_SETTINGS = {"resolve_target": workspace_path, "limit": MAX_TEXT_BYTES,
                     "scope": "workspaces-v1", "snapshot_version": snapshot_version}
