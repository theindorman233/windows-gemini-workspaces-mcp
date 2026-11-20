import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

import live_workspaces as live
import synthetic_edits as edits
import workspace_files as workspace
from oauth_probe import create_probe


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(edits.tempfile, "tempdir", str(tmp_path))
    root = tmp_path / "demo"
    root.mkdir()
    monkeypatch.setitem(workspace.WORKSPACES, "demo", root)
    return edits.initialize(seed_fixture=False), root


def test_find_filters_paths_and_decodes_unicode(roots, tmp_path, monkeypatch):
    store, root = roots
    (root / "diagnostics").mkdir()
    paths = [root / "计划[甲].md", root / "diagnostics" / "计划[甲].md",
             tmp_path / "计划[甲].md", root / "计划甲.md"]
    for path in paths:
        path.touch()

    def run(arguments, **options):
        assert "-timeout" not in arguments
        assert arguments[-6:] == ["-path", f"{root}\\", "/a-d", "nopath:", "-regex", r"\x{8ba1}\x{5212}\x{5b}\x{7532}\x{5d}"]
        assert options["timeout"] == 5
        return SimpleNamespace(stdout="\r\n".join(map(str, paths)).encode("utf-8-sig"))

    monkeypatch.setattr(live.subprocess, "run", run)
    assert live.find_files(store, "demo", "计划[甲]") == {"paths": ["demo/计划[甲].md"], "truncated": False}


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
def change(name: str, before: str, after: str) -> dict:
    return {"path": f"demo/{name}", "version": edits.digest(before.encode()),
            "edits": [{"old_text": before, "new_text": after}]}


def test_content_search_reads_current_text_and_reports_limits(roots, monkeypatch):
    if not live.RIPGREP.is_file():
        pytest.skip("ripgrep is not installed in Tools/bin")
    store, root = roots
    folder = root / "章节"
    folder.mkdir()
    (folder / "a.md").write_bytes("标题\r\n证据 [A]\r\n".encode())
    changed = folder / "b.md"
    changed.write_bytes("初稿\r\n结尾\n".encode())
    (folder / ".private.md").write_bytes("私密 [A]\n".encode())
    (root / "outside.md").write_bytes("其他 [A]\n".encode())
    (root / "子目录").mkdir()
    (root / "子目录" / "c.txt").write_bytes("子目录 [C]\n".encode())
    (folder / "link.md").hardlink_to(root / "outside.md")
    expected = {"path": "demo/章节/a.md", "line": 2, "text": "证据 [A]"}

    async def search():
        async with Client(create_probe("fixture-only", workspace_store=store, live=True)) as client:
            result = await client.call_tool("search_workspace_content", {
                "workspace": "demo", "query": "[a]", "directory": "章节",
            })
            return result.data

    assert asyncio.run(search())["hits"] == [expected]
    changed.write_bytes("已更新 [A]\r\n".encode())
    hits = live.search_content(store, "demo", "[A]", "章节")["hits"]
    assert sorted(hits, key=lambda hit: hit["path"]) == [expected, {
        "path": "demo/章节/b.md", "line": 1, "text": "已更新 [A]",
    }]
    assert live.search_content(store, "demo", "[A]", "章节", limit=1)["truncated"]
    assert not live.search_content(store, "demo", "--no-ignore", "章节")["hits"]
    assert live.search_content(store, "demo", "[C]")["hits"] == [
        {"path": "demo/子目录/c.txt", "line": 1, "text": "子目录 [C]"},
    ]
    with pytest.raises(ValueError):
        live.search_content(store, "demo", "[A]", "../other")
    with pytest.raises(ValueError, match="unavailable"):
        live.search_content(store, "demo", "[A]", "missing-dir")
    assert live.scan_content_chunk([("demo/nul.txt", ["prefix\x00[A]"])], "[A]", 5, 2) == [
        {"path": "demo/nul.txt", "line": 1, "text": "prefix\x00[A]"},
    ]
    monkeypatch.setattr(live, "CONTENT_SCAN_LIMIT", 1)
    assert live.search_content(store, "demo", "不存在", "章节")["truncated"]


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
def test_live_tools_and_full_batch_workflow(roots):
    store, root = roots
    first, second = root / "甲.md", root / "乙.md"
    first.write_bytes("初稿\r\n第二行".encode())
    second.write_bytes(b"before")
    server = create_probe("fixture-only", workspace_store=store, live=True)
    with TestClient(server.http_app(path="/mcp-workspaces-live")) as client:
        assert client.post("/mcp-workspaces-live", json={"method": "tools/call"}).status_code == 401

    async def propose():
        async with Client(server) as client:
            assert {tool.name for tool in await client.list_tools()} == {
                "probe", "list_workspaces", "list_workspace_dir", "find_workspace_files", "search_workspace_content",
                "read_workspace_ranges", "query_workspace_git", "search_workspace_ast",
                "edit_workspace_file", "write_workspace_file",
                "run_workspace_command", "read_command_job", "stop_command_job",
            }
            ranges = [{"path": "demo/甲.md", "start_line": 1, "end_line": 2}]
            first_read, = (await client.call_tool("read_workspace_ranges", {"ranges": ranges})).data
            assert first_read["text"] == "初稿\r\n第二行"
            first.write_bytes("实时\r\n第二行".encode())
            second_read, = (await client.call_tool("read_workspace_ranges", {"ranges": ranges})).data
            assert second_read["text"] == "实时\r\n第二行"
            assert first_read["version"] != second_read["version"]
            return live.propose_batch(store, [
                {"path": "demo/甲.md", "version": second_read["version"], "edits": [
                    {"old_text": "实时", "new_text": "定稿"}, {"old_text": "第二行", "new_text": "正文"},
                ]}, change("乙.md", "before", "after"),
            ])["proposal_id"]

    proposal = asyncio.run(propose())
    with pytest.raises(ValueError, match="local batch approval"):
        live.batch_action(store, "apply-batch", proposal, "wrong")
    preview = live.batch_action(store, "show-batch", proposal)
    assert len(preview["files"]) == 2
    assert first.read_bytes() == "实时\r\n第二行".encode()
    second.write_bytes(b"newer")
    with pytest.raises(ValueError, match="file version changed"):
        live.batch_action(store, "apply-batch", proposal, preview["approval_sha256"])
    assert first.read_bytes() == "实时\r\n第二行".encode()
    second.write_bytes(b"before")
    result = live.batch_action(store, "apply-batch", proposal, preview["approval_sha256"])
    assert first.read_bytes() == "定稿\r\n正文".encode()
    assert second.read_bytes() == b"after"
    live.restore_batch_file(store, result["files"][0]["restore_id"])
    assert first.read_bytes() == "实时\r\n第二行".encode()


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
@pytest.mark.parametrize("cancel", [False, True])
def test_batch_resumes_or_cancels_after_interrupted_write(roots, monkeypatch, cancel):
    store, root = roots
    for name in ("first.md", "second.md"):
        (root / name).write_bytes(b"before")
    proposal = live.propose_batch(store, [change(name, "before", "after")
                                         for name in ("first.md", "second.md")])["proposal_id"]
    approval = live.batch_action(store, "show-batch", proposal)["approval_sha256"]
    actual_write = live.LIVE_SETTINGS["guarded_write"]

    def interrupted_write(store, path, data, expected):
        actual_write(store, path, data, expected)
        if path.endswith("second.md"):
            raise OSError("interrupted after writing")

    monkeypatch.setitem(live.LIVE_SETTINGS, "guarded_write", interrupted_write)
    with pytest.raises(ValueError, match="batch stopped"):
        live.batch_action(store, "apply-batch", proposal, approval)
    status = live.batch_action(store, "batch-status", proposal)
    assert [file["status"] for file in status["files"]] == ["applied", "applying"]
    monkeypatch.setitem(live.LIVE_SETTINGS, "guarded_write", actual_write)
    if cancel:
        with pytest.raises(ValueError, match="finish or cancel"):
            live.restore_batch_file(store, status["files"][0]["restore_id"])
        assert live.batch_action(store, "cancel-batch", proposal)["status"] == "cancelled"
        for file in live.batch_action(store, "batch-status", proposal)["files"]:
            assert file["status"] in {"applied", "restore-pending"}
            live.restore_batch_file(store, file["restore_id"])
        assert (root / "first.md").read_bytes() == (root / "second.md").read_bytes() == b"before"
        renewed = live.propose_batch(store, [change(name, "before", "after") for name in ("first.md", "second.md")])
        assert renewed["proposal_id"] != proposal and renewed["status"] == "pending"
    else:
        assert live.batch_action(store, "apply-batch", proposal, approval)["status"] == "applied"
        assert (root / "second.md").read_bytes() == b"after"


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
def test_live_reads_reject_protected_files_and_links(roots):
    store, root = roots
    original = root / "original.md"
    original.write_bytes(b"original")
    (root / "linked.md").hardlink_to(original)
    with pytest.raises(ValueError, match="one link"):
        live.read_live(store, "demo/linked.md")
    with pytest.raises(ValueError, match="protected"):
        live.read_live(store, "demo/.private.md")


def test_git_query_rejects_invalid_requests(roots):
    store, _ = roots
    with pytest.raises(ValueError):
        live.git_query(store, "demo", "clone")
    with pytest.raises(ValueError):
        live.git_query(store, "demo", "log", limit=0)
    with pytest.raises(ValueError):
        live.git_query(store, "demo", "diff")
    with pytest.raises(ValueError):
        live.git_query(store, "demo", "diff", path="work/README.md")
    with pytest.raises(ValueError):
        live.git_query(store, "demo", "status", directory="../other")


@pytest.mark.skipif(os.name != "nt", reason="uses Windows workspace guards")
def test_git_query_reports_status_log_and_diff(roots):
    if not live.GIT.is_file():
        pytest.skip("git is not installed at the configured path")
    store, root = roots

    def git(*arguments):
        import subprocess

        subprocess.run([str(live.GIT), "-C", str(root), *arguments], capture_output=True, check=True)

    git("init")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Synthetic Fixture")
    document = root / "笔记.md"
    document.write_bytes(b"first\n")
    status = live.git_query(store, "demo", "status")
    assert "笔记.md" in status["output"] and not status["truncated"]
    git("add", "--", "笔记.md")
    git("commit", "-m", "synthetic initial commit")
    log = live.git_query(store, "demo", "log")
    assert "synthetic initial commit" in log["output"]
    document.write_bytes(b"first\nsecond\n")
    marker = root / "ext-diff-ran.txt"
    helper = f'"{Path(sys.executable).as_posix()}" -c "open({marker.as_posix()!r}, \'w\').write(\'ran\')"'
    git("config", "diff.external", helper)
    (root / ".gitattributes").write_text("*.md diff=custom\n", encoding="utf-8")
    git("config", "diff.custom.textconv", helper)
    diff = live.git_query(store, "demo", "diff", path="demo/笔记.md")
    assert "+second" in diff["output"] and not marker.exists()
    with pytest.raises(ValueError, match="protected"):
        live.git_query(store, "demo", "diff", path="demo/.git/config")
    with pytest.raises(ValueError, match="diff target is unavailable"):
        live.git_query(store, "demo", "diff", path="demo/missing_dir/笔记.md")
    (root / "linked.md").hardlink_to(document)
    with pytest.raises(ValueError, match="one link"):
        live.git_query(store, "demo", "diff", path="demo/linked.md")
    with pytest.raises(ValueError, match="workspace directory is unavailable"):
        live.git_query(store, "demo", "status", directory="missing")


def test_ast_search_rejects_invalid_requests(roots):
    store, _ = roots
    with pytest.raises(ValueError):
        live.search_ast(store, "demo", "def $F($$$): $$$", "cobol")
    with pytest.raises(ValueError):
        live.search_ast(store, "demo", "", "python")
    with pytest.raises(ValueError):
        live.search_ast(store, "demo", "def $F($$$): $$$", "python", limit=51)
    with pytest.raises(ValueError):
        live.search_ast(store, "demo", "def $F($$$): $$$", "python", directory="../other")


@pytest.mark.skipif(os.name != "nt", reason="uses Windows workspace guards")
def test_ast_search_filters_protected_paths_and_limits(roots):
    if not live.AST_GREP.is_file():
        pytest.skip("ast-grep is not installed in Tools/bin")
    store, root = roots
    folder = root / "src"
    folder.mkdir()
    (folder / "a.py").write_bytes("def alpha(x):\n    return x\n\n\ndef beta(y):\n    return y\n".encode())
    (folder / ".private.py").write_bytes(b"def hidden(z):\n    return z\n")
    (folder / "diagnostics").mkdir()
    (folder / "diagnostics" / "b.py").write_bytes(b"def guarded(z):\n    return z\n")
    (folder / "notes.md").write_bytes(b"def not_python(z):\n")

    result = live.search_ast(store, "demo", "def $F($$$): $$$", "python", "src")
    assert [(hit["path"], hit["line"]) for hit in result["hits"]] == [
        ("demo/src/a.py", 1), ("demo/src/a.py", 5),
    ]
    assert result["hits"][0]["text"].startswith("def alpha")
    assert not result["truncated"] and result["skipped"] == 2
    limited = live.search_ast(store, "demo", "def $F($$$): $$$", "python", "src", limit=1)
    assert len(limited["hits"]) == 1 and limited["truncated"]
    empty = live.search_ast(store, "demo", "class $C: $$$", "python", "src")
    assert empty["hits"] == [] and not empty["truncated"] and empty["skipped"] == 0
    invalid = live.search_ast(store, "demo", "def (((", "python", "src")
    assert invalid["hits"] == [] and "ERROR" in invalid["warning"]

    async def search():
        async with Client(create_probe("fixture-only", workspace_store=store, live=True)) as client:
            result = await client.call_tool("search_workspace_ast", {
                "workspace": "demo", "pattern": "def $F($$$): $$$", "language": "python",
                "directory": "src",
            })
            return result.data

    assert len(asyncio.run(search())["hits"]) == 2




@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
def test_direct_write_edit_backup_restore_and_protections(roots, monkeypatch):
    store, root = roots
    monkeypatch.setitem(workspace.WORKSPACES, "self_mcp", Path(__file__).resolve().parent.parent)
    server = create_probe("fixture-only", workspace_store=store, live=True)

    async def exercise():
        async with Client(server) as client:
            created = (await client.call_tool("write_workspace_file", {
                "path": "demo/嵌套/新建.md", "content": "初版\n第二行\n", "version": "missing",
            })).data
            assert created["ok"] and created["previous_version"] == "missing" and created["backup_id"] is None
            assert (root / "嵌套" / "新建.md").read_text(encoding="utf-8") == "初版\n第二行\n"

            stale_create = (await client.call_tool("write_workspace_file", {
                "path": "demo/嵌套/新建.md", "content": "覆盖", "version": "missing",
            })).data
            assert not stale_create["ok"] and "version changed" in stale_create["error"]

            edited = (await client.call_tool("edit_workspace_file", {
                "path": "demo/嵌套/新建.md",
                "edits": [{"old_text": "初版", "new_text": "修订版"}],
                "version": created["version"],
            })).data
            assert edited["ok"] and edited["backup_id"]
            assert (root / "嵌套" / "新建.md").read_text(encoding="utf-8") == "修订版\n第二行\n"

            overwritten = (await client.call_tool("write_workspace_file", {
                "path": "demo/嵌套/新建.md", "content": "全量改写\n",
            })).data
            assert overwritten["ok"] and overwritten["backup_id"]
            assert (root / "嵌套" / "新建.md").read_text(encoding="utf-8") == "全量改写\n"

            blocked_env = (await client.call_tool("write_workspace_file", {
                "path": "demo/.env", "content": "SECRET=1",
            })).data
            assert not blocked_env["ok"] and "protected" in blocked_env["error"]

            blocked_self = (await client.call_tool("edit_workspace_file", {
                "path": "self_mcp/README.md",
                "edits": [{"old_text": "Windows", "new_text": "Test"}],
            })).data
            assert not blocked_self["ok"] and "not editable" in blocked_self["error"]
            return edited["backup_id"], overwritten["backup_id"]

    edit_bak, write_bak = asyncio.run(exercise())
    with pytest.raises(ValueError, match="changed after direct edit"):
        live.restore_direct_backup(store, edit_bak)
    assert live.restore_direct_backup(store, write_bak)["status"] == "restored"
    assert (root / "嵌套" / "新建.md").read_text(encoding="utf-8") == "修订版\n第二行\n"
    assert live.restore_direct_backup(store, edit_bak)["status"] == "restored"
    assert (root / "嵌套" / "新建.md").read_text(encoding="utf-8") == "初版\n第二行\n"
    with pytest.raises(ValueError, match="already restored"):
        live.restore_direct_backup(store, edit_bak)


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards and junctions")
def test_direct_write_all_workspaces_concurrency_errors_and_cli_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(edits.tempfile, "tempdir", str(tmp_path))
    roots_map = {"work": tmp_path / "work", "demo": tmp_path / "demo", "sandbox": tmp_path / "sandbox"}
    for alias, path in roots_map.items():
        path.mkdir()
        monkeypatch.setitem(workspace.WORKSPACES, alias, path)
    store = edits.initialize(seed_fixture=False)

    for alias in ("work", "demo", "sandbox"):
        res = live.write_live_file(store, f"{alias}/sub/item.txt", f"v1-{alias}")
        assert res["ok"] and (roots_map[alias] / "sub" / "item.txt").read_text(encoding="utf-8") == f"v1-{alias}"

    unchanged = live.write_live_file(store, "work/sub/item.txt", "v1-work")
    assert unchanged["ok"] and unchanged["unchanged"] and unchanged["backup_id"] is None

    for protected in ("demo/.git/config.txt", "work/.ssh/key.txt", "sandbox/diagnostics/log.txt", "work/credentials.json"):
        res = live.write_live_file(store, protected, "x")
        assert not res["ok"] and "protected" in res["error"]

    outside = tmp_path / "outside"
    outside.mkdir()
    junction = roots_map["sandbox"] / "junc"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    junc_res = live.write_live_file(store, "sandbox/junc/escape.txt", "nope")
    assert not junc_res["ok"] and "ordinary directories" in junc_res["error"]

    assert not live.write_live_file(store, "work/bad.txt", "a\x00b")["ok"]
    assert not live.write_live_file(store, "work/big.txt", "x" * (workspace.MAX_TEXT_BYTES + 1))["ok"]
    assert not live.edit_live_file(store, "work/missing.txt", [{"old_text": "a", "new_text": "b"}])["ok"]
    assert not live.edit_live_file(store, "work/sub/item.txt", [], version="")["ok"]
    assert not live.edit_live_file(store, "work/sub/item.txt", [{"old_text": "v1", "new_text": "v2"}] * 13)["ok"]
    assert not live.edit_live_file(store, "work/sub/item.txt", [{"old_text": "v1-work", "new_text": "v2"}], version="0" * 64)["ok"]
    live.write_live_file(store, "work/dup.txt", "dup dup")
    assert not live.edit_live_file(store, "work/dup.txt", [{"old_text": "dup", "new_text": "one"}])["ok"]
    assert not live.edit_live_file(store, "work/dup.txt", [
        {"old_text": "dup dup", "new_text": "x"}, {"old_text": "dup", "new_text": "y"},
    ])["ok"]

    version = live.write_live_file(store, "work/race.txt", "start")["version"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(live.edit_live_file, store, "work/race.txt", [{"old_text": "start", "new_text": "a"}], version)
        f2 = pool.submit(live.edit_live_file, store, "work/race.txt", [{"old_text": "start", "new_text": "b"}], version)
        outcomes = [f1.result(), f2.result()]
    assert sorted(item["ok"] for item in outcomes) == [False, True]
    winner = next(item for item in outcomes if item["ok"])
    loser = next(item for item in outcomes if not item["ok"])
    assert "version changed" in loser["error"]
    assert len(list((store / "direct-backups").glob("*.bak"))) == 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        c1 = pool.submit(live.write_live_file, store, "sandbox/shared_new_dir/one.txt", "1")
        c2 = pool.submit(live.write_live_file, store, "sandbox/shared_new_dir/two.txt", "2")
        assert c1.result()["ok"] and c2.result()["ok"]

    cli_target = roots_map["demo"] / "cli.txt"
    live.write_live_file(store, "demo/cli.txt", "before")
    cli_edit = live.edit_live_file(store, "demo/cli.txt", [{"old_text": "before", "new_text": "after"}])
    assert cli_target.read_text(encoding="utf-8") == "after"
    import runpy
    monkeypatch.setattr(sys, "argv", ["synthetic_edits.py", "restore-direct", str(store), cli_edit["backup_id"]])
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "synthetic_edits.py"), run_name="__main__")
    assert cli_target.read_text(encoding="utf-8") == "before"


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
def test_crlf_edit_write_and_error_messages(roots):
    store, root = roots
    target = root / "crlf.md"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\ndup\r\ndup\r\n")

    not_found = live.edit_live_file(store, "demo/crlf.md", [{"old_text": "missing", "new_text": "x"}])
    assert not not_found["ok"] and "edits[0]: old_text not found" in not_found["error"]

    multi = live.edit_live_file(store, "demo/crlf.md", [
        {"old_text": "alpha", "new_text": "A"},
        {"old_text": "dup", "new_text": "D"},
    ])
    assert not multi["ok"] and "edits[1]: old_text matched 2 times (must be unique)" in multi["error"]

    edited = live.edit_live_file(store, "demo/crlf.md", [{"old_text": "alpha\nbeta", "new_text": "A\nB"}])
    assert edited["ok"]
    assert target.read_bytes() == b"A\r\nB\r\ngamma\r\ndup\r\ndup\r\n"

    overwritten = live.write_live_file(store, "demo/crlf.md", "line1\nline2\n")
    assert overwritten["ok"]
    assert target.read_bytes() == b"line1\r\nline2\r\n"


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
def test_write_live_file_version_conflict_leaves_no_empty_parent_dir(roots):
    store, root = roots
    res = live.write_live_file(store, "demo/ghost_dir/nested/file.md", "hello", version="a" * 64)
    assert not res["ok"]
    assert not (root / "ghost_dir").exists()


def test_normalize_rel_dir_and_directory_tolerance(roots):
    store, root = roots
    sub = root / "src" / "mod"
    sub.mkdir(parents=True)
    (sub / "demo.py").write_bytes(b"def hello():\n    return 1\n")

    assert live.normalize_rel_dir("./src\\mod/") == "src/mod"
    assert live.normalize_rel_dir("/src/mod/") == "src/mod"
    assert live.normalize_rel_dir("./") == ""

    if live.RIPGREP.is_file():
        res = live.search_content(store, "demo", "hello", directory=".\\src\\mod/")
        assert len(res["hits"]) == 1 and res["hits"][0]["path"] == "demo/src/mod/demo.py"
    if live.AST_GREP.is_file():
        ast_res = live.search_ast(store, "demo", "def $F(): $$$", "python", directory="./src\\mod/")
        assert len(ast_res["hits"]) == 1
    listed = live.list_dir("demo", directory=".\\src\\mod/")
    assert listed["directory"] == "src/mod"
    assert any(e["path"] == "demo/src/mod/demo.py" for e in listed["entries"])


@pytest.mark.skipif(os.name != "nt", reason="uses Windows ripgrep and file guards")
def test_find_files_fallback_to_rg_and_search_ignores_venv(roots, tmp_path, monkeypatch):
    if not live.RIPGREP.is_file():
        pytest.skip("ripgrep is not installed in Tools/bin")
    store, root = roots
    (root / "notes.md").write_bytes(b"needle_token\n")
    venv = root / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "notes.md").write_bytes(b"needle_token\n")
    nm = root / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "notes.md").write_bytes(b"needle_token\n")

    monkeypatch.setattr(live, "EVERYTHING", tmp_path / "missing_es.exe")
    found = live.find_files(store, "demo", "notes")
    assert found["paths"] == ["demo/notes.md"]

    searched = live.search_content(store, "demo", "needle_token")
    assert [h["path"] for h in searched["hits"]] == ["demo/notes.md"]


@pytest.mark.skipif(os.name != "nt", reason="uses Windows junctions")
def test_list_dir_traversal_protections_and_limits(roots, tmp_path):
    _, root = roots
    (root / "alpha.md").write_bytes(b"abc")
    sub = root / "sub"
    sub.mkdir()
    (sub / "beta.py").write_bytes(b"12345")
    deep = sub / "deep"
    deep.mkdir()
    (deep / "gamma.txt").write_bytes(b"z")
    for hidden in (".git", ".venv", "node_modules", "__pycache__", "diagnostics"):
        d = root / hidden
        d.mkdir()
        (d / "skip.txt").write_bytes(b"x")
    (root / "credentials.json").write_bytes(b"{}")
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "junc"), str(outside)], check=True, capture_output=True)

    res = live.list_dir("demo", depth=2, limit=50)
    paths = [e["path"] for e in res["entries"]]
    assert "demo/alpha.md" in paths
    assert "demo/sub" in paths
    assert "demo/sub/beta.py" in paths
    assert "demo/sub/deep" in paths
    assert "demo/sub/deep/gamma.txt" not in paths
    for bad in ("demo/.git", "demo/.venv", "demo/node_modules", "demo/__pycache__",
                "demo/diagnostics", "demo/credentials.json", "demo/junc"):
        assert bad not in paths
    assert not res["truncated"]

    limited = live.list_dir("demo", depth=2, limit=2)
    assert len(limited["entries"]) == 2 and limited["truncated"]


@pytest.mark.skipif(os.name != "nt", reason="uses Windows file guards")
def test_direct_backup_pruning_enforces_limit(roots, monkeypatch):
    import time
    store, root = roots
    monkeypatch.setattr(live, "MAX_DIRECT_BACKUPS", 3)
    target = root / "prune.md"
    target.write_bytes(b"v0")
    backup_ids = []
    for i in range(1, 6):
        res = live.write_live_file(store, "demo/prune.md", f"v{i}")
        assert res["ok"] and res["backup_id"]
        backup_ids.append(res["backup_id"])
        time.sleep(0.02)
    backup_dir = store / "direct-backups"
    remaining_json = sorted(p.stem for p in backup_dir.glob("*.json"))
    remaining_bak = sorted(p.stem for p in backup_dir.glob("*.bak"))
    assert len(remaining_json) == 3 and remaining_json == remaining_bak
    assert set(remaining_json) == set(backup_ids[-3:])


@pytest.mark.skipif(os.name != "nt", reason="uses Windows git")
def test_git_query_diff_deleted_unstaged_file_and_sanitizes_stderr(roots):
    if not live.GIT.is_file():
        pytest.skip("git is not installed at the configured path")
    store, root = roots
    def git(*args):
        subprocess.run([str(live.GIT), "-C", str(root), *args], capture_output=True, check=True)
    git("init")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Synthetic Fixture")
    doc = root / "gone.md"
    doc.write_bytes(b"hello deleted\n")
    git("add", "--", "gone.md")
    git("commit", "-m", "add gone.md")
    doc.unlink()
    diff = live.git_query(store, "demo", "diff", path="demo/gone.md")
    assert "-hello deleted" in diff["output"]
