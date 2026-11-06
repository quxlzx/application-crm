# Application CRM

A tiny job-application pipeline tracker: one process, one port, one SQLite file —
serving a browser UI, a REST API, and an [MCP](https://modelcontextprotocol.io) server
from the same FastMCP app. Built to sit next to an agent (Hermes/Claude/etc.) so the
agent can record and interrogate your application pipeline while you use the web UI.

## Why

Spreadsheets of job applications rot. This keeps a stage pipeline
(`lead → applied → assessment → interview → offer | rejected | withdrawn`),
follow-up dates, and an append-only event audit log per application — and exposes
all of it as agent tools with domain-prefixed names (`apps_*`).

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install "fastmcp>=2"
.venv/bin/python server.py            # UI at http://127.0.0.1:8097/  ·  MCP at /mcp
```

Optional systemd user unit (`application-crm.service` in this repo):

```bash
cp application-crm.service ~/.config/systemd/user/
systemctl --user enable --now application-crm
```

Register with an MCP client as a streamable-HTTP server at `http://127.0.0.1:8097/mcp`.

## What's inside

| Surface | Endpoints / tools |
|---|---|
| MCP tools | `apps_add`, `apps_list`, `apps_get`, `apps_stage`, `apps_followup`, `apps_note`, `apps_due`, `apps_stats`, `apps_delete` |
| REST | `GET/POST /api/apps`, `GET/PATCH/DELETE /api/apps/{id}`, `GET /api/stats` |
| Web UI | `/` — zero-build single page, auto-refresh, inline stage dropdowns |

Behavior details:
- `apps_stage` stamps the `applied` date automatically when entering `applied`,
  and clears it if you revert to `lead` (no premature "applied" dates).
- `apps_due` is the action radar: follow-ups due within N days (incl. overdue)
  plus *stalled* open applications (no update for 14+ days).
- Every stage change, note, and follow-up edit lands in an append-only `events`
  table — the pipeline can't silently drift.

## Privacy

`applications.db` is gitignored and never committed — it holds your real
application data. The server creates a fresh empty DB on first run.

## License

MIT
