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

One devbox runs exactly **one project at a time**: only the active profile's
folder is mounted at `/workspace`. You work on that single project, you never
see other projects' files, and you do not switch projects — that is the user's
host-side action.

## What you get

A bridge exposing OpenCode-native tools over the tunnel:
`read/write/edit/apply_patch/glob/grep/bash/lsp/todowrite`, with a job +
permission protocol. You call them through a stdlib-only Python client
(`arena/mcp_client.py` + the `mcp` wrapper).

## What the user must give you

- a repository link: `https://github.com/WhiteBite/remote-devbox-mcp`
- `INGRESS` — the public base URL (e.g. `https://xxxx.trycloudflare.com`)
- `BRIDGE_TOKEN` — equals `MCP_BEARER_TOKEN`
- optionally `HOST_TOKEN` (== `MCP_PUBLIC_TOKEN`), present only when the project
  exposes a runner/host-MCP; used to call its `run_*` commands

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
   (measured facts about your sandbox). The task comes from the user in the chat.

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

The task arrives **in the chat** from the user. Read it before any plan; never
invent it. Expect: goal, scope, acceptance criteria, verification procedure,
what not to touch.

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

## Verify UI (Playwright)

Your playwright/chromium runs in your sandbox; the app runs on the devbox.
The hand-off block prints `UI=<INGRESS>/p/<port>/` — the ready URL for the active
project's app; open that with your Playwright and the bearer header.
Reach it over the tunnel — two ways:

1. Ingress path with the bearer token (APIs and plain static):

   ```python
   ctx = browser.new_context(
       extraHTTPHeaders={"Authorization": f"Bearer {INGRESS_TOKEN}"}
   )
   page = ctx.new_page()
   page.goto(f"{INGRESS}/p/{PORT}/", wait_until="networkidle")
   page.screenshot(path="shots/ui.png")
   ```

   `PORT` must be in the profile's allowlist (otherwise 403). Absolute asset
   paths (`/assets/...`) lose the `/p/<port>` prefix — use option 2.
2. `PREVIEW` URL (root, no token) — best for SPA/dev servers with absolute
   asset paths and for `flutter run -d web-server`: `page.goto(PREVIEW)`.

Ask the user to expose the dev server:
`python devbox.py preview http://host.docker.internal:<port>` (host app) or a
container origin; the starter `devbox.py start --preview` prints the URL.

Notes:
- WebSockets are proxied: the bridge forwards the upgrade and, on upstream
  `101`, pipes bytes both ways — HMR/live-reload works through the same URL.
- Electron/desktop: run the app with `--remote-debugging-port=<p>
  --remote-allow-origins=*`, expose port `<p>` (allowlist), then drive it with
  Playwright `connect_over_cdp`. `GET <INGRESS>/p/<p>/json/version` returns
  `webSocketDebuggerUrl` pointing at `127.0.0.1` — rewrite its host to the
  tunnel and scheme to `wss`:
  `connect_over_cdp(f"wss://<tunnel-host>/p/<p>/devtools/browser/<uuid>",
  headers={"Authorization": f"Bearer {INGRESS_TOKEN}"})`.
- Keep screenshots in `/agent` and show them with `present_file`.

## Troubleshooting

| Symptom | Action |
|---|---|
| 502/520/521/523/530 | client retries itself; wait |
| persistent 530 >3 min | wait 2-3 min, retry `check` once, then ask the user |
| 401 after a Docker restart | INGRESS URL rotated; ask for the new one |
| `metadata.exit != 0` | exit 5, read `metadata.outputPath` if truncated |
| status `cancelling` | don't poll; start a new job |
| client killed by timeout | the job is alive on the devbox; `./mcp job <id>` |