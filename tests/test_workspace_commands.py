import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import synthetic_edits as edits
import workspace_commands as cmds
import workspace_files as workspace
from oauth_probe import create_probe


@pytest.fixture
def cmd_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(edits.tempfile, "tempdir", str(tmp_path))
    root = tmp_path / "demo"
    root.mkdir()
    monkeypatch.setitem(workspace.WORKSPACES, "demo", root)
    return edits.initialize(seed_fixture=False), root


@pytest.mark.skipif(os.name != "nt", reason="uses Windows pwsh command runner")
def test_command_sync_exit_code_unicode_env_and_errors(cmd_roots, tmp_path, monkeypatch):
    if not cmds.PWSH.is_file():
        pytest.skip("pwsh is not installed")
    store, root = cmd_roots
    sub = root / "子目录"
    sub.mkdir()
    py = Path(sys.executable).as_posix()
    monkeypatch.setenv("MCP_PROBE_SECRET_TEST", "should-not-leak")

    ok = cmds.run_workspace_command(
        store, "demo",
        f'& "{py}" -c "import os; print(\'中文输出-ok\', os.environ.get(\'MCP_PROBE_SECRET_TEST\'))"',
        directory="子目录",
    )
    assert ok["ok"] and ok["status"] == "completed" and ok["returncode"] == 0
    assert "中文输出-ok None" in ok["stdout"]
    assert Path(ok["cwd"]) == sub

    fail_native = cmds.run_workspace_command(store, "demo", f'& "{py}" -c "import sys; sys.exit(3)"')
    assert not fail_native["ok"] and fail_native["status"] == "completed" and fail_native["returncode"] == 3

    fail_cmdlet = cmds.run_workspace_command(store, "demo", "Get-Item 'Z:\\definitely_missing_xyz'")
    assert not fail_cmdlet["ok"] and fail_cmdlet["returncode"] == 1
    assert "CLIXML" not in fail_cmdlet["stderr"]

    backtick = cmds.run_workspace_command(store, "demo", "Write-Output hi `")
    assert not backtick["ok"] and backtick["returncode"] == 1

    surrogate = cmds.run_workspace_command(store, "demo", "Write-Output \ud800")
    assert not surrogate["ok"] and "surrogates" in surrogate["error"]

    outside = tmp_path / "outside"
    outside.mkdir()
    junction = root / "junc"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    bad_junc = cmds.run_workspace_command(store, "demo", "Write-Output hi", directory="junc")
    assert not bad_junc["ok"] and "ordinary directories" in bad_junc["error"]

    assert not cmds.run_workspace_command(store, "demo", "Write-Output hi", directory=".git")["ok"]
    assert not cmds.run_workspace_command(store, "demo", "Write-Output hi", directory="missing")["ok"]
    assert not cmds.run_workspace_command(store, "demo", "Write-Output hi", timeout_seconds=True)["ok"]
    assert not cmds.read_command_job("unknown-job")["ok"]
    assert not cmds.read_command_job("unknown-job", wait_seconds=21)["ok"]
    assert not cmds.read_command_job("unknown-job", wait_seconds=True)["ok"]
    assert not cmds.stop_command_job("unknown-job")["ok"]


def test_stream_capture_handles_utf8_boundaries_and_large_tail():
    cap = cmds.StreamCapture()
    prefix = b"a" * (cmds.MAX_CAPTURE_CHARS * 2)
    tail_text = "结尾汉字-" * 20
    payload = prefix + tail_text.encode("utf-8")
    for idx in range(0, len(payload), 3):
        cap.append(payload[idx:idx + 3])
    cap.finish()
    rendered, truncated = cap.text()
    assert truncated and "\ufffd" not in rendered and rendered.endswith(tail_text)


def test_completing_snapshot_and_retained_job_pruning(tmp_path, monkeypatch):
    from types import SimpleNamespace

    dummy_proc = SimpleNamespace(pid=999999)
    completing = cmds.CommandJob(job_id="cmd-completing", cwd=tmp_path, process=dummy_proc, timeout_seconds=10,
                                 status="completing", returncode=0)
    snap = cmds._snapshot_job(completing)
    assert snap["ok"] and snap["status"] == "running"

    monkeypatch.setattr(cmds, "MAX_RETAINED_JOBS", 2)
    with cmds._JOBS_LOCK:
        saved = dict(cmds._JOBS)
        cmds._JOBS.clear()
        try:
            for idx in range(3):
                j = cmds.CommandJob(job_id=f"j-{idx}", cwd=tmp_path, process=dummy_proc, timeout_seconds=10, status="completed")
                j.done.set()
                cmds._prune_finished_jobs_locked()
                cmds._JOBS[j.job_id] = j
            assert list(cmds._JOBS.keys()) == ["j-1", "j-2"]
        finally:
            cmds._JOBS.clear()
            cmds._JOBS.update(saved)


@pytest.mark.skipif(os.name != "nt", reason="uses Windows pwsh command runner")
def test_command_timeout_auto_background_caps_and_mcp_strict_types(cmd_roots, monkeypatch):
    if not cmds.PWSH.is_file():
        pytest.skip("pwsh is not installed")
    store, root = cmd_roots
    py = Path(sys.executable).as_posix()

    marker = root / "grandchild.txt"
    grand_script = root / "grand.py"
    grand_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        f'subprocess.Popen([sys.executable, "-c", "import pathlib, time; time.sleep(2); pathlib.Path({marker.as_posix()!r}).write_text(str(1))"])\n'
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    timed = cmds.run_workspace_command(store, "demo", f'& "{py}" "{grand_script.as_posix()}"', timeout_seconds=1)
    assert not timed["ok"] and timed["status"] == "timed_out"
    time.sleep(2.3)
    assert not marker.exists()

    monkeypatch.setattr(cmds, "SYNC_WAIT_CAP_SECONDS", 1)
    promoted = cmds.run_workspace_command(
        store, "demo", f'& "{py}" -c "import time; time.sleep(2); print(\'auto-done\')"',
        timeout_seconds=30, background=False,
    )
    assert promoted["ok"] and promoted["status"] == "running"
    finished = cmds.read_command_job(promoted["job_id"], wait_seconds=10)
    assert finished["ok"] and finished["status"] == "completed" and "auto-done" in finished["stdout"]
    already_done = cmds.stop_command_job(promoted["job_id"])
    assert already_done["status"] == "completed"

    monkeypatch.setattr(cmds, "MAX_RUNNING_JOBS", 2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                cmds.run_workspace_command, store, "demo",
                f'& "{py}" -c "import time; time.sleep(4)"', background=True, timeout_seconds=15,
            )
            for _ in range(4)
        ]
        results = [f.result() for f in futures]
    started = [r for r in results if r["ok"]]
    rejected = [r for r in results if not r["ok"]]
    assert len(started) == 2 and len(rejected) == 2
    for r in started:
        cmds.stop_command_job(r["job_id"])

    server = create_probe("fixture-only", workspace_store=store, live=True)

    async def exercise_mcp():
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool("run_workspace_command", {
                    "workspace": "demo", "command": "Write-Output hi", "timeout_seconds": True,
                })
            with pytest.raises(ToolError):
                await client.call_tool("find_workspace_files", {
                    "workspace": "demo", "query": "a", "limit": True,
                })
            bg = (await client.call_tool("run_workspace_command", {
                "workspace": "demo",
                "command": f'& "{py}" -c "import time; print(\'bg-ready\', flush=True); time.sleep(10)"',
                "background": True, "timeout_seconds": 30,
            })).data
            assert bg["ok"] and bg["status"] == "running"
            polled = (await client.call_tool("read_command_job", {"job_id": bg["job_id"], "wait_seconds": 1})).data
            assert polled["job_id"] == bg["job_id"]
            stopped = (await client.call_tool("stop_command_job", {"job_id": bg["job_id"]})).data
            assert not stopped["ok"] and stopped["status"] == "stopped"
            return stopped

    assert "bg-ready" in asyncio.run(exercise_mcp())["stdout"]


def test_stream_capture_ansi_stripping_and_head_tail_truncation():
    cap = cmds.StreamCapture()
    raw = (
        "\x1b[31mERR_HEADER\x1b[0m "
        "\x1b]0;Window Title\x07"
        "\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\"
        "\x1b[?25lOK"
    )
    cap.append(raw.encode("utf-8"))
    cap.finish()
    rendered, truncated = cap.text()
    assert not truncated
    assert rendered == "ERR_HEADER linkOK"
    assert "\x1b" not in rendered

    mid_cap = cmds.StreamCapture()
    head_marker = "HEADER_START_"
    tail_marker = "_FOOTER_END"
    mid_payload = head_marker + ("m" * 10000) + tail_marker
    for idx in range(0, len(mid_payload), 4096):
        mid_cap.append(mid_payload[idx:idx + 4096].encode("utf-8"))
    mid_cap.finish()
    mid_rendered, mid_trunc = mid_cap.text()
    expected_dropped = len(mid_payload) - cmds.HEAD_RETURN_CHARS - cmds.TAIL_RETURN_CHARS
    assert mid_trunc
    assert mid_rendered.startswith(head_marker)
    assert mid_rendered.endswith(tail_marker)
    assert f"\n...[truncated {expected_dropped} chars]...\n" in mid_rendered

    big_cap = cmds.StreamCapture()
    big_payload = head_marker + ("x" * (cmds.MAX_CAPTURE_CHARS * 2)) + tail_marker
    for idx in range(0, len(big_payload), 4096):
        big_cap.append(big_payload[idx:idx + 4096].encode("utf-8"))
    big_cap.finish()
    big_rendered, big_trunc = big_cap.text()
    assert big_trunc
    assert big_rendered.startswith(head_marker)
    assert big_rendered.endswith(tail_marker)
    assert "\n...[truncated " in big_rendered and " chars]...\n" in big_rendered


@pytest.mark.skipif(os.name != "nt", reason="uses Windows pwsh command runner")
def test_ansi_output_and_sensitive_env_vars_filtered(cmd_roots, monkeypatch):
    if not cmds.PWSH.is_file():
        pytest.skip("pwsh is not installed")
    store, _ = cmd_roots
    py = Path(sys.executable).as_posix()

    monkeypatch.setenv("FAKE_API_TOKEN", "tok-123")
    monkeypatch.setenv("my_secret_value", "sec-456")
    monkeypatch.setenv("CUSTOM_API_KEY", "key-789")
    monkeypatch.setenv("db_password_prod", "pwd-000")
    monkeypatch.setenv("SAFE_CUSTOM_FLAG", "keep-me")

    env_script = (
        "import json, os; "
        "keys = ['FAKE_API_TOKEN', 'my_secret_value', 'MY_SECRET_VALUE', 'CUSTOM_API_KEY', "
        "'db_password_prod', 'DB_PASSWORD_PROD', 'SAFE_CUSTOM_FLAG', "
        "'NO_COLOR', 'CLICOLOR', 'FORCE_COLOR', 'TERM']; "
        "print(json.dumps({k: os.environ.get(k) for k in keys}))"
    )
    res = cmds.run_workspace_command(store, "demo", f'& "{py}" -c "{env_script}"')
    assert res["ok"] and res["returncode"] == 0
    import json
    env_data = json.loads(res["stdout"].strip())
    assert env_data["FAKE_API_TOKEN"] is None
    assert env_data["my_secret_value"] is None and env_data["MY_SECRET_VALUE"] is None
    assert env_data["CUSTOM_API_KEY"] is None
    assert env_data["db_password_prod"] is None and env_data["DB_PASSWORD_PROD"] is None
    assert env_data["SAFE_CUSTOM_FLAG"] == "keep-me"
    assert env_data["NO_COLOR"] == "1"
    assert env_data["CLICOLOR"] == "0"
    assert env_data["FORCE_COLOR"] == "0"
    assert env_data["TERM"] == "dumb"

    ansi_res = cmds.run_workspace_command(
        store,
        "demo",
        f'& "{py}" -c "import sys; print(\'\\x1b[32mGREEN_OK\\x1b[0m\\x1b]0;t\\x07\'); '
        f'sys.stderr.write(\'\\x1b[31mRED_ERR\\x1b[0m\\n\')"',
    )
    assert ansi_res["ok"]
    assert "GREEN_OK" in ansi_res["stdout"] and "\x1b" not in ansi_res["stdout"]
    assert "RED_ERR" in ansi_res["stderr"] and "\x1b" not in ansi_res["stderr"]

    psstyle_res = cmds.run_workspace_command(store, "demo", "if ($PSStyle) { $PSStyle.OutputRendering } else { 'PlainText' }")
    assert psstyle_res["ok"] and "PlainText" in psstyle_res["stdout"]


@pytest.mark.skipif(os.name != "nt", reason="uses Windows Job Object cleanup")
def test_orphan_grandchild_killed_by_job_object_and_graceful_degradation(cmd_roots, monkeypatch):
    if not cmds.PWSH.is_file():
        pytest.skip("pwsh is not installed")
    store, root = cmd_roots
    py = Path(sys.executable).as_posix()

    marker = root / "orphan_survived.txt"
    parent_script = root / "spawn_orphan.py"
    parent_script.write_text(
        "import pathlib, subprocess, sys\n"
        f"child_code = {f'import pathlib, time; time.sleep(1.5); pathlib.Path({marker.as_posix()!r}).write_text(str(1))'!r}\n"
        "p = subprocess.Popen(\n"
        "    [sys.executable, '-c', child_code],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "print(f'SPAWNED:{p.pid}')\n",
        encoding="utf-8",
    )
    res = cmds.run_workspace_command(store, "demo", f'& "{py}" "{parent_script.as_posix()}"', timeout_seconds=10)
    assert res["ok"] and res["status"] == "completed" and res["returncode"] == 0
    assert "SPAWNED:" in res["stdout"]
    time.sleep(2.0)
    assert not marker.exists(), "orphan grandchild should have been terminated when Job Object closed"

    # Verify graceful degradation if AssignProcessToJobObject fails
    monkeypatch.setattr(cmds._kernel32, "AssignProcessToJobObject", lambda h_job, h_proc: 0)
    from types import SimpleNamespace
    assert cmds._create_and_assign_job_object(SimpleNamespace(_handle=1234)) is None
