"""Windows-only: verify Claude Stop hook command string runs under cmd.exe (quoted python)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows hook quoting smoke")


def test_claude_stop_hook_command_executes(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    db_path = tmp_path / "smoke_memory.db"
    monkeypatch.setattr("brainvault.db.get_db_path", lambda: db_path)
    monkeypatch.setattr("brainvault.installer._seed_vault", lambda: None)

    from brainvault.installer import install

    install(agents=["claude_code"])

    settings = home / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    stop_cmds = [
        h.get("command", "")
        for e in data.get("hooks", {}).get("Stop", [])
        for h in e.get("hooks", [])
        if "brainvault.capture" in h.get("command", "")
    ]
    assert stop_cmds, "expected Stop hook with brainvault.capture"
    cmd = stop_cmds[0]
    env = os.environ.copy()
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    subprocess.run(cmd, shell=True, check=True, env=env, timeout=120)


def test_cursor_hook_commands_execute(tmp_path, monkeypatch):
    from brainvault.adapters.cursor import CursorAdapter

    cursor_home = tmp_path / "cursor-home"
    cursor_home.mkdir(parents=True)
    (cursor_home / "extensions").mkdir()
    (cursor_home / "mcp.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(CursorAdapter, "CURSOR_DIR", cursor_home)
    monkeypatch.setattr(CursorAdapter, "MCP_CONFIG", cursor_home / "mcp.json")
    monkeypatch.setattr(CursorAdapter, "RULES_DIR", cursor_home / "rules")
    monkeypatch.setattr(CursorAdapter, "RULES_FILE", cursor_home / "rules" / "brainvault.mdc")
    monkeypatch.setattr(CursorAdapter, "HOOKS_CONFIG", cursor_home / "hooks.json")
    monkeypatch.setattr(CursorAdapter, "SESSIONS_PATH", cursor_home / "projects")

    db_path = tmp_path / "smoke_memory_cursor.db"
    monkeypatch.setattr("brainvault.db.get_db_path", lambda: db_path)
    monkeypatch.setattr("brainvault.installer._seed_vault", lambda: None)

    from brainvault.installer import install

    install(agents=["cursor"])

    hooks_path = cursor_home / "hooks.json"
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks = data.get("hooks") or {}
    commands: list[str] = []
    for key in ("stop", "postToolUse", "afterFileEdit", "afterShellExecution"):
        for entry in hooks.get(key, []):
            if isinstance(entry, dict) and "brainvault" in entry.get("command", ""):
                commands.append(entry["command"])
    assert len(commands) == 4, f"expected 4 brainvault hook commands, got {commands!r}"

    env = os.environ.copy()

    stop_cmd = next(c for c in commands if "capture" in c)
    subprocess.run(stop_cmd, shell=True, check=True, env=env, timeout=120)

    tool_cmd = next(c for c in commands if "tool_capture" in c)
    payloads = [
        '{"hook_event_name":"postToolUse","conversation_id":"w-smoke-1","tool_name":"Read","tool_input":{},"tool_output":"{}"}',
        '{"hook_event_name":"afterFileEdit","conversation_id":"w-smoke-2","file_path":"C:/tmp/x.txt","edits":[]}',
        '{"hook_event_name":"afterShellExecution","conversation_id":"w-smoke-3","command":"echo ok","duration":1,"output":""}',
    ]
    for body in payloads:
        subprocess.run(
            tool_cmd,
            shell=True,
            check=True,
            env=env,
            timeout=60,
            input=body.encode("utf-8"),
        )
