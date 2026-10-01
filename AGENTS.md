# AGENTS.md

> Operational instructions for coding agents (Codex, Cursor, OpenCode, Claude Code).
> Content below is hand-verified against the repository; commands are real.

Project: WhiteBite/remote-devbox-mcp — turns your own Windows+Docker machine into a remote devbox that an external coding agent drives through an outbound Cloudflare tunnel and a containerized opencode-mcp-bridge.

## Commands

```bash
# install dev dependencies
python -m pip install -r requirements-dev.txt

# run the test suite (pytest, tests/ with unit/integration/e2e dirs)
python -m pytest

# lint (ruff config lives in pyproject.toml; there is no compiled build step)
python -m ruff check home arena tests
```

## Repository map

| Path | Purpose |
| --- | --- |
| `home/` | host side: `devbox.py` core (`rdm/` package), docker-compose stack, `host/runner-mcp.py` |
| `arena/` | agent-side Python MCP client (`mcp_client.py`, `mcp` wrapper) |
| `projects/` | per-project JSON profiles (`muffin.json`, `_template.json`) |
| `tests/` | pytest suite (unit / integration / e2e) |
| `.discoverability/project.yml` | source of truth for repo metadata |

## Do / Don't

- **Do** run `python -m pytest` before committing.
- **Do** keep `.discoverability/project.yml` in sync with the README.
- **Don't** edit `.py`, `.ps1`, compose or Dockerfile files as part of doc-only work.
- **Don't** bump versions, create tags, publish, force-push or delete files without explicit human confirmation.
- **Don't** rewrite unrelated files while fixing a specific finding.

## Discoverability (RDK)

- `npx repo-aeo audit` — Discoverability Score 0-100 and findings; read-only.
- The rdk-audit workflow fails pull requests below `vars.RDK_MIN_SCORE`.
