#!/usr/bin/env python3
"""HANNI Application CRM — web UI + local MCP on one port.
Tracks job applications: stage pipeline, follow-up dates, event history.
SQLite store; REST API for the browser UI; MCP tools (apps_*) for the agent.
Pattern proven by homework-mcp: systemd user unit + hermes config set mcp_servers.application_crm.url.
"""
import json, os, sqlite3, time
from pathlib import Path
from fastmcp import FastMCP
from starlette.responses import JSONResponse, HTMLResponse

DB = Path(os.getenv("APPS_DB", str(Path(__file__).parent / "applications.db")))
mcp = FastMCP("application-crm")

STAGES = ["lead", "applied", "assessment", "interview", "offer", "rejected", "withdrawn"]
CLOSED = {"rejected", "withdrawn"}
STALL_DAYS = 14

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS applications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL, role TEXT DEFAULT '', location TEXT DEFAULT '',
            url TEXT DEFAULT '', source TEXT DEFAULT '',
            stage TEXT DEFAULT 'lead', applied TEXT DEFAULT '',
            followup TEXT DEFAULT '', notes TEXT DEFAULT '',
            created TEXT DEFAULT (datetime('now','localtime')),
            updated TEXT DEFAULT (datetime('now','localtime')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id INTEGER NOT NULL, kind TEXT DEFAULT '', text TEXT DEFAULT '',
            created TEXT DEFAULT (datetime('now','localtime')))""")
init()

def row_json(r):
    return {k: r[k] for k in r.keys()}

def log_event(c, app_id, kind, text=""):
    c.execute("INSERT INTO events(app_id,kind,text) VALUES(?,?,?)", (app_id, kind, text))

def _list(stage="", q="", open_only=False):
    with db() as c:
        qq, a = "SELECT * FROM applications WHERE 1=1", []
        if stage: qq += " AND stage=?"; a.append(stage)
        if q:
            qq += " AND (company LIKE ? OR role LIKE ? OR location LIKE ?)"
            a += [f"%{q}%"] * 3
        qq += " ORDER BY CASE WHEN followup='' THEN 9 ELSE 0 END, followup ASC, updated DESC"
        rows = [row_json(r) for r in c.execute(qq, a)]
    today = time.strftime("%Y-%m-%d")
    for r in rows:
        r["overdue_followup"] = bool(r["followup"]) and r["followup"] < today and r["stage"] not in CLOSED
    if open_only:
        rows = [r for r in rows if r["stage"] not in CLOSED]
    return rows

def _get(id):
    with db() as c:
        r = c.execute("SELECT * FROM applications WHERE id=?", (id,)).fetchone()
        if not r: return None
        out = row_json(r)
        out["events"] = [row_json(e) for e in c.execute(
            "SELECT kind,text,created FROM events WHERE app_id=? ORDER BY id", (id,))]
        return out

# ---- MCP tools ----
@mcp.tool(name="apps_add")
def apps_add(company: str, role: str = "", url: str = "", location: str = "",
             source: str = "", notes: str = "", followup: str = "") -> str:
    """Add a job application as stage 'lead'. followup format YYYY-MM-DD (optional next-action date)."""
    if not company.strip(): return json.dumps({"error": "company required"})
    if followup and len(followup) != 10: return json.dumps({"error": "followup must be YYYY-MM-DD"})
    with db() as c:
        cur = c.execute("INSERT INTO applications(company,role,url,location,source,notes,followup) VALUES(?,?,?,?,?,?,?)",
                        (company.strip(), role, url, location, source, notes, followup))
        log_event(c, cur.lastrowid, "created", f"source={source}" if source else "")
        return json.dumps(row_json(c.execute("SELECT * FROM applications WHERE id=?", (cur.lastrowid,)).fetchone()))

@mcp.tool(name="apps_list")
def apps_list(stage: str = "", query: str = "", open_only: bool = False) -> str:
    """List applications. stage: lead/applied/assessment/interview/offer/rejected/withdrawn. open_only skips rejected+withdrawn."""
    return json.dumps(_list(stage, query, open_only), indent=1)

@mcp.tool(name="apps_get")
def apps_get(id: int) -> str:
    """One application with its full event history."""
    r = _get(id)
    return json.dumps(r or {"error": "not found"}, indent=1)

@mcp.tool(name="apps_stage")
def apps_stage(id: int, stage: str, note: str = "") -> str:
    """Move an application to a new stage (lead/applied/assessment/interview/offer/rejected/withdrawn). Sets applied date automatically when entering 'applied'."""
    if stage not in STAGES: return json.dumps({"error": f"stage must be one of {STAGES}"})
    with db() as c:
        r = c.execute("SELECT * FROM applications WHERE id=?", (id,)).fetchone()
        if not r: return json.dumps({"error": "not found"})
        old = r["stage"]
        applied = r["applied"]
        if stage == "applied" and not applied: applied = time.strftime("%Y-%m-%d")
        if stage == "lead": applied = ""  # reverting to lead clears a premature applied date
        c.execute("UPDATE applications SET stage=?, applied=?, updated=datetime('now','localtime') WHERE id=?",
                  (stage, applied, id))
        log_event(c, id, "stage", f"{old} -> {stage}" + (f" | {note}" if note else ""))
        return json.dumps(_get(id), indent=1)

@mcp.tool(name="apps_followup")
def apps_followup(id: int, date: str, note: str = "") -> str:
    """Set the next follow-up date (YYYY-MM-DD) for an application. Empty date clears it."""
    if date and len(date) != 10: return json.dumps({"error": "date must be YYYY-MM-DD"})
    with db() as c:
        if not c.execute("SELECT id FROM applications WHERE id=?", (id,)).fetchone():
            return json.dumps({"error": "not found"})
        c.execute("UPDATE applications SET followup=?, updated=datetime('now','localtime') WHERE id=?", (date, id))
        log_event(c, id, "followup", date + (f" | {note}" if note else ""))
        return json.dumps(_get(id), indent=1)

@mcp.tool(name="apps_note")
def apps_note(id: int, text: str) -> str:
    """Append a note to an application (keeps notes field current + logs it)."""
    with db() as c:
        r = c.execute("SELECT notes FROM applications WHERE id=?", (id,)).fetchone()
        if not r: return json.dumps({"error": "not found"})
        new = (r["notes"] + "\n" + text).strip()
        c.execute("UPDATE applications SET notes=?, updated=datetime('now','localtime') WHERE id=?", (new, id))
        log_event(c, id, "note", text)
        return json.dumps({"ok": True, "notes": new})

@mcp.tool(name="apps_due")
def apps_due(days: int = 7) -> str:
    """Action radar: follow-ups due within N days (incl. overdue) + stalled open applications (no update for 14+ days)."""
    today = time.strftime("%Y-%m-%d")
    horizon = time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))
    stall_cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - STALL_DAYS * 86400))
    rows = _list(open_only=True)
    due = [f'{r["company"]} — {r["role"]} | follow-up {r["followup"]}'
           + (" ⚠OVERDUE" if r["overdue_followup"] else "")
           for r in rows if r["followup"] and r["followup"] <= horizon]
    stalled = [f'{r["company"]} — {r["role"]} | stage {r["stage"]}, last update {r["updated"]}'
               for r in rows if r["updated"][:10] <= stall_cut]
    return json.dumps({"due_within_days": due, "stalled_14d": stalled}, indent=1)

@mcp.tool(name="apps_stats")
def apps_stats() -> str:
    """Pipeline counts by stage, open total, overdue follow-ups, stalled count."""
    rows = _list()
    by = {s: sum(1 for r in rows if r["stage"] == s) for s in STAGES}
    open_rows = [r for r in rows if r["stage"] not in CLOSED]
    return json.dumps({
        "total": len(rows), "by_stage": by, "open": len(open_rows),
        "overdue_followups": sum(1 for r in open_rows if r["overdue_followup"]),
        "stalled_14d": sum(1 for r in open_rows if r["updated"][:10] <=
                           time.strftime("%Y-%m-%d", time.localtime(time.time() - STALL_DAYS * 86400)))}, indent=1)

@mcp.tool(name="apps_delete")
def apps_delete(id: int) -> str:
    """Delete an application and its event history."""
    with db() as c:
        n = c.execute("DELETE FROM applications WHERE id=?", (id,)).rowcount
        c.execute("DELETE FROM events WHERE app_id=?", (id,))
        return json.dumps({"deleted": n})

# ---- REST + UI for the browser ----
@mcp.custom_route("/api/apps", methods=["GET"])
async def api_list(request):
    return JSONResponse(_list(request.query_params.get("stage", ""),
                              request.query_params.get("q", ""),
                              request.query_params.get("open", "") == "1"))

@mcp.custom_route("/api/apps", methods=["POST"])
async def api_add(request):
    b = await request.json()
    return JSONResponse(json.loads(apps_add(b.get("company", ""), b.get("role", ""),
                                            b.get("url", ""), b.get("location", ""),
                                            b.get("source", ""), b.get("notes", ""),
                                            b.get("followup", ""))))

@mcp.custom_route("/api/apps/{id}", methods=["GET"])
async def api_get(request):
    return JSONResponse(_get(int(request.path_params["id"])) or {"error": "not found"})

@mcp.custom_route("/api/apps/{id}", methods=["PATCH"])
async def api_patch(request):
    b = await request.json()
    id = int(request.path_params["id"])
    out = {}
    if "stage" in b: out["stage"] = json.loads(apps_stage(id, b["stage"], b.get("note", "")))
    if "followup" in b: out["followup"] = json.loads(apps_followup(id, b["followup"]))
    if "notes" in b: out["notes"] = json.loads(apps_note(id, b["notes"]))
    return JSONResponse(out or {"error": "nothing to update"})

@mcp.custom_route("/api/apps/{id}", methods=["DELETE"])
async def api_del(request):
    return JSONResponse(json.loads(apps_delete(int(request.path_params["id"]))))

@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request):
    return JSONResponse(json.loads(apps_stats()))

UI = """<!doctype html><meta charset=utf-8><title>HANNI Application CRM</title>
<style>body{font-family:system-ui;background:#15161c;color:#e8e8ef;max-width:1000px;margin:2em auto;padding:0 1em}
h1{font-size:1.3em}table{width:100%;border-collapse:collapse}td,th{padding:.5em;border-bottom:#2c2e3a 1px solid;text-align:left;vertical-align:top}
.overdue{color:#ff7b7b;font-weight:600}.due{color:#8fd08f}a{color:#7aa2f7}
button{background:#2c2e3a;color:#e8e8ef;border:0;border-radius:6px;padding:.35em .7em;cursor:pointer}
button:hover{background:#3a3c4a}
input,select{background:#22232c;color:#e8e8ef;border:1px solid #3a3c4a;border-radius:6px;padding:.35em}
#add{display:flex;gap:.5em;flex-wrap:wrap;margin:1em 0}
.badge{display:inline-block;padding:.15em .6em;border-radius:10px;font-size:.8em}
.lead{background:#3a3c4a}.applied{background:#1e3a5f}.assessment{background:#4a3b1e}
.interview{background:#1e4a3a}.offer{background:#1e5f2f;color:#c8ffc8}
.rejected{background:#4a1e1e;opacity:.7}.withdrawn{background:#2c2e3a;opacity:.5}
#stats{opacity:.7;margin-left:1em}small{opacity:.55}</style>
<h1>💼 Application CRM <small style="opacity:.6">HANNI ✦</small><span id=stats></span></h1>
<div id=add><input id=co placeholder="company" style="width:12em"><input id=ro placeholder="role" style="width:16em">
<input id=lo placeholder="location"><input id=ur placeholder="url" style="width:14em">
<input id=fu type=date title="follow-up date">
<button onclick="add()">Add</button></div>
<table><thead><tr><th>Company</th><th>Role</th><th>Stage</th><th>Follow-up</th><th>Updated</th><th>Notes</th><th></th></tr></thead><tbody id=tb></tbody></table>
<script>
const STAGES=["lead","applied","assessment","interview","offer","rejected","withdrawn"];
const f=async(u,o)=>(await fetch(u,o)).json();
const esc=s=>(s||"").replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');
async function draw(){const rows=await f('/api/apps');
tb.innerHTML=rows.map(r=>`<tr>
<td><b>${esc(r.company)}</b><br><small>${esc(r.location)}${r.applied?' · applied '+r.applied:''}</small></td>
<td>${r.url?`<a href="${r.url}" target=_blank>${esc(r.role)}</a>`:esc(r.role)}</td>
<td><select onchange="stage(${r.id},this.value)" class=badge-${r.stage}>${STAGES.map(s=>`<option ${s==r.stage?'selected':''}>${s}</option>`).join('')}</select><span class="badge ${r.stage}">&nbsp;</span></td>
<td class="${r.overdue_followup?'overdue':r.followup?'due':''}">${r.followup||'—'}${r.overdue_followup?' ⚠':''}</td>
<td><small>${r.updated}</small></td>
<td style="max-width:18em;font-size:.85em">${esc(r.notes)}</td>
<td><button onclick="del(${r.id})">✕</button></td></tr>`).join('');
const s=await f('/api/stats');
stats.textContent=` — ${s.open} open · ${s.by_stage.interview} interviewing · ${s.overdue_followups} overdue follow-ups · ${s.stalled_14d} stalled`;}
async function add(){if(!co.value)return;await f('/api/apps',{method:'POST',headers:{'content-type':'application/json'},
body:JSON.stringify({company:co.value,role:ro.value,location:lo.value,url:ur.value,followup:fu.value})});
co.value=ro.value=lo.value=ur.value=fu.value='';draw();}
async function stage(id,st){await f('/api/apps/'+id,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({stage:st})});draw();}
async function del(id){if(!confirm('delete?'))return;await f('/api/apps/'+id,{method:'DELETE'});draw();}
draw();setInterval(draw,15000);</script>"""

@mcp.custom_route("/", methods=["GET"])
async def ui(request):
    return HTMLResponse(UI)

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(os.getenv("PORT", "8097")), path="/mcp")
