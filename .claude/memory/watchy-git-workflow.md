---
name: watchy-git-workflow
description: "Cross-machine Git sync workflow for watchy (local + VPS) — pull at start, push at checkpoints"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b5650f24-68b0-4787-b928-29a712a1ef71
---

Watchy is worked on across **two machines (local + VPS)**, kept in sync via Git.

**Why:** both machines must not diverge; Git is the single source of truth.

**How to apply:**
- **Always `git pull` before starting any work session.**
- **Always `git push` when ending a session or hitting a checkpoint.**
- Commit messages briefly describe what changed.
- **Commit the `.claude/` directory** (keeps Claude Code config in sync) — do NOT gitignore it.
- **Never commit `.env` or secrets** (Watchy secrets live in `~/watchy_config/secrets.yaml`,
  outside the repo — keep it that way).
- Session flow: start → `git pull` → work → `git add -A && git commit -m "..."` → `git push` → end.
- **If a `git pull`/merge shows CONFLICTS: STOP and tell the user. Do NOT resolve automatically.**

Branching: the user's workflow commits/pushes directly on `main` for this repo (overrides the
default "branch first" rule). The implementation plan lives at `docs/IMPLEMENTATION_PLAN.md`
(in-repo). See [[watchy-issue-plan]].

**Releases (GitHub):** v1.0.0 (2026-06-24, production deployment), **v1.1.0 (2026-09-13, tag on
b3aae12: take-profit #28/#30, market-calendar + tiered Tier 2 cadence, 10:02 UTC peak-aware start,
DeepSeek V4.1 Flash, advisor urgency/decision log #31)**. User accepted Claude's semver call (minor,
not major, despite config additions). Create with `gh release create vX --target <FULL sha>` — a
short sha fails with HTTP 422. Tags don't trigger the VPS auto-update (it only watches `origin/main`).
Version metadata: v1.0.0/v1.1.0 shipped with `0.1.0` still in the package (never bumped). Since the
Watchy 2.0 work (2026-09-22) `pyproject.toml` and `watchy/__init__.py` both say **`2.0.0rc1`** (PEP 440
form of tag `v2.0.0-rc.1`), and `tests/test_v2_regressions.py::TestVersion` enforces they match — bump
both together for every release. The daemon logs `Watchy <version> starting`. Bumping = push to main =
daemon restart. See [[watchy-2-implementation]].
