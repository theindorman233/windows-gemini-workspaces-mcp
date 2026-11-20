import argparse
import difflib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path


INITIAL_TEXT = "Windows Gemini synthetic edit fixture.\n"
TARGETS = {"sample.txt", "new.txt"}
MAX_BYTES = 4096


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def initialize(*, seed_fixture: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="gemini-edit-fixture-")).resolve()
    if seed_fixture:
        (root / "sample.txt").write_bytes(INITIAL_TEXT.encode())
    with closing(sqlite3.connect(root / "proposals.db")) as connection, connection:
        connection.execute("CREATE TABLE proposals (id TEXT PRIMARY KEY, body TEXT, state TEXT, approval TEXT)")
        if not seed_fixture:
            connection.execute("CREATE TABLE exports (path TEXT PRIMARY KEY, target TEXT NOT NULL, content BLOB)")
    return root


def connect(root: Path) -> sqlite3.Connection:
    validate_root(root)
    database = root / "proposals.db"
    if not regular_file(database):
        raise ValueError("synthetic proposal store is missing")
    return sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)


def reparse(attributes: os.stat_result) -> bool:
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def validate_root(root: Path) -> None:
    temp_dir = Path(tempfile.gettempdir()).resolve()
    in_temp = root.parent == temp_dir and root.name.startswith("gemini-edit-fixture-")
    in_local_app_data = False
    persistent_base = None
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        persistent_base = (Path(local_app_data) / "WindowsGeminiMCP").resolve(strict=False)
        resolved_root = root.resolve(strict=False)
        in_local_app_data = resolved_root == persistent_base or resolved_root.is_relative_to(persistent_base)
    if not (in_temp or in_local_app_data):
        raise ValueError("expected a generated synthetic workspace")
    attributes = root.lstat()
    if not stat.S_ISDIR(attributes.st_mode) or reparse(attributes):
        raise ValueError("synthetic workspace must be an ordinary directory")
    if in_local_app_data and persistent_base is not None:
        current = root
        while True:
            attrs = current.lstat()
            if not stat.S_ISDIR(attrs.st_mode) or reparse(attrs):
                raise ValueError("synthetic workspace must be an ordinary directory")
            if current.resolve(strict=False) == persistent_base or current.parent == current:
                break
            current = current.parent


def regular_file(target: Path) -> bool:
    try:
        attributes = target.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(attributes.st_mode) or reparse(attributes) or attributes.st_nlink != 1:
        raise ValueError("expected a regular file with one link")
    return True


def target_path(root: Path, name: str) -> Path:
    if name not in TARGETS:
        raise ValueError("unknown synthetic target")
    validate_root(root)
    return root / name


def read_bytes(target: Path, limit: int = MAX_BYTES) -> bytes | None:
    if not regular_file(target):
        return None
    try:
        with target.open("rb") as source:
            data = source.read(limit + 1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise ValueError("target is too large")
    return data


def fixture_version(root: Path, name: str, *, resolve_target=target_path, limit: int = MAX_BYTES) -> dict:
    data = read_bytes(resolve_target(root, name), limit)
    if data is not None:
        decode_text(data)
    return {"path": name, "version": digest(data) if data is not None else "missing"}


def decode_text(data: bytes) -> str:
    if b"\0" in data:
        raise ValueError("target must be UTF-8 text")
    return data.decode("utf-8")


def propose_edit(root: Path, name: str, version: str, replacement: str, *,
                 resolve_target=target_path, limit: int = MAX_BYTES, scope: str = "fixture", snapshot_version=None) -> dict:
    data = replacement.encode()
    if len(data) > limit or not data or b"\0" in data:
        raise ValueError(f"replacement must contain 1–{limit} UTF-8 bytes without NUL")
    if version != "missing" and not re.fullmatch(r"[0-9a-f]{64}", version):
        raise ValueError("invalid expected version")
    if digest(data) == version:
        raise ValueError("replacement must change the target")
    target = resolve_target(root, name)
    proposal = {"path": name, "version": version, "replacement": replacement}
    if scope != "fixture":
        proposal["scope"] = scope
        proposal["target"] = str(target)
    body = json.dumps(proposal, sort_keys=True)
    proposal_id = digest(body.encode())
    with closing(connect(root)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        observed = (snapshot_version(connection, name) if snapshot_version else
                    fixture_version(root, name, resolve_target=resolve_target, limit=limit)["version"])
        if observed != version:
            raise ValueError("target version changed")
        existing = connection.execute("SELECT state FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if existing:
            return {"proposal_id": proposal_id, "status": existing[0]}
        count = connection.execute(
            "SELECT COUNT(*) FROM proposals WHERE state IN ('pending', 'applying', 'restoring')"
        ).fetchone()[0]
        if count >= 32:
            raise ValueError("synthetic proposal limit reached")
        connection.execute("INSERT INTO proposals VALUES (?, ?, 'pending', NULL)", (proposal_id, body))
    return {"proposal_id": proposal_id, "status": "pending"}


def atomic_write(target: Path, data: bytes, *, create: bool = False, replace=os.replace) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        staged = Path(stream.name)
    try:
        with staged.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create and os.name == "nt":
            os.rename(staged, target)
        elif create:
            os.link(staged, target)
        else:
            replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def local_action(root: Path, command: str, proposal_id: str, approval: str = "", *,
                 resolve_target=target_path, limit: int = MAX_BYTES, scope: str = "fixture", guarded_write=None,
                 block_on_other_intents: bool = True) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", proposal_id):
        raise ValueError("invalid proposal ID")
    with closing(connect(root)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT body, state, approval FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None or digest(row[0].encode()) != proposal_id:
            raise ValueError("proposal missing or changed")
        body, state, approved_digest = row
        proposal = json.loads(body)
        if proposal.get("scope", "fixture") != scope:
            raise ValueError("proposal belongs to another mode")
        target = resolve_target(root, proposal["path"])
        if scope != "fixture" and str(target) != proposal["target"]:
            raise ValueError("proposal target no longer resolves to the approved location")
        display_path = target.name if scope == "fixture" else str(target)
        replacement = proposal["replacement"].encode()
        current = read_bytes(target, limit)
        version = digest(current) if current is not None else "missing"
        backup = root / f"{proposal_id}.bak"
        original_version = proposal["version"]
        revised_version = digest(replacement)
        if command == "show":
            if state != "pending" or version != original_version:
                raise ValueError("proposal already used or target version changed")
            diff = list(difflib.unified_diff(decode_text(current or b"").splitlines(keepends=True),
                                            decode_text(replacement).splitlines(keepends=True),
                                            fromfile=display_path, tofile=display_path))
            local_digest = digest(f"{root}\n{body}\n{secrets.token_hex(32)}".encode())
            connection.execute("UPDATE proposals SET approval=? WHERE id=?", (local_digest, proposal_id))
            return {"path": display_path, "approval_sha256": local_digest, "diff": diff}
        if command == "apply" and (not approval or approval != approved_digest):
            raise ValueError("apply requires the locally reviewed approval digest")
        if block_on_other_intents and connection.execute("SELECT 1 FROM proposals WHERE id<>? AND state IN ('applying', 'restoring')",
                              (proposal_id,)).fetchone():
            raise ValueError("resume the interrupted synthetic operation first")
        if command == "apply" and state in {"pending", "applying"}:
            if state == "pending" and version != original_version:
                raise ValueError("target version changed")
            before, after, output = original_version, revised_version, replacement
            intent, finished = "applying", "applied"
        elif command == "restore" and state in {"applied", "restoring"} and original_version != "missing":
            if state == "applied" and version != revised_version:
                raise ValueError("only unchanged modifications can be restored")
            output = read_bytes(backup, limit)
            if output is None or digest(output) != original_version:
                raise ValueError("backup missing or changed")
            before, after = revised_version, original_version
            intent, finished = "restoring", "restored"
        else:
            raise ValueError("proposal already used or operation unavailable")
        if state != intent:
            connection.execute("UPDATE proposals SET state=? WHERE id=?", (intent, proposal_id))
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT state FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0] != intent:
                raise ValueError("proposal already used")
        target = resolve_target(root, proposal["path"])
        current = read_bytes(target, limit)
        version = digest(current) if current is not None else "missing"
        if version not in {before, after}:
            raise ValueError("target version changed during operation")
        if intent == "applying" and before != "missing":
            original = read_bytes(backup, limit)
            if original is None and current is not None and version == before:
                atomic_write(backup, current, create=True)
            elif original is None or digest(original) != before:
                raise ValueError("backup missing or changed")
        if version == before:
            if guarded_write:
                guarded_write(root, proposal["path"], output, before)
            else:
                target = resolve_target(root, proposal["path"])
                checked = read_bytes(target, limit)
                if (digest(checked) if checked is not None else "missing") != before:
                    raise ValueError("target version changed before writing")
                atomic_write(target, output, create=before == "missing")
        connection.execute("UPDATE proposals SET state=? WHERE id=?", (finished, proposal_id))
        return {"status": finished, "path": display_path}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--workspaces", action="store_true")
    share = commands.add_parser("share")
    share.add_argument("workspace", type=Path)
    share.add_argument("path")
    for name in ("show-batch", "apply-batch", "batch-status", "cancel-batch", "restore-batch-file", "restore-direct"):
        command = commands.add_parser(name)
        command.add_argument("workspace", type=Path)
        command.add_argument("proposal_id")
        if name == "apply-batch":
            command.add_argument("approval")
    for name in ("show", "apply", "restore"):
        command = commands.add_parser(name)
        command.add_argument("workspace", type=Path)
        command.add_argument("proposal_id")
        command.add_argument("--workspaces", action="store_true")
        if name == "apply":
            command.add_argument("approval")
    arguments = parser.parse_args()
    if arguments.command == "init":
        print(initialize(seed_fixture=not arguments.workspaces))
    elif arguments.command == "share":
        from workspace_files import share_workspace_text
        print(json.dumps(share_workspace_text(arguments.workspace, arguments.path), ensure_ascii=True))
    elif arguments.command == "restore-batch-file":
        from live_workspaces import restore_batch_file
        print(json.dumps(restore_batch_file(arguments.workspace, arguments.proposal_id), ensure_ascii=True))
    elif arguments.command == "restore-direct":
        from live_workspaces import restore_direct_backup
        print(json.dumps(restore_direct_backup(arguments.workspace, arguments.proposal_id), ensure_ascii=True))
    elif arguments.command in {"show-batch", "apply-batch", "batch-status", "cancel-batch"}:
        from live_workspaces import batch_action
        print(json.dumps(batch_action(arguments.workspace, arguments.command, arguments.proposal_id,
                                      getattr(arguments, "approval", "")), ensure_ascii=True))
    else:
        settings = {}
        if arguments.workspaces:
            from workspace_files import EDIT_SETTINGS
            settings = EDIT_SETTINGS
        print(json.dumps(local_action(arguments.workspace, arguments.command, arguments.proposal_id,
                                      getattr(arguments, "approval", ""), **settings), ensure_ascii=True))
