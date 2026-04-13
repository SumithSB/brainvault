"""
tests/test_code_scan.py — Tests for code_scan.py (file tree, import extraction, cochange).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from brainvault import code_scan, db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# scan_file_tree
# ---------------------------------------------------------------------------


class TestScanFileTree:
    def test_detects_python_files(self, tmp_path):
        _write(tmp_path, "app.py", "import os\n")
        files, errors = code_scan.scan_file_tree(tmp_path)
        assert any(f["file_path"] == "app.py" for f in files)
        assert all(f["language"] == "python" for f in files)
        assert errors == 0

    def test_detects_typescript_files(self, tmp_path):
        _write(tmp_path, "index.ts", "import React from 'react'\n")
        files, _ = code_scan.scan_file_tree(tmp_path)
        assert any(f["file_path"] == "index.ts" and f["language"] == "typescript" for f in files)

    def test_skips_unsupported_extensions(self, tmp_path):
        _write(tmp_path, "README.md", "# Hello")
        _write(tmp_path, "package.json", "{}")
        _write(tmp_path, "app.py", "x = 1")
        files, _ = code_scan.scan_file_tree(tmp_path)
        paths = [f["file_path"] for f in files]
        assert "README.md" not in paths
        assert "package.json" not in paths
        assert "app.py" in paths

    def test_skips_node_modules(self, tmp_path):
        _write(tmp_path, "node_modules/lib/index.js", "module.exports = {}")
        _write(tmp_path, "src/app.js", "const x = 1")
        files, _ = code_scan.scan_file_tree(tmp_path)
        paths = [f["file_path"] for f in files]
        assert not any("node_modules" in p for p in paths)
        assert any("app.js" in p for p in paths)

    def test_skips_hidden_directories(self, tmp_path):
        _write(tmp_path, ".vscode/settings.py", "x = 1")
        _write(tmp_path, "src/main.py", "x = 1")
        files, _ = code_scan.scan_file_tree(tmp_path)
        paths = [f["file_path"] for f in files]
        assert not any(".vscode" in p for p in paths)
        assert "src/main.py" in paths

    def test_skips_large_files(self, tmp_path):
        large = tmp_path / "big.py"
        large.write_bytes(b"x = 1\n" * 60_000)  # > 256KB
        files, errors = code_scan.scan_file_tree(tmp_path)
        # Large file still appears in results but with empty imports (size skip is not an error)
        p = next((f for f in files if f["file_path"] == "big.py"), None)
        assert p is not None
        assert p["imports"] == []
        assert errors == 0  # size skip is not a parse error

    def test_nested_paths_are_relative(self, tmp_path):
        _write(tmp_path, "src/auth/jwt.py", "import hmac")
        files, _ = code_scan.scan_file_tree(tmp_path)
        paths = [f["file_path"] for f in files]
        assert "src/auth/jwt.py" in paths
        # Must be relative, not absolute
        assert not any(p.startswith("/") for p in paths)

    def test_multiple_languages_detected(self, tmp_path):
        _write(tmp_path, "main.py", "import os")
        _write(tmp_path, "app.ts", "import React from 'react'")
        _write(tmp_path, "server.go", 'import "fmt"')
        files, _ = code_scan.scan_file_tree(tmp_path)
        langs = {f["language"] for f in files}
        assert langs == {"python", "typescript", "go"}


# ---------------------------------------------------------------------------
# Import extraction — Python
# ---------------------------------------------------------------------------


class TestPythonImports:
    def _extract(self, source: str) -> list[str]:
        tmp = Path("/tmp/_test_brainvault_py.py")
        tmp.write_text(source, encoding="utf-8")
        imports, _ = code_scan._extract_imports(tmp, "python")
        return imports

    def test_simple_import(self):
        assert "os" in self._extract("import os\n")

    def test_from_import(self):
        assert "pathlib" in self._extract("from pathlib import Path\n")

    def test_multi_import(self):
        imports = self._extract("import os, sys, json\n")
        assert "os" in imports
        assert "sys" in imports
        assert "json" in imports

    def test_relative_import(self):
        assert ".utils" in self._extract("from .utils import helper\n")

    def test_deduplication(self):
        imports = self._extract("import os\nimport os\n")
        assert imports.count("os") == 1


# ---------------------------------------------------------------------------
# Import extraction — JavaScript / TypeScript
# ---------------------------------------------------------------------------


class TestJavaScriptImports:
    def _extract(self, source: str, lang: str = "javascript") -> list[str]:
        tmp = Path(f"/tmp/_test_brainvault.{lang[:2]}")
        tmp.write_text(source, encoding="utf-8")
        imports, _ = code_scan._extract_imports(tmp, lang)
        return imports

    def test_es_module_import(self):
        assert "react" in self._extract("import React from 'react'\n")

    def test_require(self):
        assert "./utils" in self._extract("const x = require('./utils')\n")

    def test_dynamic_import(self):
        assert "./chunk" in self._extract("import('./chunk')\n")

    def test_typescript_import(self):
        assert "./types" in self._extract("import type { Foo } from './types'\n", "typescript")

    def test_named_import(self):
        assert "lodash" in self._extract("import { map, filter } from 'lodash'\n")


# ---------------------------------------------------------------------------
# Import extraction — Go
# ---------------------------------------------------------------------------


class TestGoImports:
    def _extract(self, source: str) -> list[str]:
        tmp = Path("/tmp/_test_brainvault.go")
        tmp.write_text(source, encoding="utf-8")
        imports, _ = code_scan._extract_imports(tmp, "go")
        return imports

    def test_single_import(self):
        assert "fmt" in self._extract('import "fmt"\n')

    def test_grouped_import(self):
        source = 'import (\n    "fmt"\n    "os"\n)\n'
        imports = self._extract(source)
        assert "fmt" in imports
        assert "os" in imports


# ---------------------------------------------------------------------------
# Import extraction — Dart
# ---------------------------------------------------------------------------


class TestDartImports:
    def _extract(self, source: str) -> list[str]:
        tmp = Path("/tmp/_test_brainvault.dart")
        tmp.write_text(source, encoding="utf-8")
        imports, _ = code_scan._extract_imports(tmp, "dart")
        return imports

    def test_package_import(self):
        imports = self._extract("import 'package:flutter/material.dart';\n")
        assert "package:flutter/material.dart" in imports

    def test_relative_import(self):
        imports = self._extract("import '../auth/service.dart';\n")
        assert "../auth/service.dart" in imports


# ---------------------------------------------------------------------------
# build_cochange_matrix
# ---------------------------------------------------------------------------


class TestBuildCochangeMatrix:
    def _mock_commits(self, commits):
        """Patch _get_all_commit_files to return a fixed list."""
        return patch.object(code_scan, "_get_all_commit_files", return_value=commits)

    def test_basic_pair_counted(self, tmp_path):
        commits = [
            ("2024-01-01", ["auth.py", "test_auth.py"]),
            ("2024-01-02", ["auth.py", "test_auth.py"]),
        ]
        with self._mock_commits(commits):
            pairs = code_scan.build_cochange_matrix(tmp_path, min_count=2)
        assert len(pairs) == 1
        assert pairs[0]["count"] == 2

    def test_canonical_order(self, tmp_path):
        commits = [("2024-01-01", ["z_file.py", "a_file.py"])] * 3
        with self._mock_commits(commits):
            pairs = code_scan.build_cochange_matrix(tmp_path, min_count=1)
        assert pairs[0]["file_a"] < pairs[0]["file_b"]

    def test_min_count_filter(self, tmp_path):
        commits = [("2024-01-01", ["auth.py", "test_auth.py"])]  # only 1 co-occurrence
        with self._mock_commits(commits):
            pairs = code_scan.build_cochange_matrix(tmp_path, min_count=2)
        assert pairs == []

    def test_unsupported_extensions_excluded(self, tmp_path):
        # .lock and .json should not produce pairs
        commits = [
            ("2024-01-01", ["package-lock.json", "yarn.lock", "app.py"]),
        ] * 3
        with self._mock_commits(commits):
            pairs = code_scan.build_cochange_matrix(tmp_path, min_count=1)
        # No pair should involve lock/json files
        for p in pairs:
            assert not p["file_a"].endswith(".json")
            assert not p["file_b"].endswith(".lock")

    def test_last_date_tracked(self, tmp_path):
        commits = [
            ("2024-01-01", ["a.py", "b.py"]),
            ("2024-06-15", ["a.py", "b.py"]),
        ]
        with self._mock_commits(commits):
            pairs = code_scan.build_cochange_matrix(tmp_path, min_count=2)
        assert pairs[0]["last_date"] == "2024-06-15"

    def test_empty_repo_returns_empty(self, tmp_path):
        with self._mock_commits([]):
            pairs = code_scan.build_cochange_matrix(tmp_path)
        assert pairs == []


# ---------------------------------------------------------------------------
# index_repo (integration — mocks git and DB)
# ---------------------------------------------------------------------------


class TestIndexRepo:
    def test_index_repo_writes_to_db(self, tmp_path):
        _write(tmp_path, "auth.py", "import jwt\n")
        _write(tmp_path, "utils.py", "import os\n")
        commits = [("2024-01-01", ["auth.py", "utils.py"])] * 3

        with patch.object(code_scan, "_get_all_commit_files", return_value=commits):
            stats = code_scan.index_repo(tmp_path, "testproject", verbose=False)

        assert stats["files_found"] == 2
        assert stats["cochange_pairs"] == 1
        assert db.is_repo_indexed(str(tmp_path))

    def test_index_repo_is_idempotent(self, tmp_path):
        _write(tmp_path, "main.py", "import os\n")
        commits = [("2024-01-01", ["main.py"])]

        with patch.object(code_scan, "_get_all_commit_files", return_value=commits):
            code_scan.index_repo(tmp_path, "proj", verbose=False)
            code_scan.index_repo(tmp_path, "proj", verbose=False)

        # Should still be 1 file, not 2
        with db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM code_entities WHERE project = 'proj'"
            ).fetchone()[0]
        assert count == 1

    def test_index_repo_returns_language_counts(self, tmp_path):
        _write(tmp_path, "app.py", "import os")
        _write(tmp_path, "app.ts", "import x from 'y'")

        with patch.object(code_scan, "_get_all_commit_files", return_value=[]):
            stats = code_scan.index_repo(tmp_path, "proj", verbose=False)

        assert stats["languages"]["python"] == 1
        assert stats["languages"]["typescript"] == 1


# ---------------------------------------------------------------------------
# db — code context functions
# ---------------------------------------------------------------------------


class TestCodeContextData:
    def test_returns_memories_for_query(self):
        db.save_memory("use JWT for stateless auth", "decision", project="pluto", source="agent")
        result = db.get_code_context_data("pluto", "JWT auth", limit=5)
        assert len(result["memories"]) >= 1
        assert result["project"] == "pluto"
        assert result["query"] == "JWT auth"

    def test_returns_empty_files_when_not_indexed(self):
        db.save_memory("some decision", "decision", project="unindexed")
        result = db.get_code_context_data("unindexed", "auth")
        assert result["ranked_files"] == []

    def test_cochange_partners_surfaced(self, tmp_path):
        # Index a repo with two co-changing files
        from unittest.mock import patch as _patch

        from brainvault import code_scan as cs

        _write(tmp_path, "auth.py", "import jwt")
        _write(tmp_path, "test_auth.py", "import pytest")
        commits = [("2024-01-01", ["auth.py", "test_auth.py"])] * 5

        with _patch.object(cs, "_get_all_commit_files", return_value=commits):
            cs.index_repo(tmp_path, "myproj", verbose=False)

        # Save a git memory that mentions auth.py
        db.save_memory(
            "[git] abc12345: refactor auth module\nDate: 2024-01-01\nAuthor: dev\n"
            "Changed: 2 files, +50 -10 lines\nFiles: auth.py, test_auth.py",
            "decision",
            project="myproj",
            source="git",
        )

        result = db.get_code_context_data("myproj", "auth", limit=5)
        file_paths = [f["file_path"] for f in result["ranked_files"]]
        assert "auth.py" in file_paths

    def test_file_path_like_match(self, tmp_path):
        from unittest.mock import patch as _patch

        from brainvault import code_scan as cs

        _write(tmp_path, "auth/jwt.py", "import hmac")
        _write(tmp_path, "utils/helpers.py", "import os")

        with _patch.object(cs, "_get_all_commit_files", return_value=[]):
            cs.index_repo(tmp_path, "likematch", verbose=False)

        result = db.get_code_context_data("likematch", "jwt", limit=5)
        file_paths = [f["file_path"] for f in result["ranked_files"]]
        assert any("jwt" in p for p in file_paths)

    def test_is_repo_indexed_false_before_index(self):
        assert db.is_repo_indexed("/nonexistent/repo") is False

    def test_is_repo_indexed_true_after_index(self, tmp_path):
        from unittest.mock import patch as _patch

        from brainvault import code_scan as cs

        _write(tmp_path, "main.py", "x = 1")
        with _patch.object(cs, "_get_all_commit_files", return_value=[]):
            cs.index_repo(tmp_path, "checkproj", verbose=False)
        assert db.is_repo_indexed(str(tmp_path)) is True
