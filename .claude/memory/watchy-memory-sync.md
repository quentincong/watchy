---
name: watchy-memory-sync
description: Local bidirectional project-rule and memory compatibility between Claude Code and Codex
metadata:
  node_type: memory
  type: project
  originSessionId: a1e73bf8-6f02-4b49-b5b4-fb1b37ab7cc8
---

# Claude Code and Codex local compatibility

## Current decision — 2026-09-10

The purpose of this setup is **Codex & Claude Code development compatibility and
bidirectional compatibility**. Both tools must read the same project rules and the same
memory files on this computer, so a decision or development checkpoint recorded by one
tool is immediately available to the other.

- `CLAUDE.md` is the authoritative shared project-rule file.
- `AGENTS.md` is a thin Codex entry point that requires Codex to read `CLAUDE.md` in full.
- `.claude/memory/` is the single shared project-memory directory.
- Claude Code's native project-memory path,
  `C:\Users\qc\.claude\projects\C--Users-qc-watchy\memory`, is a Windows directory
  junction to `C:\Users\qc\watchy\.claude\memory`.
- The previous native-memory directory was retained as a dated backup when the junction
  was created; it is not an active memory source.

Claude Code therefore reaches the shared files through its native memory path, while
Codex reaches them through the repository path. There is no copy step and no one-way
overwrite; both paths resolve to the same physical files.

## Claude Code on WSL — 2026-09-10 (Claude Code follow-up)

Claude Code is also run from WSL (`/mnt/c/Users/qc/watchy`), which keys its native memory
under the Linux home, not the Windows junction above. Both WSL project keys (the path case
depends on how the shell `cd`s in) are now symlinks to the shared directory:

- `/home/qc-linux/.claude/projects/-mnt-c-Users-qc-watchy/memory` → `/mnt/c/Users/qc/watchy/.claude/memory`
- `/home/qc-linux/.claude/projects/-mnt-c-users-qc-watchy/memory` → same

Both were empty before linking, so nothing was displaced. `MEMORY.md` hashes identically via
the WSL links, the Windows junction, and the repo path. If a new WSL key appears (different
case/path), link it the same way rather than letting it grow a private memory.

Codex also rewrote 11 untouched files LF→CRLF while working from PowerShell (all `.py`,
`watchy.service`, `ci.yml`, `settings.json`). Those were reverted to LF, and `.gitattributes`
now carries `* text=auto eol=lf` so editor CRLF is normalized on commit — a CRLF
`watchy.service` or `.py` must never reach the VPS. Check diffs with `--ignore-cr-at-eol`.

`scripts/docs_reminder.sh` (the shared PostToolUse hook) now filters on `git commit` in its
own stdin payload: WSL Claude Code ignored the settings.json `if` filter and fired the
reminder after every Bash call.

## Codex provenance rule

After every completed development step or checkpoint, Codex updates the relevant topic
file and updates `MEMORY.md` when its index needs to change. Every such Codex memory entry
must explicitly contain the following exact sentence:

**“这一些开发内容是codex在powershell里做的。”**

这一些开发内容是codex在powershell里做的。

## Reusable guide — 2026-09-10

A reusable Chinese implementation guide for applying the same local Codex and Claude
Code compatibility design to other project directories was created at:

`C:\Users\qc\Downloads\codex-claude-code-local-compatibility.md`

It covers the shared-rule layout, memory comparison and migration, Windows directory
junction setup, SessionEnd retirement, Git/GitHub boundaries, validation, rollback, and
the concrete Watchy example.

这一些开发内容是codex在powershell里做的。

## Suspended GitHub memory sync

The only purpose of `scripts/sync_memory.sh` was to copy Claude Code's machine-local
memory into the repository, commit that memory path, and attempt to push it to GitHub.
That GitHub-memory-sync feature is no longer needed and is suspended.

- The script is retained for history and possible future reactivation, but currently
  exits immediately before the retired mirror logic can run.
- Its SessionEnd hooks are disabled in both `.claude/settings.local.json` and
  `.codex/hooks.json` on this computer.
- Do not run the script while the directory junction is active. The source and destination
  now resolve to the same memory, so the old delete-then-copy mirror design is inappropriate.
- Re-enabling cross-machine or GitHub memory sync requires designing a new safe flow first;
  do not restore the old hook unchanged.

## Previous design (retired)

The 2026-06-15 design kept Claude Code memory under
`~/.claude/projects/<path-hash>/memory/` and used a SessionEnd hook to mirror it into
`.claude/memory/`, commit only that path, and push best-effort. It was a one-way
machine-to-GitHub mirror, not local bidirectional synchronization, and is now retired.

## Validation — 2026-09-10

- Both local hook configurations parse as valid JSON and contain no SessionEnd hook.
- Claude Code's native memory path reports `Junction` and targets the repository memory.
- `MEMORY.md` has the same SHA-256 hash through both paths, proving both tools see the
  same physical content.
- The retired private directory was preserved as
  `memory.pre-shared-20260910` with all 16 prior files.
- The paused shell script passes a syntax check and exits before its old mirror logic.
- `.gitignore` protects both machine-local hook configuration files from accidental commits.

这一些开发内容是codex在powershell里做的。

## Live cross-tool check — 2026-09-10 (Claude Code)

- Fresh headless `claude -p` from WSL, all file tools disabled: answered the `MEMORY.md` titles for
  `watchy-journald-persistence.md` and the Codex-written `watchy-memory-sync.md` entry → Claude Code
  auto-loads the shared index through the WSL symlink.
- `codex.exe exec --sandbox read-only` (binary: `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe`, not on
  PATH): followed AGENTS.md → CLAUDE.md → `.claude/memory/`, returned the provenance sentence, the journald
  index title, and the Claude-written "Claude Code on WSL" heading → Codex reads Claude's memory.
- Caveat: Codex's memory load is instruction-driven (AGENTS.md), not native; Claude's is native. Neither
  locks files — don't have both tools editing `MEMORY.md` at the same moment. PowerShell console shows the
  Chinese as mojibake (display only; files are UTF-8).
