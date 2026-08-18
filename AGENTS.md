# Repository Instructions

Before taking any action in this repository, read `CURRENT_HANDOFF.md` completely. It is the
single current operational handoff; older files may contain superseded sections labelled current.

For every new session:

1. Report local HEAD and `git status --short`.
2. If the task touches production/recovery, verify AWS HEAD, `LIVE_MODE`, bot process count,
   watchdog pause, autodeploy lock, and kill-switch status read-only before any mutation.
3. Preserve all unrelated dirty and untracked user files. Stage explicit task files only.
4. Keep the Copy Bot Paper-only. Do not reset the kill switch, start the bot, remove either lock,
   or treat stale/indicative prices as executable evidence unless every gate in
   `CURRENT_HANDOFF.md` currently passes.
5. Update `CURRENT_HANDOFF.md` in the same scoped commit as any production-state change. Include
   the verification timestamp, deployed commit, tests, process/lock state, blocker, and next
   permitted action.

Never commit secrets, credentials, `.env` files, SSH keys, or private keys. Never hard-reset the
AWS checkout.
