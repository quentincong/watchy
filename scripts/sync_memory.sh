#!/usr/bin/env bash
# PAUSED 2026-09-10: this script's sole purpose was GitHub memory sync. Claude Code
# and Codex now use the same local memory directory, so no hook should invoke it.
# Keep this historical script dormant unless a new safe cross-machine design replaces it.
exit 0
# Retired implementation: mirror Claude Code's per-machine memory dir into the repo
# so it could travel via Git. The native ~/.claude memory store was per-machine and
# keyed by the project's absolute path.
#
# This used to be registered as a SessionEnd hook in the machine-local
# .claude/settings.local.json and received the source directory as $1, e.g.:
#   bash scripts/sync_memory.sh "C:/Users/qc/.claude/projects/C--Users-qc-watchy/memory"
#
# Do not restore that hook unchanged. See .claude/memory/watchy-memory-sync.md.
set -uo pipefail

SRC="${1:-}"
[ -n "$SRC" ] && [ -d "$SRC" ] || exit 0

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
DEST="$REPO/.claude/memory"
mkdir -p "$DEST"

# Mirror source -> dest (clear first so deletions propagate).
rm -f "$DEST"/*.md
cp "$SRC"/*.md "$DEST"/ 2>/dev/null || true

cd "$REPO" || exit 0
git add .claude/memory
# Commit ONLY the memory pathspec — never sweep up unrelated staged work.
git diff --cached --quiet -- .claude/memory && exit 0
git commit -q -m "chore(memory): auto-sync Claude memory" \
  -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" -- .claude/memory
git push -q 2>/dev/null || true
exit 0
