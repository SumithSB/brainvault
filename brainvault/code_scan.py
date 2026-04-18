"""
brainvault/code_scan.py — Structural code indexer.

Builds two kinds of knowledge per repo:
  1. code_entities  — file paths with detected language and imports
  2. code_cochange  — file pairs that change together (from git history)

Called via CLI:
    brainvault index-repo [path] [--project <name>] [--min-cochange <n>]

Also called automatically by bootstrap-git after scanning each repo.

Design decisions:
  - Regex import extraction only (no ast) — simpler, good enough for path-level analysis
  - Single git log call for the full cochange matrix (not per-commit diff-tree)
  - Parse errors are silently skipped — indexing must never fail a repo
  - Files > 256 KB are skipped — almost certainly generated/binary
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from brainvault import db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_FILE_BYTES = 256_000  # skip files larger than this

_SKIP_DIRS = frozenset(
    {
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
        "Pods",
        ".venv",
        "venv",
        "env",
        ".tox",
        "__pycache__",
        "miniconda3",
        "anaconda3",
        "miniforge3",
        "mambaforge",
        "micromamba",
        "dist",
        "build",
        "target",
        "out",
        "_build",
        ".next",
        ".nuxt",
        "DerivedData",
        ".idea",
        ".vscode",
        ".git",
        "Library",
        "Applications",
        "System",
        "Volumes",
        "Trash",
        ".cache",
        ".local",
        "tmp",
        "temp",
        ".docker",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__generated__",
        "coverage_html_report",
    }
)

# Extension → language name
_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".dart": "dart",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".rs": "rust",
}

# Import patterns per language — each returns a list of imported path strings
_IMPORT_PATTERNS: dict[str, list[re.Pattern]] = {
    "python": [
        re.compile(r"^\s*import\s+([\w., \t]+)", re.MULTILINE),
        re.compile(r"^\s*from\s+([\w.]+)\s+import", re.MULTILINE),
    ],
    "javascript": [
        re.compile(r"""(?:import|export)\s+.*?from\s+['"]([^'"]+)['"]"""),
        re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
        re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    ],
    "typescript": [],  # reuses javascript patterns — set in __post_init equivalent below
    "go": [
        re.compile(r'"([\w./\-]+)"'),  # matches both single and grouped imports
    ],
    "dart": [
        re.compile(r"""import\s+['"]([^'"]+)['"]"""),
    ],
    "ruby": [
        re.compile(r"""require\s+['"]([^'"]+)['"]"""),
        re.compile(r"""require_relative\s+['"]([^'"]+)['"]"""),
    ],
    "java": [
        re.compile(r"^\s*import\s+([\w.]+);", re.MULTILINE),
    ],
    "kotlin": [
        re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE),
    ],
    "rust": [
        re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE),
    ],
}
# TypeScript uses the same patterns as JavaScript
_IMPORT_PATTERNS["typescript"] = _IMPORT_PATTERNS["javascript"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def index_repo(
    repo_path: Path,
    project: str,
    min_cochange: int = 2,
    verbose: bool = True,
) -> dict:
    """
    Orchestrate full structural indexing of a repository.

    Steps:
      1. Walk file tree → collect file list with language + imports
      2. Build co-change matrix from git history (single git log call)
      3. Bulk-write to DB (replace-all, idempotent)
      4. Update code_index_runs

    Returns stats dict: files_found, cochange_pairs, languages, parse_errors.
    """
    repo_str = str(repo_path)

    if verbose:
        print("  [1/2] Scanning file tree…", end="\r", flush=True)

    files, parse_errors = scan_file_tree(repo_path)

    if verbose:
        lang_summary = ", ".join(
            f"{lang}:{count}"
            for lang, count in sorted(_count_languages(files).items(), key=lambda x: -x[1])
        )
        print(f"  [1/2] {len(files)} files ({lang_summary}){' ' * 10}")
        print("  [2/2] Building co-change matrix…", end="\r", flush=True)

    pairs = build_cochange_matrix(repo_path, min_count=min_cochange)

    if verbose:
        print(f"  [2/2] {len(pairs)} co-change pairs (count ≥ {min_cochange}){' ' * 10}")

    db.index_repo_files(repo_str, project, files)
    db.bulk_record_cochange(
        repo_str, [(p["file_a"], p["file_b"], p["count"], p["last_date"]) for p in pairs]
    )
    db.update_code_index_run(repo_str, project, len(files), len(pairs))

    return {
        "files_found": len(files),
        "cochange_pairs": len(pairs),
        "languages": _count_languages(files),
        "parse_errors": parse_errors,
    }


def scan_file_tree(repo_path: Path) -> tuple[list[dict], int]:
    """
    Walk the repo and return (file_records, parse_error_count).

    Each record: {file_path: str (relative), language: str, imports: list[str]}

    Skips:
      - Directories in _SKIP_DIRS
      - Hidden directories (name starts with '.')
      - Files with unsupported extensions
      - Files larger than _MAX_FILE_BYTES
    """
    records: list[dict] = []
    parse_errors = 0

    for dirpath, dirnames, filenames in _walk(repo_path):
        rel_dir = dirpath.relative_to(repo_path)
        for name in filenames:
            ext = Path(name).suffix.lower()
            language = _LANG_MAP.get(ext)
            if not language:
                continue
            abs_path = dirpath / name
            rel_path = (rel_dir / name).as_posix()
            imports, ok = _extract_imports(abs_path, language)
            if not ok:
                parse_errors += 1
            records.append(
                {
                    "file_path": rel_path,
                    "language": language,
                    "imports": imports,
                }
            )

    return records, parse_errors


def build_cochange_matrix(
    repo_path: Path,
    min_count: int = 2,
) -> list[dict]:
    """
    Return file pairs that changed together at least min_count times.

    Uses a single `git log --name-only` call to read the entire history.
    Only pairs files with supported language extensions to avoid noise from
    lockfiles, configs, and generated assets.

    Returns list of {file_a, file_b, count, last_date} sorted by count desc.
    """
    commit_files = _get_all_commit_files(repo_path)
    if not commit_files:
        return []

    # Count co-occurrences: (file_a, file_b) → (count, latest_date)
    pair_counts: dict[tuple[str, str], list] = defaultdict(lambda: [0, None])

    for date, files in commit_files:
        # Filter to supported language files only
        supported = [f for f in files if _LANG_MAP.get(Path(f).suffix.lower())]
        if len(supported) < 2:
            continue
        for a, b in combinations(sorted(supported), 2):
            key = (min(a, b), max(a, b))
            entry = pair_counts[key]
            entry[0] += 1
            if entry[1] is None or date > entry[1]:
                entry[1] = date

    return sorted(
        [
            {"file_a": a, "file_b": b, "count": v[0], "last_date": v[1]}
            for (a, b), v in pair_counts.items()
            if v[0] >= min_count
        ],
        key=lambda x: -x["count"],
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _walk(root: Path):
    """Yield (dirpath, dirnames, filenames) skipping noise directories."""
    for dirpath, dirnames, filenames in (
        root.walk() if hasattr(root, "walk") else _pathlib_walk(root)
    ):
        # Prune in place so os.walk doesn't descend into skipped dirs
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS]
        yield Path(dirpath), dirnames, filenames


def _pathlib_walk(root: Path):
    """Fallback for Python < 3.12 (Path.walk() added in 3.12)."""
    import os

    yield from os.walk(root)


def _extract_imports(path: Path, language: str) -> tuple[list[str], bool]:
    """
    Read path and extract import strings using regex.
    Returns (imports, success). On any read/parse error returns ([], False).
    """
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return [], True  # skipped for size, not an error
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False

    patterns = _IMPORT_PATTERNS.get(language, [])
    imports: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(source):
            raw = match.group(1).strip()
            # Python `import a, b, c` → split on comma
            if language == "python" and "," in raw:
                imports.extend(p.strip() for p in raw.split(",") if p.strip())
            else:
                imports.append(raw)

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped = []
    for imp in imports:
        if imp not in seen:
            seen.add(imp)
            deduped.append(imp)

    return deduped[:50], True  # cap at 50 imports per file


def _get_all_commit_files(repo_path: Path) -> list[tuple[str, list[str]]]:
    """
    Run a single git log call and return [(iso_date, [file, ...]), ...].

    Uses `--name-only --pretty=format:"%ai"` — one date line followed by
    file lines, separated by blank lines between commits.
    Returns [] on any error.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "log", "--name-only", "--pretty=format:%ai"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    commits: list[tuple[str, list[str]]] = []
    current_date: str | None = None
    current_files: list[str] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            # blank line separates commits
            if current_date is not None and current_files:
                commits.append((current_date, current_files))
            current_date = None
            current_files = []
        elif current_date is None:
            # First non-blank after a separator is the date from --pretty=format:%ai
            current_date = line[:10]  # just the date portion YYYY-MM-DD
        else:
            current_files.append(line)

    # Flush final commit
    if current_date is not None and current_files:
        commits.append((current_date, current_files))

    return commits


def _count_languages(files: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in files:
        lang = f["language"]
        counts[lang] = counts.get(lang, 0) + 1
    return counts
