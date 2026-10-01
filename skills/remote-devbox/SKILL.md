---
name: remote-devbox
description: >-
  Drive the user's own Windows + Docker machine as a remote devbox through the
  remote-devbox-mcp bridge: clone the tiny arena client, connect over their
  Cloudflare ingress, and run edits, builds, tests and a running app on their
  hardware instead of a cloud sandbox. Use when the user shares a
  remote-devbox-mcp repository link together with an INGRESS URL and tokens, or
  says "use my machine/devbox", "run this on my computer", "connect to my
  devbox", "arena agent", or "remote devbox". Do not use for local-only tasks or
  when no devbox URL/token is available.
---

# remote-devbox

Turn the user's machine into your build/test host. You keep the brain; their
machine only executes tools (no LLM, no agent on their side).

## What you get

A bridge exposing OpenCode-native tools over the tunnel:
`read/write/edit/apply_patch/glob/grep/bash/lsp/todowrite`, with a job +
permission protocol. You call them through a stdlib-only Python client
(`arena/mcp_client.py` + the `mcp` wrapper).

## What the user must give you

- a repository link: `https://github.com/WhiteBite/remote-devbox-mcp`
- `INGRESS` — the public base URL (e.g. `https://xxxx.trycloudflare.com`)
- `BRIDGE_TOKEN` — equals `MCP_BEARER_TOKEN`
- optionally `HOST_TOKEN` (== `MCP_PUBLIC_TOKEN`) and extra endpoints for
  host-MCP / runner-MCP

If any of these are missing, ask — do not invent them.

## Bootstrap

1. Fetch the agent side (no install needed beyond `python3` 3.10+ and `bash`):
   `git clone --depth 1 <repo> devbox && cd devbox/arena && chmod +x mcp`
   (the exec bit is lost after a sandbox snapshot — re-`chmod` each session).
2. Write `~/.remote-devbox-mcp.conf` from `mcp.conf.example`:
   `MCP_URL=<INGRESS>/p/8787/mcp`, `MCP_TOKEN=<BRIDGE_TOKEN>`, `MCP_TIMEOUT=300`.
3. Handshake: `./mcp check` — expect `opencode-mcp-bridge` and the tool list.
   A single `530` is normal (tunnel reconnecting); on persistent `530/1033`
   wait 2-3 min, retry once, then stop and report to the user.
4. Read `arena/AGENT_INSTRUCTIONS.md` (full rules) and `arena/SANDBOX_FACTS.md`
   (measured facts about your sandbox). The task spec lives in the project at
   `/workspace/ARENA_TASK.md`.

## Client commands

```bash
./mcp check                                   # handshake + tools
./mcp list                                    # tool names
./mcp schema edit                             # argument schema
./mcp run read '{"filePath":"src/Main.java","offset":1,"limit":80}'
./mcp run --auto edit '{"filePath":"...","oldString":"...","newString":"..."}'
./mcp run --auto bash '{"command":"./gradlew test"}'
./mcp reply <job_id> <permission_id> once     # approve one request
./mcp job <job_id>                            # re-fetch a long job's result
./mcp run --auto --wait-seconds auto --polls auto bash '{"command":"./gradlew test"}'
```

Mutations (`write/edit/apply_patch/bash/webfetch`) default to
`awaiting_permission`; `--auto` replies `once` and waits.

## Where the rules live

- `arena/AGENT_INSTRUCTIONS.md` — job/permission protocol, tool names, exit
  codes, obligations (diagnostics after edits, git discipline, never read
  secrets).
- `arena/SANDBOX_FACTS.md` — your sandbox limits (bash timeout default 30 s,
  max 1800 s; only `/home/user` persists).
- `ARENA.md` — the human-facing entry point (bootstrap, workflow, hand-off
  template).

## Task convention

The spec is `/workspace/ARENA_TASK.md`. Create it from
`arena/ARENA_TASK.template.md` when you are the one framing the work: goal,
scope, acceptance criteria, verification procedure, and what not to touch.

## Exit codes

`0` ok · `2` config · `3` tunnel · `4` permission needed · `5` job error.
A `completed` job with a non-zero bash exit still exits `5` — a failed test is
never a success.

## Do / Don't

- **Do** run every build/test on the devbox via `bash`, not in your sandbox.
- **Do** verify after every edit (LSP or a compile/test run).
- **Do** rename via `lsp`, not text replace.
- **Don't** read or print secrets (`.env`, keys, `application-prod.*`).
- **Don't** write outside `/workspace` — the bridge rejects it.
- **Don't** run `cat > file` via `bash`; use the file tools.
- **Don't** install packages inside the container for host-build projects
  (MidasAI etc.) — use the runner there instead.

## Troubleshooting

| Symptom | Action |
|---|---|
| 502/520/521/523/530 | client retries itself; wait |
| persistent 530 >3 min | wait 2-3 min, retry `check` once, then ask the user |
| 401 after a Docker restart | INGRESS URL rotated; ask for the new one |
| `metadata.exit != 0` | exit 5, read `metadata.outputPath` if truncated |
| status `cancelling` | don't poll; start a new job |
| client killed by timeout | the job is alive on the devbox; `./mcp job <id>` |