"""
brainvault/git_scan.py — Mine git history for architectural decision memories.

Called via CLI:
    brainvault git-scan [path] [--project <name>] [--since <date>] [--limit <n>]
    brainvault bootstrap-git [root] [--since <date>] [--limit-per-repo <n>] [--dry-run]

Uses git CLI via subprocess — no gitpython or pygit2 dependency.
All subprocess calls use args lists (never shell=True) to prevent injection.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from brainvault import db

# Directories that are never git repos — skip during discovery
_DISCOVER_SKIP = frozenset(
    {
        # Package managers / deps
        "node_modules",
        "vendor",
        "bower_components",
        ".npm",
        ".yarn",
        ".pnpm-store",
        ".cargo",
        ".rustup",
        ".gem",
        ".bundle",
        ".m2",
        ".gradle",
        ".ivy2",
        "Pods",  # CocoaPods
        # Python envs
        ".venv",
        "venv",
        "env",
        ".env",
        ".tox",
        "__pycache__",
        "miniconda3",
        "anaconda3",
        "miniforge3",
        "mambaforge",
        "micromamba",
        # Build artefacts
        "dist",
        "build",
        "target",
        "out",
        "_build",
        ".next",
        ".nuxt",
        "DerivedData",  # Xcode
        # Editors / IDEs
        ".idea",
        ".vscode",
        # macOS system / Apple
        "Library",
        "Applications",
        "System",
        "Volumes",
        "Trash",
        ".Trash",
        # Cache / temp
        ".cache",
        ".local",
        "tmp",
        "temp",
        # Docker / VMs
        ".docker",
    }
)

# Commit subject keywords that indicate significant architectural work
SIGNIFICANT_KEYWORDS = frozenset(
    {
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
    }
)

# Patterns in commit subjects that indicate noise to skip
NOISE_PATTERNS = frozenset(
    {
        "wip",
        "work in progress",
        "auto-merge",
        "bump",
        "update dependencies",
        "update deps",
        "dependency update",
        "dependabot",
    }
)

# Maps commit subject keywords to memory_type
MEMORY_TYPE_MAP: dict[str, str] = {
    "refactor": "decision",
    "migrate": "decision",
    "replace": "decision",
    "remove": "decision",
    "redesign": "decision",
    "add": "pattern",
    "implement": "pattern",
    "introduce": "pattern",
    "upgrade": "pattern",
    "fix": "note",
}

# ASCII Unit Separator — safe field delimiter that cannot appear in git output
_SEP = "\x1f"


class CommitInfo(TypedDict):
    hash: str  # full 40-char SHA
    short_hash: str  # first 8 chars
    message: str  # subject line (first line of commit message)
    author: str  # "Name <email>"
    date: str  # ISO 8601 string
    is_merge: bool


class CommitStats(TypedDict):
    files_changed: int
    additions: int
    deletions: int
    top_files: list[str]  # up to 5, sorted by (additions + deletions) descending


def _run_git(args: list[str], cwd: Path) -> str:
    """
    Run a git command and return stdout as a stripped string.
    Returns "" on CalledProcessError or FileNotFoundError (git not found).
    Never uses shell=True.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd)] + args,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _resolve_repo_path(path: str | Path) -> Path:
    """
    Validate that path is a git repository and return its resolved absolute Path.
    Raises ValueError with a descriptive message if validation fails.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"Path does not exist: {p}")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {p}")
    if not (p / ".git").exists():
        raise ValueError(f"Path is not a git repository (no .git directory): {p}")
    # Sanity check: git can actually read it
    result = _run_git(["rev-parse", "--git-dir"], cwd=p)
    if not result:
        raise ValueError(f"git cannot read repository at: {p}")
    return p.resolve()


def _get_commits(repo_path: Path, since: datetime, limit: int) -> list[CommitInfo]:
    """
    Return commits from git log, newest first, within the given date range.

    Uses ASCII Unit Separator (\\x1f) as field delimiter — safe against any
    characters that might appear in commit messages, author names, or dates.
    """
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")
    fmt = _SEP.join(["%H", "%s", "%an <%ae>", "%aI", "%P"])
    output = _run_git(
        ["log", f"--format={fmt}", f"--since={since_iso}", f"--max-count={limit}"],
        cwd=repo_path,
    )
    if not output:
        return []

    commits: list[CommitInfo] = []
    for line in output.splitlines():
        parts = line.split(_SEP)
        if len(parts) < 5:
            continue
        full_hash, message, author, date, parents = parts[0], parts[1], parts[2], parts[3], parts[4]
        commits.append(
            CommitInfo(
                hash=full_hash,
                short_hash=full_hash[:8],
                message=message.strip(),
                author=author.strip(),
                date=date.strip(),
                is_merge=len(parents.split()) >= 2,
            )
        )
    return commits


def _get_commit_stats(repo_path: Path, commit_hash: str) -> CommitStats:
    """
    Get diff statistics for a single commit using git diff-tree --numstat.
    Returns zero-valued struct on any error.
    """
    output = _run_git(
        ["diff-tree", "--no-commit-id", "-r", "--numstat", commit_hash],
        cwd=repo_path,
    )
    if not output:
        return CommitStats(files_changed=0, additions=0, deletions=0, top_files=[])

    total_additions = 0
    total_deletions = 0
    file_impacts: list[tuple[int, str]] = []  # (impact, filename)

    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        add_str, del_str, filename = parts
        # Binary files show "-" for both counts
        try:
            additions = int(add_str)
        except ValueError:
            additions = 0
        try:
            deletions = int(del_str)
        except ValueError:
            deletions = 0
        total_additions += additions
        total_deletions += deletions
        file_impacts.append((additions + deletions, filename.strip()))

    file_impacts.sort(key=lambda x: -x[0])
    top_files = [f for _, f in file_impacts[:5]]

    return CommitStats(
        files_changed=len(file_impacts),
        additions=total_additions,
        deletions=total_deletions,
        top_files=top_files,
    )


def _is_noise(message: str) -> bool:
    """Return True if the commit message matches any noise pattern."""
    lower = message.lower()
    if lower.startswith("wip") or "work in progress" in lower:
        return True
    return any(pattern in lower for pattern in NOISE_PATTERNS)


def _is_significant(commit: CommitInfo, stats: CommitStats) -> bool:
    """
    Return True if this commit is worth saving as a memory.
    Exclusions are checked first; a single inclusion is sufficient.
    """
    message_lower = commit["message"].lower()

    # Hard exclusions — noise always wins regardless of other signals
    if _is_noise(commit["message"]):
        return False

    # Inclusions — keyword or merge always wins over trivial-size check
    if commit["is_merge"]:
        return True
    words = message_lower.split()
    if any(w in SIGNIFICANT_KEYWORDS for w in words):
        return True

    # Size-based inclusions — only apply after confirming no keyword
    if stats["files_changed"] > 5:
        return True
    if (stats["additions"] + stats["deletions"]) > 50:
        return True

    # Trivial single-file exclusion — only reached if no keyword and small diff
    if stats["files_changed"] == 1 and (stats["additions"] + stats["deletions"]) <= 10:
        return False

    return False


def _classify_memory_type(commit: CommitInfo) -> str:
    """Map a commit to a memory_type based on its subject keywords."""
    words = commit["message"].lower().split()
    # Check first word first for speed, then scan all words
    if words and words[0] in MEMORY_TYPE_MAP:
        return MEMORY_TYPE_MAP[words[0]]
    for word in words:
        if word in MEMORY_TYPE_MAP:
            return MEMORY_TYPE_MAP[word]
    if commit["is_merge"]:
        return "decision"
    return "note"


def _format_memory_content(commit: CommitInfo, stats: CommitStats) -> str:
    """Produce the rich memory string stored in the DB."""
    lines = [
        f"[git] {commit['short_hash']}: {commit['message']}",
        f"Date: {commit['date'][:10]}",
        f"Author: {commit['author']}",
        f"Changed: {stats['files_changed']} files, +{stats['additions']} -{stats['deletions']} lines",
    ]
    if stats["top_files"]:
        lines.append(f"Files: {', '.join(stats['top_files'][:5])}")
    return "\n".join(lines)


def scan_repo(
    repo_path: Path,
    project: str,
    since: datetime,
    limit: int,
    verbose: bool = True,
) -> dict:
    """
    Mine a git repository's history and save significant commits as memories.

    Returns a stats dict:
        commits_examined, commits_saved, already_scanned, not_significant
    """
    db.init_db()
    resolved = _resolve_repo_path(repo_path)
    repo_key = str(resolved)

    commits = _get_commits(resolved, since=since, limit=limit)

    examined = 0
    saved = 0
    already_scanned = 0
    not_significant = 0
    is_tty = sys.stdout.isatty()

    for i, commit in enumerate(commits):
        examined += 1

        if db.is_commit_scanned(repo_key, commit["hash"]):
            already_scanned += 1
            continue

        stats = _get_commit_stats(resolved, commit["hash"])

        if not _is_significant(commit, stats):
            not_significant += 1
            db.mark_commit_scanned(repo_key, commit["hash"])
            continue

        memory_type = _classify_memory_type(commit)
        content = _format_memory_content(commit, stats)
        db.save_memory(content, memory_type, project=project, source="git")
        db.mark_commit_scanned(repo_key, commit["hash"])
        saved += 1

        if verbose:
            pct = int((i + 1) / len(commits) * 100)
            line = f"  [{pct:3d}%] {commit['short_hash']}: {commit['message'][:60]}"
            if is_tty:
                print(line, end="\r", flush=True)
            else:
                print(line)

    if verbose and commits and is_tty:
        print()  # newline after final \r

    return {
        "commits_examined": examined,
        "commits_saved": saved,
        "already_scanned": already_scanned,
        "not_significant": not_significant,
    }


def discover_repos(
    root: Path,
    max_depth: int = 6,
    progress: bool = True,
) -> list[Path]:
    """
    Walk the directory tree under root and return all git repository paths found.

    A directory is a git repo if it contains a '.git' subdirectory.
    Skips directories in _DISCOVER_SKIP and hidden dirs (starting with '.')
    to avoid traversing noise like node_modules, venvs, and macOS system dirs.

    If progress=True and stdout is a TTY, prints a live counter while scanning.
    Returns paths sorted alphabetically.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []

    repos: list[Path] = []
    dirs_visited = [0]
    is_tty = sys.stdout.isatty()

    def _walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = list(path.iterdir())
        except PermissionError:
            return

        dirs_visited[0] += 1
        if progress and is_tty and dirs_visited[0] % 50 == 0:
            print(
                f"  Scanning… {dirs_visited[0]} dirs checked, {len(repos)} repos found",
                end="\r",
                flush=True,
            )

        # If this directory is a git repo, record it and don't recurse inside
        if (path / ".git").is_dir():
            try:
                result = _run_git(["rev-parse", "--git-dir"], cwd=path)
                if result:
                    repos.append(path)
            except Exception:
                pass
            return

        for child in sorted(children):
            if not child.is_dir():
                continue
            name = child.name
            # Skip hidden dirs and known noise dirs
            if name.startswith(".") or name in _DISCOVER_SKIP:
                continue
            _walk(child, depth + 1)

    _walk(root, 0)

    if progress and is_tty and dirs_visited[0] >= 50:
        print(" " * 60, end="\r")  # clear the progress line

    return sorted(repos)
