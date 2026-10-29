# Per-task operating loop

Read this after the host is running and the Harness page is open. `SKILL.md`
covers setup and the rules; this file covers how to run one task end to end.

## 1. Orient before acting

Take a fresh snapshot of the page instead of assuming the last layout. Confirm:

- the page belongs to this operator's task space (`spaceId` printed by the
  connect module), not a user space;
- the workspace and model shown in the UI are the ones the user expects;
- no task is currently running in this session.

If a task is already running, wait for it or ask the user. Do not queue a second
task into the same session.

## 2. Write the dispatch text to a file

Compose the task message as a file, then send it by referencing the file. Keep
Chinese and other non-ASCII text out of `-e` command lines, and do not edit or
replay a previously sent script. Every dispatch is new text for the current task.

The message should state the goal, the exact write scope, and how the result will
be checked. Ask for one focused unit of work with a verifiable artifact.

## 3. Select controls from the live snapshot

Choose the input box, send action and workspace switcher from the snapshot you
just took. Never hardcode button indices, element ids, model names, ports, space
ids or page labels: they change between versions and between runs.

## 4. Wait for real completion

Completion is a new terminal message plus an idle input state, not a spinner and
not merely a reachable port. Keep polling with a bounded wait. If the page is
still streaming, continue waiting rather than re-sending.

## 5. Verify, then report

Treat the chat reply as a claim. Check the tool trace: which read, write or shell
calls actually ran, and in what order. Then verify the artifacts independently —
read the produced files yourself and confirm their content, encoding and line
endings. Report only what you verified, and say plainly what remains unverified.

## Space and tab handling

The connect module uses the documented ego-browser API only:

- `listTaskSpaces()` lists spaces with a numeric `id`; a TaskSpace handle exposes
  `spaceId`, `name`, `ownership`, `pages()`, `tabs()`, `newPage()`, `adopt()`,
  `page(label)` and `userPage()`.
- `taskSpace(name)` resumes your own space with that name; a name owned by the
  user raises `EGO_TASK_SPACE_NAME_IN_USE`.
- `claimTaskSpace(id)` claims a user-owned or inactive space, and is used only
  when the user explicitly asked to continue that space.
- `tabs()` returns every tab as `{label?, page, targetId, title, url, active,
  openedBy}`. A tab without a label is unmanaged and must be adopted before use.

Rules that follow from that:

- A new space already has a blank Page `p1`. Navigate it instead of opening
  another blank page.
- Reuse the same space for ongoing work with this user. Never open a second
  space to recover from a failed attempt.
- Only a tab whose URL matches the recorded origin exactly may be taken over. If
  several tabs match, stop and ask which one to use. Never adopt a lone,
  unrelated tab, a blank tab belonging to the user, or another loopback service.
- After an explicit claim, `pages()` may be empty while `tabs()` still holds the
  unmanaged Harness tab: adopt that tab's page rather than opening a new one.
- When a space with the expected name exists but is not agent-owned, the module
  refuses and tells you to ask the user and re-run with `--claim-space <id>`.
  It does not claim on its own and does not create a replacement space.

## When the user takes over

If the user starts interacting with the browser or the Harness window, stop
driving the page. Report the current state and wait for instructions. Do not
compete for the same tab.

## Boundaries

- The hidden web host and the desktop Harness share the same Harness data and
  user storage, but a session already running in the desktop host stays owned by
  that host. This skill does not take it over.
- Browser events avoid fighting the user's global keyboard and mouse, but model
  requests and tool execution still consume this machine and the user's account.
- There is no unattended mode. A long task needs the current chat to keep
  following it, or a separate run the user explicitly arranges.
- Keep the authenticated URL out of every log you produce. The connect module
  never prints it, and `scripts/run_ego.ps1` masks whatever the browser driver
  echoes back.
