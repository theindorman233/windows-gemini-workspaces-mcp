import asyncio
import sqlite3
from contextlib import closing

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

import synthetic_edits as edits
from oauth_probe import create_probe


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(edits.tempfile, "tempdir", str(tmp_path))
    return edits.initialize()


def test_mcp_can_propose_but_only_local_approval_changes_and_restores(workspace):
    target = workspace / "sample.txt"
    server = create_probe("fixture-only", edit_workspace=workspace)

    async def submit():
        async with Client(server) as client:
            assert {tool.name for tool in await client.list_tools()} == {"probe", "synthetic_file_version", "propose_edit"}
            version = (await client.call_tool("synthetic_file_version", {"path": target.name})).data["version"]
            return (await client.call_tool("propose_edit", {
                "path": target.name, "version": version, "replacement": "Synthetic revised text.\n",
            })).data["proposal_id"]

    with TestClient(server.http_app(path="/mcp-edit-fixture")) as client:
        assert client.post("/mcp-edit-fixture", json={"method": "tools/call"}).status_code == 401
    proposal = asyncio.run(submit())
    assert target.read_bytes() == edits.INITIAL_TEXT.encode()
    with pytest.raises(ValueError, match="approval digest"):
        edits.local_action(workspace, "apply", proposal, proposal)
    preview = edits.local_action(workspace, "show", proposal)
    assert "+Synthetic revised text.\n" in preview["diff"]
    with pytest.raises(ValueError, match="approval digest"):
        edits.local_action(workspace, "apply", proposal)
    assert edits.local_action(workspace, "apply", proposal, preview["approval_sha256"])["status"] == "applied"
    assert target.read_bytes() == b"Synthetic revised text.\n"
    assert (workspace / f"{proposal}.bak").read_bytes() == edits.INITIAL_TEXT.encode()
    with pytest.raises(ValueError, match="already used"):
        edits.local_action(workspace, "apply", proposal, preview["approval_sha256"])
    assert edits.local_action(workspace, "restore", proposal)["status"] == "restored"
    assert target.read_bytes() == edits.INITIAL_TEXT.encode()


def test_stale_create_preserves_existing_file(workspace):
    first = edits.propose_edit(workspace, "new.txt", "missing", "approved new file")["proposal_id"]
    second = edits.propose_edit(workspace, "new.txt", "missing", "competing new file")["proposal_id"]
    first_digest = edits.local_action(workspace, "show", first)["approval_sha256"]
    second_digest = edits.local_action(workspace, "show", second)["approval_sha256"]
    edits.local_action(workspace, "apply", first, first_digest)
    with pytest.raises(ValueError, match="version changed"):
        edits.local_action(workspace, "apply", second, second_digest)
    assert (workspace / "new.txt").read_bytes() == b"approved new file"


def test_changed_proposal_is_rejected(workspace):
    proposal = edits.propose_edit(workspace, "new.txt", "missing", "approved new file")["proposal_id"]
    with closing(sqlite3.connect(workspace / "proposals.db")) as connection, connection:
        connection.execute("UPDATE proposals SET body='{}' WHERE id=?", (proposal,))
    with pytest.raises(ValueError, match="proposal missing or changed"):
        edits.local_action(workspace, "apply", proposal, proposal)
    assert not (workspace / "new.txt").exists()


def test_path_is_fixed_and_changed_target_blocks_restore(workspace):
    with pytest.raises(ValueError, match="unknown synthetic target"):
        edits.propose_edit(workspace, "../sample.txt", "missing", "text")
    version = edits.fixture_version(workspace, "sample.txt")["version"]
    proposal = edits.propose_edit(workspace, "sample.txt", version, "text")["proposal_id"]
    approval = edits.local_action(workspace, "show", proposal)["approval_sha256"]
    edits.local_action(workspace, "apply", proposal, approval)
    target = workspace / "sample.txt"
    target.write_bytes(b"later change")
    with pytest.raises(ValueError, match="unchanged modifications"):
        edits.local_action(workspace, "restore", proposal)
    assert target.read_bytes() == b"later change"


def test_resume_after_file_changed_before_record_commit(workspace, monkeypatch):
    version = edits.fixture_version(workspace, "sample.txt")["version"]
    proposal = edits.propose_edit(workspace, "sample.txt", version, "synthetic revision")["proposal_id"]
    approval = edits.local_action(workspace, "show", proposal)["approval_sha256"]
    original_writer = edits.atomic_write

    def write_then_fail(target, data, *, create=False):
        original_writer(target, data, create=create)
        if target.name == "sample.txt":
            raise OSError("interrupted after replacement")

    for command, expected in (("apply", b"synthetic revision"), ("restore", edits.INITIAL_TEXT.encode())):
        with monkeypatch.context() as patches:
            patches.setattr(edits, "atomic_write", write_then_fail)
            with pytest.raises(OSError, match="interrupted"):
                edits.local_action(workspace, command, proposal, approval)
        assert (workspace / "sample.txt").read_bytes() == expected
        edits.local_action(workspace, command, proposal, approval)
        assert (workspace / "sample.txt").read_bytes() == expected


def test_terminal_proposals_do_not_consume_pending_quota(workspace):
    with closing(sqlite3.connect(workspace / "proposals.db")) as connection, connection:
        for i in range(40):
            state = ("applied", "restored", "cancelled")[i % 3]
            connection.execute("INSERT INTO proposals VALUES (?, '{}', ?, NULL)", (f"{i:064x}", state))
    version = edits.fixture_version(workspace, "sample.txt")["version"]
    result = edits.propose_edit(workspace, "sample.txt", version, "fresh proposal\n")
    assert result["status"] == "pending"


def test_validate_root_accepts_localappdata_persistent_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    persistent = tmp_path / "WindowsGeminiMCP" / "store"
    persistent.mkdir(parents=True)
    edits.validate_root(persistent)
    unrelated = tmp_path / "OtherApp" / "store"
    unrelated.mkdir(parents=True)
    with pytest.raises(ValueError, match="expected a generated synthetic workspace"):
        edits.validate_root(unrelated)
