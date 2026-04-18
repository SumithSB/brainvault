# Security

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports.

Instead, email the maintainer at **sumith2620@gmail.com** with:

- A description of the issue and its impact
- Steps to reproduce (if possible)
- Affected versions or commit range, if known

We aim to acknowledge within a few business days and coordinate a fix and disclosure timeline.

## Scope

Brainvault is **local-first**: data lives in `~/.brainvault/memory.db`, with host configuration under `~/.claude/` (Claude Code) and/or `~/.cursor/` (Cursor) after `brainvault install`. There is no hosted multi-tenant service; threat modeling should assume **same-user / same-machine** attackers and malicious local data (e.g. opening an untrusted vault file).

## Supply chain

- Install from PyPI: `pip install brainvault`
- Optional extras: `pip install 'brainvault[semantic]'` (pulls embedding dependencies; first run may download a model to the local Hugging Face cache)

Review dependency updates and CI workflows in pull requests before merging.
