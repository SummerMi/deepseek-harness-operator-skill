---
name: deepseek-harness-operator
description: Dispatch work to a background DeepSeek Harness web host while the user keeps the main desktop, then check what it actually produced. Use when asked to run or continue work inside DeepSeek Harness, connect to its web UI, or verify a Harness session's artifacts.
metadata:
  short-description: Background DeepSeek Harness operator
  author: summi
---

# DeepSeek Harness Operator

The user keeps using the main screen. You operate the DeepSeek Harness official
web UI in a hidden, loopback-only host process, and DeepSeek Harness does the
main execution. Your job is to dispatch one task at a time, read the real tool
trace, and verify the artifacts on disk. You are the operator, not the executor.

A session that is already running in the desktop Harness belongs to that host.
Do not take it over, and do not drive the same session from two hosts at once.

## Prerequisites

- DeepSeek Harness is installed from its official installer on this machine.
- `ego-browser` is installed and its `nodejs` mode works.
- The user has already authenticated a DeepSeek account in Harness. You never
  create or fake credentials, and you never print the authenticated URL.

## Workflow

1. `python scripts/harness_operator.py discover` — locate `DeepSeek Harness.exe`
   and the host CLI entry. Packaged installs keep that entry inside
   `resources/app.asar`; the helper validates it against the archive index and
   reports `cli_kind: "asar"` with a virtual path Electron can load.
2. `python scripts/harness_operator.py start` — starts with profile
   `codex-operator`, hidden, on `127.0.0.1` with an ephemeral port. It reuses a
   verified healthy record, takes an exclusive lock while starting, and refuses
   when the state is unclear instead of guessing.
3. `python scripts/harness_operator.py status` — confirm the recorded PID **and**
   process creation stamp still match and that the port answers. A reachable
   port is not proof that a model session works.
4. `python scripts/harness_operator.py connect-script` — write a private `.mjs`
   module, then run it through `scripts/run_ego.ps1`. That wrapper is mandatory:
   it masks authenticated URLs in everything ego-browser prints, including its
   own navigation diagnostics.

After connecting, read the snapshot, dispatch one task, wait for real
completion, and verify the outcome from the tool trace and the produced files.
Do not report a task as done because the UI looks busy or a port is open.

For the per-task loop, space reuse, tab adoption and takeover handling, read
[references/workflow.md](references/workflow.md).

## Hard rules

- Never print, commit or paste the authenticated launch URL or any token. The
  helper prints only profile, PID, credential-free origin and private state path.
- Never run a connect module with a bare `ego-browser nodejs -e ...` command;
  always go through `scripts/run_ego.ps1`.
- Never hardcode the port, model name, space id, page label or selector. Read
  them from the current status and the current snapshot each round.
- Never replay an old send script. Each dispatch is new text written for the
  current task and passed through a file, not through `-e` with inline non-ASCII.
- `--claim-space <id>` takes the positive integer task space id and only applies
  when the user explicitly asks to continue that space. List the spaces first.
  Never claim a user-owned or inactive space on your own, and never create a
  second space to recover from a failure.
- Keep private state outside the profile directory, and never upload
  `operator-state.json`, `startup-*.log` or `connect-*.mjs`.
- Stop when the user takes over the browser. A task assigned to Harness does not
  expand what the user approved: follow the current authorization and the
  tool's own requirements, and do not reach for deletion, deployment, remote
  exposure or other accounts.
