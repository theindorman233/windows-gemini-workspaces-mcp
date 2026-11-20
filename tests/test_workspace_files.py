import asyncio
import os
from contextlib import closing
from pathlib import Path

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

import synthetic_edits as edits
import workspace_files as files
from oauth_probe import create_probe


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(edits.tempfile, "tempdir", str(tmp_path))
    workspace_roots = {"work": tmp_path / "work", "docs": tmp_path / "docs", "sandbox": tmp_path / "sandbox"}
    for root in workspace_roots.values():
        root.mkdir()
    monkeypatch.setattr(files, "WORKSPACES", workspace_roots)
    return edits.initialize(seed_fixture=False), workspace_roots


@pytest.mark.parametrize("path", ["work/../x.txt", "work/.git/config.txt", "docs/../sandbox/x.txt", "sandbox/file.txt:stream",
                                  "work/CON.txt", "sandbox/path\\file.txt", "work/captured_request.txt", "sandbox//x.txt"])
def test_unsafe_paths_are_rejected(roots, path):
    with pytest.raises(ValueError):
        files.workspace_path(roots[0], path)


@pytest.mark.skipif(os.name != "nt", reason="tests Windows path and replacement APIs")
@pytest.mark.parametrize("alias", ["work", "docs", "sandbox"])
def test_workspace_tool_exports_snapshot_then_requires_local_apply(roots, alias):
    store, workspace_roots = roots
    name = f"{alias}/sample.txt"
    target = workspace_roots[alias] / "sample.txt"
    target.write_bytes(b"sample from test")
    server = create_probe("fixture-only", workspace_store=store)
    with pytest.raises(ValueError, match="share command"):
        files.read_workspace_text(store, name)
    with TestClient(server.http_app(path="/mcp-workspaces")) as client:
        assert client.post("/mcp-workspaces", json={"method": "tools/call"}).status_code == 401
    files.share_workspace_text(store, name)

    async def propose():
        async with Client(server) as client:
            snapshot = (await client.call_tool("read_workspace_text", {"path": name})).data
            assert snapshot["text"] == "sample from test"
            return (await client.call_tool("propose_workspace_edit", {
                "path": name, "version": snapshot["version"], "replacement": "next version",
            })).data["proposal_id"]

    proposal = asyncio.run(propose())
    assert target.read_bytes() == b"sample from test"
    preview = edits.local_action(store, "show", proposal, **files.EDIT_SETTINGS)
    assert Path(preview["path"]) == target
    edits.local_action(store, "apply", proposal, preview["approval_sha256"], **files.EDIT_SETTINGS)
    assert target.read_bytes() == b"next version"
    assert files.read_workspace_text(store, name)["text"] == "sample from test"
    edits.local_action(store, "restore", proposal, **files.EDIT_SETTINGS)
    assert target.read_bytes() == b"sample from test"


def test_search_ranges_and_patch_use_shared_snapshots(roots):
    store, workspace_roots = roots
    target = workspace_roots["docs"] / "report.txt"
    original = "First\r\n关键词 Unique\r\n关键词 repeat repeat\r\n"
    target.write_bytes(original.encode())
    (workspace_roots["docs"] / "unshared.txt").write_text("关键词 unshared", encoding="utf-8")
    path = "docs/report.txt"
    with closing(edits.connect(store)) as connection, connection:
        connection.execute("INSERT INTO exports VALUES (?, ?, ?)", (path, str(target), original.encode()))

    async def exercise():
        async with Client(create_probe("fixture-only", workspace_store=store)) as client:
            search = (await client.call_tool("search_workspace", {"query": "关键词", "limit": 1})).data
            assert search["hits"] == [{"path": path, "line": 2, "text": "关键词 Unique"}]
            second = (await client.call_tool("search_workspace", {"query": "关键词", **search["next"]})).data
            assert [hit["line"] for hit in second["hits"]] == [3]
            assert second["next"] is None
            assert files.search_workspace(store, "report", "docs", "path")["hits"] == [{"path": path, "line": 0}]
            assert files.search_workspace(store, "关键词", "work")["hits"] == []
            ranges = await client.call_tool("read_workspace_ranges", {"ranges": [
                {"path": path, "start_line": 2, "end_line": 2},
                {"path": path, "start_line": 4, "end_line": 4},
            ]})
            assert ranges.data[0]["text"] == "关键词 Unique\r\n"
            assert ranges.data[1]["end_line"] is None
            version = ranges.data[0]["version"]
            result = await client.call_tool("propose_workspace_patch", {
                "path": path, "version": version, "old_text": "Unique", "new_text": "Revised",
            })
            assert result.data["status"] == "pending"
            with pytest.raises(ValueError, match="exactly once"):
                files.propose_workspace_patch(store, path, version, "repeat", "different")
            with pytest.raises(ValueError, match="version changed"):
                files.propose_workspace_patch(store, path, "0" * 64, "Unique", "different")
            return result.data["proposal_id"]

    proposal = asyncio.run(exercise())
    assert target.read_bytes() == original.encode()
    preview = edits.local_action(store, "show", proposal, **files.EDIT_SETTINGS)
    assert "+关键词 Revised\r\n" in preview["diff"]
    edits.local_action(store, "apply", proposal, preview["approval_sha256"], **files.EDIT_SETTINGS)
    assert target.read_bytes() == original.replace("Unique", "Revised").encode()
    edits.local_action(store, "restore", proposal, **files.EDIT_SETTINGS)
    assert target.read_bytes() == original.encode()


def test_empty_search_page_can_resume(roots, monkeypatch):
    store, workspace_roots = roots
    monkeypatch.setattr(files, "SEARCH_SCAN_LIMIT", 2)
    with closing(edits.connect(store)) as connection, connection:
        for name, content in (("a.txt", b"nothing"), ("b.txt", b"nothing"), ("c.txt", b"needle")):
            connection.execute("INSERT INTO exports VALUES (?, ?, ?)",
                               (f"work/{name}", str(workspace_roots["work"] / name), content))
    first = files.search_workspace(store, "needle", "work")
    assert first == {"hits": [], "next": {"after_path": "work/b.txt", "after_line": 0}}
    assert files.search_workspace(store, "needle", "work", **first["next"])["hits"] == [
        {"path": "work/c.txt", "line": 1, "text": "needle"},
    ]


def test_range_continuation_preserves_long_lines(roots):
    store, workspace_roots = roots
    path = "work/long.txt"
    text = "x" * (files.MAX_SNIPPET_CHARS + 27) + "\r\ntail\r\n"
    with closing(edits.connect(store)) as connection, connection:
        connection.execute("INSERT INTO exports VALUES (?, ?, ?)",
                           (path, str(workspace_roots["work"] / "long.txt"), text.encode()))
    first, = files.read_workspace_ranges(store, [{"path": path, "start_line": 1, "end_line": 2}])
    assert first["end_line"] == 1
    assert first["next"]["start_column"] == files.MAX_SNIPPET_CHARS + 1
    second, = files.read_workspace_ranges(store, [first["next"]])
    assert first["text"] + second["text"] == text
    assert second["next"] is None


def test_propose_patch_crlf_tolerance_and_clear_errors(roots):
    store, workspace_roots = roots
    target = workspace_roots["docs"] / "crlf.txt"
    original = "line1\r\nline2\r\nline3\r\ndup\r\ndup\r\n"
    target.write_bytes(original.encode())
    path = "docs/crlf.txt"
    with closing(edits.connect(store)) as connection, connection:
        connection.execute("INSERT INTO exports VALUES (?, ?, ?)", (path, str(target), original.encode()))
    version = edits.digest(original.encode())

    with pytest.raises(ValueError, match=r"edits\[0\]: old_text not found"):
        files.propose_workspace_patch(store, path, version, "nonexistent", "x")
    with pytest.raises(ValueError, match=r"edits\[0\]: old_text matched 2 times \(must be unique"):
        files.propose_workspace_patch(store, path, version, "dup", "x")

    proposal = files.propose_workspace_patch(store, path, version, "line1\nline2", "L1\nL2")["proposal_id"]
    preview = edits.local_action(store, "show", proposal, **files.EDIT_SETTINGS)
    edits.local_action(store, "apply", proposal, preview["approval_sha256"], **files.EDIT_SETTINGS)
    assert target.read_bytes() == b"L1\r\nL2\r\nline3\r\ndup\r\ndup\r\n"


def test_read_workspace_ranges_live_reader_skips_sqlite(roots, monkeypatch):
    store, _ = roots
    def fail_connect(_store):
        raise AssertionError("connect should not be called when reader is provided")
    monkeypatch.setattr(files, "connect", fail_connect)
    result = files.read_workspace_ranges(
        store,
        [{"path": "work/demo.txt", "start_line": 1, "end_line": 2}],
        reader=lambda _s, _p: b"alpha\nbeta\n",
    )
    assert result[0]["text"] == "alpha\nbeta\n"
    assert result[0]["total_lines"] == 2
