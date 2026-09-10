#!/usr/bin/env bash
# PostToolUse(git commit) hook: after a commit that touched code (watchy/ or
# config.yaml) but no docs (README*/CLAUDE.md/docs/), inject a reminder to check
# whether README/docs need a matching update — per CLAUDE.md "Keeping docs in sync".
# Reminder only: never blocks, never edits. Wired in .claude/settings.json (shared,
# committed — path-agnostic so both machines inherit it).
set -uo pipefail

# Not every Claude Code build honors the settings.json `if: Bash(git commit*)` filter
# (WSL fires this after every Bash call), so check the hook payload ourselves.
printf '%s' "$(cat)" | grep -q 'git commit' || exit 0

files="$(git show --name-only --format= HEAD 2>/dev/null)" || exit 0
code="$(printf '%s\n' "$files" | grep -E '^watchy/.*\.py$|^config\.yaml$' || true)"
docs="$(printf '%s\n' "$files" | grep -E '^README|^CLAUDE\.md$|^docs/' || true)"

if [ -n "$code" ] && [ -z "$docs" ]; then
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Docs-sync reminder: the commit you just made changed code (watchy/ or config.yaml) but no README/CLAUDE.md/docs. Per CLAUDE.md \"Keeping docs in sync\", verify whether README and docs need a matching update."}}'
fi
exit 0
