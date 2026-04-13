"""
tests/test_git_scan.py — Tests for brainvault/git_scan.py

All subprocess calls are mocked — no real git repository required.
conftest.py autouse fixtures (mock_embeddings, tmp_db) handle DB and embedding isolation.
"""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brainvault import db
from brainvault.git_scan import (
    CommitInfo,
    CommitStats,
    _classify_memory_type,
    _format_memory_content,
    _get_commit_stats,
    _get_commits,
    _is_significant,
    _resolve_repo_path,
    _run_git,
    scan_repo,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_commit(message: str, is_merge: bool = False) -> CommitInfo:
    return CommitInfo(
        hash="a" * 40,
        short_hash="aaaaaaaa",
        message=message,
        author="Alice <a@example.com>",
        date="2024-01-15T10:00:00+00:00",
        is_merge=is_merge,
    )


def _make_stats(files: int = 1, additions: int = 5, deletions: int = 5) -> CommitStats:
    return CommitStats(
        files_changed=files,
        additions=additions,
        deletions=deletions,
        top_files=[],
    )


# ---------------------------------------------------------------------------
# _run_git
# ---------------------------------------------------------------------------


def test_run_git_returns_stdout(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="abc123\n", returncode=0)
        result = _run_git(["log", "--oneline", "-1"], cwd=tmp_path)
    assert result == "abc123"


def test_run_git_returns_empty_on_called_process_error(tmp_path):
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
        result = _run_git(["log"], cwd=tmp_path)
    assert result == ""


def test_run_git_returns_empty_when_git_not_found(tmp_path):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = _run_git(["log"], cwd=tmp_path)
    assert result == ""


def test_run_git_never_uses_shell(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        _run_git(["log"], cwd=tmp_path)
    kwargs = mock_run.call_args[1]
    assert kwargs.get("shell") is not True


# ---------------------------------------------------------------------------
# _resolve_repo_path
# ---------------------------------------------------------------------------


def test_resolve_repo_path_valid(tmp_path):
    (tmp_path / ".git").mkdir()
    with patch("brainvault.git_scan._run_git", return_value=".git"):
        result = _resolve_repo_path(tmp_path)
    assert result == tmp_path.resolve()


def test_resolve_repo_path_nonexistent():
    with pytest.raises(ValueError, match="does not exist"):
        _resolve_repo_path(Path("/nonexistent/path/xyz_brainvault_test"))


def test_resolve_repo_path_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        _resolve_repo_path(f)


def test_resolve_repo_path_no_git_dir(tmp_path):
    with pytest.raises(ValueError, match="not a git repository"):
        _resolve_repo_path(tmp_path)


# ---------------------------------------------------------------------------
# _get_commits
# ---------------------------------------------------------------------------

_FAKE_LOG = (
    "\x1f".join(
        [
            "abc1234500000000000000000000000000000000",
            "refactor auth module",
            "Alice <a@example.com>",
            "2024-01-15T10:00:00+00:00",
            "",  # no parents = not a merge
        ]
    )
    + "\n"
    + "\x1f".join(
        [
            "def5678900000000000000000000000000000000",
            "Merge pull request #42",
            "GitHub <noreply@github.com>",
            "2024-01-10T09:00:00+00:00",
            "parent1 parent2",  # two parents = merge
        ]
    )
)


def test_get_commits_parses_fields(tmp_path):
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with patch("brainvault.git_scan._run_git", return_value=_FAKE_LOG):
        commits = _get_commits(tmp_path, since=since, limit=100)
    assert len(commits) == 2
    assert commits[0]["hash"] == "abc1234500000000000000000000000000000000"
    assert commits[0]["short_hash"] == "abc12345"
    assert commits[0]["message"] == "refactor auth module"
    assert commits[0]["author"] == "Alice <a@example.com>"
    assert commits[0]["is_merge"] is False


def test_get_commits_detects_merge(tmp_path):
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with patch("brainvault.git_scan._run_git", return_value=_FAKE_LOG):
        commits = _get_commits(tmp_path, since=since, limit=100)
    merge = next(c for c in commits if "Merge" in c["message"])
    assert merge["is_merge"] is True


def test_get_commits_empty_repo(tmp_path):
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with patch("brainvault.git_scan._run_git", return_value=""):
        commits = _get_commits(tmp_path, since=since, limit=100)
    assert commits == []


# ---------------------------------------------------------------------------
# _get_commit_stats
# ---------------------------------------------------------------------------

_FAKE_NUMSTAT = "10\t5\tsrc/auth.py\n3\t0\tsrc/models.py\n0\t20\told_file.py\n"


def test_get_commit_stats_totals(tmp_path):
    with patch("brainvault.git_scan._run_git", return_value=_FAKE_NUMSTAT):
        stats = _get_commit_stats(tmp_path, "abc123")
    assert stats["additions"] == 13
    assert stats["deletions"] == 25
    assert stats["files_changed"] == 3


def test_get_commit_stats_top_files_sorted_by_impact(tmp_path):
    with patch("brainvault.git_scan._run_git", return_value=_FAKE_NUMSTAT):
        stats = _get_commit_stats(tmp_path, "abc123")
    # old_file.py: 20, src/auth.py: 15, src/models.py: 3
    assert stats["top_files"][0] == "old_file.py"
    assert stats["top_files"][1] == "src/auth.py"


def test_get_commit_stats_handles_binary_files(tmp_path):
    numstat = "-\t-\timage.png\n5\t2\tsrc/code.py\n"
    with patch("brainvault.git_scan._run_git", return_value=numstat):
        stats = _get_commit_stats(tmp_path, "abc123")
    assert stats["additions"] == 5
    assert stats["deletions"] == 2
    assert stats["files_changed"] == 2


def test_get_commit_stats_returns_zeros_on_error(tmp_path):
    with patch("brainvault.git_scan._run_git", return_value=""):
        stats = _get_commit_stats(tmp_path, "abc123")
    assert stats["files_changed"] == 0
    assert stats["additions"] == 0
    assert stats["top_files"] == []


# ---------------------------------------------------------------------------
# _is_significant
# ---------------------------------------------------------------------------


def test_is_significant_merge_commit():
    c = _make_commit("Merge pull request #42 from feature/auth", is_merge=True)
    assert _is_significant(c, _make_stats()) is True


@pytest.mark.parametrize(
    "keyword",
    [
        "refactor",
        "migrate",
        "add",
        "implement",
        "fix",
        "remove",
        "replace",
        "introduce",
        "redesign",
        "upgrade",
    ],
)
def test_is_significant_keyword(keyword):
    c = _make_commit(f"{keyword} the auth module")
    assert _is_significant(c, _make_stats()) is True


def test_is_significant_many_files():
    c = _make_commit("update various configs")
    assert _is_significant(c, _make_stats(files=6)) is True


def test_is_significant_large_diff():
    c = _make_commit("update logic")
    assert _is_significant(c, _make_stats(additions=30, deletions=30)) is True


def test_is_not_significant_wip():
    c = _make_commit("wip: half-finished auth")
    assert _is_significant(c, _make_stats(files=10, additions=100)) is False


def test_is_not_significant_auto_merge():
    c = _make_commit("auto-merge from main", is_merge=True)
    assert _is_significant(c, _make_stats()) is False


def test_is_not_significant_dependabot():
    c = _make_commit("Bump django from 4.1 to 4.2 (dependabot)")
    assert _is_significant(c, _make_stats()) is False


def test_is_not_significant_trivial_single_file():
    c = _make_commit("update README.md")
    assert _is_significant(c, _make_stats(files=1, additions=5, deletions=3)) is False


# ---------------------------------------------------------------------------
# _classify_memory_type
# ---------------------------------------------------------------------------


def test_classify_refactor_is_decision():
    assert _classify_memory_type(_make_commit("refactor auth module")) == "decision"


def test_classify_add_is_pattern():
    assert _classify_memory_type(_make_commit("add rate limiting middleware")) == "pattern"


def test_classify_fix_is_note():
    assert _classify_memory_type(_make_commit("fix null pointer in login")) == "note"


def test_classify_merge_no_keyword_is_decision():
    assert _classify_memory_type(_make_commit("Merge PR #99", is_merge=True)) == "decision"


def test_classify_unknown_defaults_to_note():
    assert _classify_memory_type(_make_commit("tweak config timeout")) == "note"


def test_classify_keyword_not_first_word():
    # "implement" found in second word position
    assert _classify_memory_type(_make_commit("initial implement approach")) == "pattern"


# ---------------------------------------------------------------------------
# _format_memory_content
# ---------------------------------------------------------------------------


def test_format_memory_content_includes_all_fields():
    c = _make_commit("refactor auth module")
    c["date"] = "2024-01-15T10:00:00+00:00"
    c["short_hash"] = "abc12345"
    stats = CommitStats(
        files_changed=3,
        additions=40,
        deletions=10,
        top_files=["src/auth.py", "src/models.py", "tests/test_auth.py"],
    )
    content = _format_memory_content(c, stats)
    assert "[git] abc12345: refactor auth module" in content
    assert "Date: 2024-01-15" in content
    assert "Changed: 3 files, +40 -10 lines" in content
    assert "src/auth.py" in content


def test_format_memory_content_omits_files_line_when_empty():
    c = _make_commit("fix bug")
    c["date"] = "2024-01-15T10:00:00+00:00"
    c["short_hash"] = "abc12345"
    stats = CommitStats(files_changed=1, additions=3, deletions=1, top_files=[])
    content = _format_memory_content(c, stats)
    assert "Files:" not in content


# ---------------------------------------------------------------------------
# scan_repo integration (mocked git)
# ---------------------------------------------------------------------------

_FAKE_SINGLE_COMMIT_LOG = "\x1f".join(
    [
        "abc1234500000000000000000000000000000000",
        "refactor authentication to use JWT",
        "Alice <a@example.com>",
        "2024-01-15T10:00:00+00:00",
        "",
    ]
)

_FAKE_NUMSTAT_LARGE = "20\t5\tsrc/auth.py\n10\t2\tsrc/models.py\n"


def _make_git_side_effect(log_output, numstat_output):
    def side_effect(args, cwd):
        if "log" in args:
            return log_output
        if "diff-tree" in args:
            return numstat_output
        return ".git"  # rev-parse

    return side_effect


def test_scan_repo_saves_significant_commit(tmp_path):
    (tmp_path / ".git").mkdir()
    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    with patch(
        "brainvault.git_scan._run_git",
        side_effect=_make_git_side_effect(_FAKE_SINGLE_COMMIT_LOG, _FAKE_NUMSTAT_LARGE),
    ):
        stats = scan_repo(tmp_path, project="myproject", since=since, limit=100, verbose=False)

    assert stats["commits_saved"] == 1
    assert stats["commits_examined"] == 1
    memories = db.get_project_memories("myproject")
    assert len(memories) == 1
    assert "[git] abc12345" in memories[0]["content"]
    assert memories[0]["memory_type"] == "decision"
    assert memories[0]["source"] == "git"


def test_scan_repo_is_idempotent(tmp_path):
    (tmp_path / ".git").mkdir()
    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    side_effect = _make_git_side_effect(_FAKE_SINGLE_COMMIT_LOG, _FAKE_NUMSTAT_LARGE)
    with patch("brainvault.git_scan._run_git", side_effect=side_effect):
        first = scan_repo(tmp_path, project="myproject", since=since, limit=100, verbose=False)
    with patch("brainvault.git_scan._run_git", side_effect=side_effect):
        second = scan_repo(tmp_path, project="myproject", since=since, limit=100, verbose=False)

    assert first["commits_saved"] == 1
    assert second["commits_saved"] == 0
    assert second["already_scanned"] == 1
    assert len(db.get_project_memories("myproject")) == 1


def test_scan_repo_filters_wip(tmp_path):
    (tmp_path / ".git").mkdir()
    wip_log = "\x1f".join(
        [
            "abc1234500000000000000000000000000000000",
            "wip: half-done feature",
            "Alice <a@example.com>",
            "2024-01-15T10:00:00+00:00",
            "",
        ]
    )
    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    with patch(
        "brainvault.git_scan._run_git",
        side_effect=_make_git_side_effect(wip_log, "1\t1\tfile.py\n"),
    ):
        stats = scan_repo(tmp_path, project="proj", since=since, limit=100, verbose=False)

    assert stats["commits_saved"] == 0
    assert stats["not_significant"] == 1


def test_scan_repo_raises_on_non_git_dir(tmp_path):
    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="not a git repository"):
        scan_repo(tmp_path, project="proj", since=since, limit=100, verbose=False)


# ---------------------------------------------------------------------------
# db deduplication functions
# ---------------------------------------------------------------------------


def test_is_commit_scanned_initial():
    assert db.is_commit_scanned("/path/to/repo", "abc123") is False


def test_mark_and_check_commit_scanned():
    db.mark_commit_scanned("/path/to/repo", "abc123")
    assert db.is_commit_scanned("/path/to/repo", "abc123") is True


def test_mark_commit_scanned_is_idempotent():
    db.mark_commit_scanned("/path/to/repo", "abc123")
    db.mark_commit_scanned("/path/to/repo", "abc123")  # should not raise
    assert db.is_commit_scanned("/path/to/repo", "abc123") is True


def test_same_hash_different_repo_not_duplicate():
    db.mark_commit_scanned("/repo/a", "abc123")
    assert db.is_commit_scanned("/repo/b", "abc123") is False


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_cmd_git_scan_invalid_limit(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["brainvault", "git-scan", "--limit", "notanumber"])
    from brainvault.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "integer" in captured.out


def test_cmd_git_scan_non_git_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["brainvault", "git-scan", str(tmp_path), "--project", "test"])
    from brainvault.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "not a git repository" in captured.out


def test_cmd_git_scan_runs_successfully(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["brainvault", "git-scan", str(tmp_path), "--project", "testproject", "--limit", "10"],
    )

    def fake_run_git(args, cwd):
        if "rev-parse" in args:
            return ".git"
        return ""  # no commits

    with patch("brainvault.git_scan._run_git", side_effect=fake_run_git):
        from brainvault.cli import main

        main()  # should not raise

    captured = capsys.readouterr()
    assert "testproject" in captured.out
    assert "Done." in captured.out
