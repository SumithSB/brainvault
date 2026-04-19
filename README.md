<div align="center">
  <a href="https://github.com/SumithSB/brainvault" title="Brainvault on GitHub">
    <img
      src="https://github.com/SumithSB/brainvault/raw/main/assets/logo.png"
      width="180"
      height="180"
      alt="Brainvault logo"
      decoding="async"
    />
  </a>

  <h1>Brainvault</h1>

  <p><strong>Personal memory</strong> for <strong>Claude Code</strong> and Cursor — one local <strong>SQLite</strong> vault, MCP tools, and host hooks. No cloud, no extra infrastructure.</p>
  <p><em>Best experience: <strong>Claude Code</strong>. Cursor works but has limitations — see <a href="#host-support">Host support</a>.</em></p>

  <p>
    <a href="https://pypi.org/project/brainvault/"><img src="https://img.shields.io/pypi/v/brainvault.svg?style=flat-square" alt="PyPI version"/></a>
    <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+"/>
    <img src="https://img.shields.io/badge/storage-SQLite-lightgrey?style=flat-square" alt="SQLite"/>
    <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"/>
  </p>

  <p>
    <a href="https://pypi.org/project/brainvault/">PyPI</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/SumithSB/brainvault">Repository</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/SumithSB/brainvault/issues">Issues</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/SumithSB/brainvault/blob/main/CHANGELOG.md">Changelog</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/SumithSB/brainvault#readme">Documentation</a>
  </p>
</div>

---

Brainvault gives your agent **durable context**: decisions, patterns, project facts, and session notes stay in **`~/.brainvault/memory.db`**, searchable across projects. Install once globally; hooks keep the vault warm while you work.

---

## Quick start

```bash
pip install brainvault
brainvault install
```

**Fully quit** your editor (not just close a tab), then reopen so MCP loads. Verify:

```bash
brainvault doctor
```

---

## Host support

### Claude Code — recommended

Brainvault is built around Claude Code and works seamlessly there:

- **CLAUDE.md** is injected as a system-level instruction — Claude follows the memory rules reliably on every turn.
- **Stop + PostToolUse hooks** fire automatically after each turn — no manual steps.
- `get_my_context()` is called at session start without prompting.
- Cross-project context, proactive capture, and git scan all work out of the box.

### Cursor — supported, with caveats

Cursor works but the experience is noticeably less smooth:

| Limitation | Detail |
|------------|--------|
| **Agent mode only** | MCP tools are unavailable in plain chat. Must use Agent / Composer mode with brainvault enabled in MCP settings. |
| **Model compliance** | Cursor can run non-Claude models. `.mdc` rules are advisory — models follow them with varying reliability. `get_my_context()` at session start is often skipped. |
| **Rules vs system prompt** | CLAUDE.md is a hard system instruction. Cursor's `.mdc` rules are softer hints, especially for models not fine-tuned to follow them. |

**Workaround in Cursor:** if context feels missing, say `”check my context”` — that reliably triggers `get_my_context()`. Always verify brainvault is listed as an active MCP server for the current chat.

---

## How it works in practice

**Session start — context loads automatically**

Open Claude Code and start working. Brainvault calls `get_my_context()` on your first turn:

```
> hey, continuing work on the backend

[Claude calls get_my_context()]

Got it. Picking up on myapp — a few things worth flagging before we start:
- Auth middleware: session tokens stored in a way that may not meet the new compliance
  requirements — this was flagged as a known risk last sprint.
- DB connection pool set to 5 in staging config, still needs bumping before prod deploy.
```

No briefing needed. It remembered.

---

**Bug you've seen before — surfaces instantly**

```
> getting a weird memory leak, can't figure out where it's coming from

[Claude calls search_memory("memory leak background task")]

Found it — you hit this in a previous project. Root cause: background tasks holding
references to closed database connections. Fix: explicit cleanup in the task teardown,
don't rely on GC. You noted this pattern 3 weeks ago.
```

One search, actionable answer, zero re-diagnosis.

---

**Starting a new project — context follows you**

```
> starting a new service, going with FastAPI + PostgreSQL

[Claude calls register_project() + save_memory(decision)]

Registered. Noted the stack. Pulling in patterns from your other projects — you've
already solved async connection pooling, JWT auth flow, and rate limiting. Want me
to flag those decisions before you re-implement them?
```

Prior work becomes a starting point, not forgotten history.

---

**When does it get good?**

Brainvault improves as you use it — here's roughly what to expect:

| Stage | What's there | Experience |
|-------|-------------|------------|
| **Day 1** (after install + bootstrap) | Old session transcripts, git history | Basic recall — raw notes, rough search |
| **~1 week** | First live session memories, a few decisions + patterns captured | Noticeably useful — key bugs and choices surface |
| **~1 month** | Cross-project patterns, profile built up, outcomes recorded | Seamless — Claude picks up context without prompting |
| **Ongoing** | Vault grows with every session | Gets sharper the more projects and decisions accumulate |

Run `brainvault bootstrap` and `brainvault bootstrap-git ~/Projects` after install to seed the vault with your existing history — this jumpstarts the process significantly.

---

## What it does

- **Hooks:** after each agent turn — session notes, git scan, optional repo index, optional embedding backfill.
- **Search:** **FTS5** keyword search always; optional **semantic** blend if you install `[semantic]` (see below).

**MCP tools** (12) — available to the agent once MCP is connected:

| Tool | Purpose |
|------|---------|
| `get_my_context` | Profile, active projects, short vault stats |
| `search_memory` | Find memories by query (FTS5; + vectors if `[semantic]` installed) |
| `save_memory` | Store a memory (`profile`, `project`, `decision`, `pattern`, `note`) |
| `register_project` | Create or update a project record |
| `get_project` | Everything stored for one project (+ recent memories) |
| `record_outcome` | Attach outcome + sentiment to a decision |
| `reflect` | Open decisions, cross-project patterns, stale projects, hot memories |
| `update_memory` | Edit an existing memory |
| `forget` | Delete a memory (and its embedding row) |
| `get_code_context` | Memories + indexed files / co-change for a project + topic |
| `get_recent_activity` | Recent sessions and tool activity |
| `get_session_timeline` | Chronological tool events for one session |

**Memory types** (for `save_memory`):

| Type | Use for |
|------|---------|
| `profile` | You — preferences and working style (global) |
| `project` | Stack, constraints, onboarding-style facts |
| `decision` | Choices with **why** (alternatives rejected) |
| `pattern` | Repeatable “when X, do Y” rules |
| `note` | Mostly auto-captured session summaries |

**Vault:** `~/.brainvault/memory.db` (global — not per-repo).

**Configs patched (global):**

| Host | Main paths |
|------|------------|
| Claude Code | `~/.claude/settings.json`, `~/.claude/CLAUDE.md` |
| Cursor | `~/.cursor/mcp.json`, `~/.cursor/rules/brainvault.mdc`, `~/.cursor/hooks.json` |

`brainvault install --agent claude_code` or `--agent cursor` limits which host is patched.

---

## When to run what

| You want | Command |
|----------|---------|
| Catch up **old** Claude sessions (`~/.claude/projects`) | `brainvault bootstrap --host claude_code` |
| Catch up **old** Cursor transcripts (`~/.cursor/projects/.../agent-transcripts`) | `brainvault bootstrap --host cursor` |
| Both (default) | `brainvault bootstrap` |
| Mine **git** across many repos | `brainvault bootstrap-git ~/Projects` (or `~`) |
| Inspect / fix wiring | `brainvault doctor` |
| Search from terminal | `brainvault search "<query>"` |
| Vault overview | `brainvault status`, `brainvault stats`, `brainvault reflect` |
| Optional **meaning**-based search (heavier install) | `pip install 'brainvault[semantic]'` then `brainvault embed` once to backfill vectors |

Bootstrap and bootstrap-git are **idempotent** (safe to re-run).

---

## CLI (short list)

Run **`brainvault help`** for the full command list. Common ones: `install`, `uninstall`, `doctor`, `bootstrap`, `bootstrap-git`, `git-scan`, `index-repo`, `search`, `status`, `stats`, `reflect`, `export` / `import`, `embed`, `graph`, `sessions`, `activity`.

---

## Optional semantic search

```bash
pip install 'brainvault[semantic]'
brainvault embed
```

Adds local embeddings (fastembed + sqlite-vec); first run downloads a small model to `~/.cache/huggingface`. Search blends keywords + vectors when extras are installed. **Not required** for normal use.

---

## Out of scope

- Not cloud sync or team wiki.
- Not full RAG over every line of your repo; `index-repo` + `get_code_context` are structural (files, imports, co-change), not “embed all source”.

---

## Contributing / internals

- **[CLAUDE.md](CLAUDE.md)** — module map, hook behaviour, MCP tool details, how to run tests.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — releases, PyPI README images, dev notes.

```bash
git clone https://github.com/SumithSB/brainvault.git
cd brainvault
pip install -e ".[dev]"
pytest
```

MIT License.
