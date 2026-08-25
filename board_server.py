#!/usr/bin/env python3
"""
Board — a visual surface over the same goals/tasks store the MCP writes to.

WHY IT EXISTS
    A JSON query tells an agent more than a board does. This is for the human:
    a second processing substrate over the same data, and read AND write, so
    work done here flows back into the store the agent reads.

DESIGN
    One data model. Tasks live beside goals in a single store; a board with its
    own database would be another tracking system, not a consolidation.

    Columns encode WHO IS HOLDING IT, not just status, because "who's blocked"
    is the question that actually stalls work.

    Three activity layers, deliberately distinct:
      task_events    automatic  -- status/owner changes, written by the system
      note/feedback  manual     -- discussion
      work           manual     -- effort log with optional minutes
    A comment and a work entry are the same shape with different intent, so they
    share one table with a `kind` discriminator rather than two near-identical
    tables and two UIs.

Run:  python board_server.py [--port 8077] [--host 127.0.0.1]
Stdlib only -- no dependencies.
"""

import argparse, base64, hmac, json, os, secrets, uuid
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import sqlite3

DB_PATH = Path(os.environ.get("GOAL_DB", Path(__file__).parent / "goals.db"))

COLUMNS = [("todo", "To do"), ("doing", "In progress"),
           ("blocked", "Blocked"), ("done", "Done")]

KNOWN_PROJECTS = ["general"]


# HTTP Basic auth. A VPN may gate who can route here, but the board
# can also listen on the LAN.
# The MCP owns the roster; the board must not invent its own or the two disagree
# about who exists the moment anyone sets GOAL_HUMANS. Same names, same defaults.
def _name_list(var, fallback):
    raw = os.environ.get(var, "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or fallback

GOAL_OWNER = os.environ.get("GOAL_OWNER", "agent")
AGENTS = _name_list("GOAL_AGENTS", [GOAL_OWNER])
if GOAL_OWNER not in AGENTS:
    AGENTS.insert(0, GOAL_OWNER)
HUMANS = _name_list("GOAL_HUMANS", ["human"])
ALL_OWNERS = AGENTS + HUMANS

BOARD_USER = os.environ.get("BOARD_USER", "admin")
DEFAULT_ACTOR = os.environ.get("BOARD_ACTOR", HUMANS[0])
BOARD_PASS = os.environ.get("BOARD_PASS", "")


def check_auth(header: str) -> bool:
    if not BOARD_PASS:          # unset = auth disabled (local dev only)
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    # compare_digest on both halves so timing can't leak either one
    return (hmac.compare_digest(user, BOARD_USER)
            and hmac.compare_digest(pw, BOARD_PASS))


def db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.isolation_level = None   # autocommit, matching the psycopg original
    conn.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now():
    return datetime.now(timezone.utc)


def _s(v):
    return str(v) if v is not None else None


# --------------------------------------------------------------------------- API
def api_state():
    with db() as c:
        tasks = c.execute("""
            SELECT id, project, goal_id, title, detail, status, owner, blocked_on,
                   source, priority, position, due_date, on_done_create,
                   parent_task_id, created_at, updated_at, done_at
            FROM tasks ORDER BY position, created_at
        """).fetchall()
        comments = c.execute("""
            SELECT id, task_id, author, kind, body, minutes, created_at
            FROM task_comments ORDER BY created_at
        """).fetchall()
        events = c.execute("""
            SELECT task_id, actor, field, old_value, new_value, created_at
            FROM task_events ORDER BY created_at DESC LIMIT 300
        """).fetchall()
        goals = c.execute("""
            SELECT id, description, status, priority, category, owner,
                   decision_owner, next_action, blocked_by, last_worked,
                   NULL AS project   -- not in the SQLite schema; kept for shape
            FROM goals WHERE status IN ('active','ready_to_act','escalation_needed')
            ORDER BY priority DESC
        """).fetchall()

    def clean(rows, ts=("created_at", "updated_at", "done_at", "due_date")):
        return [{k: (_s(v) if k in ts else v) for k, v in r.items()} for r in rows]

    by_task = {}
    for cm in clean(comments):
        by_task.setdefault(cm["task_id"], []).append(cm)
    ev_task = {}
    for e in clean(events):
        ev_task.setdefault(e["task_id"], []).append(e)

    ts = clean(tasks)
    kids = {}
    for t in ts:
        if t["parent_task_id"]:
            kids.setdefault(t["parent_task_id"], []).append(t)
    for t in ts:
        t["comments"] = by_task.get(t["id"], [])
        t["events"] = ev_task.get(t["id"], [])[:12]
        t["work_minutes"] = sum(x["minutes"] or 0 for x in t["comments"] if x["kind"] == "work")
        ch = kids.get(t["id"], [])
        t["subtasks"] = [{"id": k["id"], "title": k["title"], "status": k["status"],
                          "owner": k["owner"], "blocked_on": k["blocked_on"]} for k in ch]
        t["sub_done"] = sum(1 for k in ch if k["status"] == "done")
        t["sub_total"] = len(ch)
        t["sub_blocked"] = sum(1 for k in ch if k["status"] == "blocked")

    projects = sorted({t["project"] for t in ts} | set(KNOWN_PROJECTS))
    return {"tasks": ts, "goals": goals, "projects": projects, "columns": COLUMNS}


def _event(c, tid, actor, field, old, new):
    c.execute("""INSERT INTO task_events (task_id, actor, field, old_value, new_value)
                 VALUES (?,?,?,?,?)""", (tid, actor, field, _s(old), _s(new)))


def api_create(d):
    tid = str(uuid.uuid4())
    with db() as c:
        c.execute("""INSERT INTO tasks (id, project, goal_id, title, detail, status,
                                        owner, blocked_on, source, priority, due_date,
                                        on_done_create, parent_task_id)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (tid, d.get("project") or "general", d.get("goal_id") or None,
                   (d.get("title") or "").strip() or "(untitled)", d.get("detail"),
                   d.get("status") or "todo", d.get("owner") or DEFAULT_ACTOR,
                   d.get("blocked_on") or None, d.get("source") or "board",
                   float(d.get("priority") or 0.5),
                   d.get("due_date") or None, d.get("on_done_create") or None,
                   d.get("parent_task_id") or None))
        _event(c, tid, d.get("actor") or DEFAULT_ACTOR, "created", None, d.get("title"))
    return {"ok": True, "id": tid}


def api_update(d):
    tid = d.pop("id")
    actor = d.pop("actor", DEFAULT_ACTOR)
    allowed = {"project", "title", "detail", "status", "owner", "blocked_on",
               "priority", "position", "goal_id", "due_date", "on_done_create",
               "parent_task_id"}
    with db() as c:
        before = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not before:
            return {"ok": False, "error": "no such task"}
        sets, vals = [], []
        for k, v in d.items():
            if k in allowed:
                sets.append(f"{k} = ?"); vals.append(v if v != "" else None)
        if not sets:
            return {"ok": False, "error": "nothing to update"}
        sets.append("updated_at = ?"); vals.append(now())
        if d.get("status") == "done":
            sets.append("done_at = ?"); vals.append(now())
        elif "status" in d:
            sets.append("done_at = NULL")
        vals.append(tid)
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)

        for k, v in d.items():
            if k in allowed and _s(before.get(k)) != _s(v):
                _event(c, tid, actor, k, before.get(k), v)

        if d.get("status") == "done" and before.get("status") != "done" and before.get("on_done_create"):
            nid = str(uuid.uuid4())
            # goal_id must carry: the conscience joins on it, and nobody typed this card.
            c.execute("""INSERT INTO tasks (id, project, title, detail, status, owner, source, goal_id)
                         VALUES (?,?,?,?,'todo',?,'auto',?)""",
                      (nid, before["project"], before["on_done_create"],
                       f"Auto-created when '{before['title']}' was completed.",
                       before["owner"], before["goal_id"]))
            _event(c, nid, "board", "created", None, before["on_done_create"])
            return {"ok": True, "spawned": before["on_done_create"]}
    return {"ok": True}


def api_comment(d):
    with db() as c:
        c.execute("""INSERT INTO task_comments (task_id, author, kind, body, minutes)
                     VALUES (?,?,?,?,?)""",
                  (d["task_id"], d.get("author") or DEFAULT_ACTOR, d.get("kind") or "note",
                   (d.get("body") or "").strip(),
                   int(d["minutes"]) if str(d.get("minutes") or "").strip().isdigit() else None))
    return {"ok": True}


def api_uncomment(d):
    with db() as c:
        c.execute("DELETE FROM task_comments WHERE id=?", (d["id"],))
    return {"ok": True}


def api_delete(d):
    with db() as c:
        c.execute("DELETE FROM task_comments WHERE task_id=?", (d["id"],))
        c.execute("DELETE FROM task_events   WHERE task_id=?", (d["id"],))
        c.execute("DELETE FROM tasks         WHERE id=?", (d["id"],))
    return {"ok": True}


ROUTES = {"create": api_create, "update": api_update, "delete": api_delete,
          "comment": api_comment, "uncomment": api_uncomment}

def _config_js():
    return (f"const AGENTS={json.dumps(AGENTS)},HUMANS={json.dumps(HUMANS)},"
            f"OWNERS={json.dumps(ALL_OWNERS)},ME={json.dumps(HUMANS[0])};")


# --------------------------------------------------------------------------- UI
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Goals MCP Project Board</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--line:#2a2f3a;--tx:#dfe3ea;--dim:#8b93a3;
      --agent:#4aa3df;--human:#d98a4a;--accent:#5ad18a;--warn:#e0685f;--work:#b98ad9;
      --input:#12151b;--pillon:#1b2a38;--pillontx:#fff;--shadow:rgba(0,0,0,.4);
      --primbg:#1b3a2a;--primtx:#bff5d6;--dragbg:#141d18;--blkline:#4a2a2a;
      --cmline:#2a4a38;--wkline:#3d2a4a;--duetx:#e8c56a;--dueline:#4a412a;--backdrop:#000b}
/* Light is re-picked for contrast on a pale background, not inverted from the dark set. */
html[data-theme=light]{--bg:#f3f5f9;--panel:#fff;--panel2:#eaeef5;--line:#ccd4e0;--tx:#1b2230;
      --dim:#5a6474;--agent:#1a6ba8;--human:#9c5411;--accent:#177f4a;--warn:#b3261e;--work:#6b3fa0;
      --input:#fff;--pillon:#dce9f6;--pillontx:#0d3d63;--shadow:rgba(16,24,40,.14);
      --primbg:#d7efe1;--primtx:#0d5232;--dragbg:#e7f3ec;--blkline:#e3aca6;
      --cmline:#a4d5bb;--wkline:#c9b2e4;--duetx:#7d5a0d;--dueline:#e2cf99;--backdrop:rgba(16,24,40,.45)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:10px;align-items:center;padding:10px 14px;background:var(--panel);
       border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600}
.pill{background:var(--panel2);border:1px solid var(--line);color:var(--dim);border-radius:999px;
      padding:3px 10px;cursor:pointer;font-size:12px;user-select:none}
.pill.on{color:var(--pillontx);border-color:var(--agent);background:var(--pillon)}
.pill.icon{font-size:15px;line-height:1;padding:4px 9px}
.pill.icon:hover{border-color:var(--agent);color:var(--tx)}
button{background:var(--panel2);color:var(--tx);border:1px solid var(--line);border-radius:6px;
       padding:6px 11px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--agent)}
button.primary{background:var(--primbg);border-color:var(--accent);color:var(--primtx)}
input,select,textarea{background:var(--input);color:var(--tx);border:1px solid var(--line);
       border-radius:6px;padding:7px 9px;font:inherit;width:100%}
.lane{padding:6px 14px 0}
.lane h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);
         margin:12px 0 6px;display:flex;gap:8px;align-items:center}
.lane h2 .c{background:var(--panel2);border-radius:999px;padding:1px 8px;font-size:10px}
.cols{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;align-items:start}
.col{background:var(--panel);border:1px solid var(--line);border-radius:10px;min-height:70px;padding:8px}
.col h3{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);
        margin:2px 0 8px;display:flex;justify-content:space-between}
.col.drag{border-color:var(--accent);background:var(--dragbg)}
.card{background:var(--panel2);border:1px solid var(--line);border-left-width:3px;border-radius:8px;
      padding:8px 9px;margin-bottom:7px;cursor:grab}
.card.own-agent{border-left-color:var(--agent)} .card.own-human{border-left-color:var(--human)}
.card .t{font-weight:500;margin-bottom:4px;word-wrap:break-word}
.meta{display:flex;gap:5px;flex-wrap:wrap;align-items:center;font-size:11px;color:var(--dim)}
.tag{background:var(--input);border:1px solid var(--line);border-radius:4px;padding:1px 6px}
.tag.blk{color:var(--warn);border-color:var(--blkline)}
.tag.cm{color:var(--accent);border-color:var(--cmline)}
.tag.wk{color:var(--work);border-color:var(--wkline)}
.tag.due{color:var(--duetx);border-color:var(--dueline)}
.det{color:var(--dim);font-size:12px;margin:3px 0 5px;white-space:pre-wrap}
.x{margin-left:auto;color:var(--dim);cursor:pointer;padding:0 3px}.x:hover{color:var(--warn)}
#goals{padding:14px}
#goals h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin:8px 0}
.goal{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 11px;margin-bottom:7px}
.goal .n{color:var(--dim);font-size:12px;margin-top:4px}
dialog{background:var(--panel);color:var(--tx);border:1px solid var(--line);border-radius:12px;
       padding:16px;width:min(640px,94vw);max-height:90vh}
dialog::backdrop{background:var(--backdrop)}
label{display:block;font-size:11px;color:var(--dim);margin:8px 0 3px;text-transform:uppercase;letter-spacing:.5px}
.row{display:flex;gap:8px}.row>*{flex:1}
.thread{max-height:230px;overflow:auto;margin-top:6px;border-top:1px solid var(--line);padding-top:8px}
.cm{background:var(--input);border:1px solid var(--line);border-left:3px solid var(--line);
    border-radius:6px;padding:7px 9px;margin-bottom:6px;font-size:13px}
.cm.work{border-left-color:var(--work)} .cm.feedback{border-left-color:var(--human)}
.cm.note{border-left-color:var(--agent)}
.cm .h{font-size:11px;color:var(--dim);display:flex;gap:6px;margin-bottom:3px}
.cm .b{white-space:pre-wrap}
.ev{font-size:11px;color:var(--dim);padding:2px 0}
@media(max-width:900px){.cols{grid-template-columns:1fr}}
</style>
<script>
(function(){var t=localStorage.getItem('board-theme');
 if(!t)t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';
 document.documentElement.setAttribute('data-theme',t);
 document.addEventListener('DOMContentLoaded',function(){paintTheme(t);});})();
function paintTheme(t){var e=document.getElementById('themetog');if(!e)return;
 var to=t==='light'?'dark':'light';
 e.textContent=to==='dark'?'☾':'☀';
 e.title='Switch to '+to+' mode';e.setAttribute('aria-label',e.title);}
function toggleTheme(){var h=document.documentElement,
 t=h.getAttribute('data-theme')==='light'?'dark':'light';
 h.setAttribute('data-theme',t);localStorage.setItem('board-theme',t);paintTheme(t);}
</script></head><body>
<header>
  <h1>Board</h1>
  <input id="q" oninput="setQ(this.value)" placeholder="search id or title…"
    style="background:var(--input);border:1px solid var(--line);border-radius:4px;padding:4px 8px;color:inherit;font-size:12px;width:200px">
  <span id="filters" style="display:flex;gap:5px;flex-wrap:wrap"></span>
  <span style="margin-left:auto;display:flex;gap:7px;align-items:center">
    <span class="pill icon" id="themetog" role="button" tabindex="0"
      onclick="toggleTheme()" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleTheme();}">☾</span>
    <span class="pill" id="lanetog" onclick="toggleLanes()">swimlanes</span>
    <span class="pill" id="donetog" onclick="toggleDone()">hide done</span>
    <button onclick="openNew()" class="primary">+ Task</button>
  </span>
</header>
<div id="board"></div>
<div id="goals"></div>

<dialog id="dlg"><div id="dlgbody"></div></dialog>

<script>
let S={tasks:[],goals:[],projects:[],columns:[]},FILTER=null,LANES=true,HIDEDONE=false,OPEN=null,QUERY='';
function setQ(v){QUERY=v.trim().toLowerCase();render()}
// Accepts a full "task <uuid>"/"goal <uuid>" paste (what copyId produces) as well as a
// bare id fragment or a plain-text title search — strip the type word if present, then
// substring-match against the id and the title/description.
function qMatch(id,text){
  if(!QUERY)return true;
  const q=QUERY.replace(/^(task|goal)\s+/,'');
  return id.toLowerCase().includes(q)||(text||'').toLowerCase().includes(q);
}
/*__CONFIG__*/

async function api(a,b){const r=await fetch('/api/'+a,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json()}
async function load(){const r=await fetch('/api/state');S=await r.json();render();if(OPEN)renderDialog(OPEN)}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function copyId(id,el,ev,kind){
  if(ev)ev.stopPropagation(); // sits inside a clickable card/goal row; don't also trigger its onclick
  // Copy "goal <uuid>" / "task <uuid>", not a bare id. A bare hex string means
  // nothing to Agent in a future session with no memory of THIS conversation —
  // the type has to travel WITH the id, not live only in the chat context around it.
  const text=(kind||'id')+' '+id;
  const done=()=>{const old=el.textContent;el.textContent='copied!';setTimeout(()=>el.textContent=old,900)};
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done);return}
  // plain-HTTP localhost has no Clipboard API — fall back to a hidden textarea + execCommand
  const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy')}catch(e){}
  document.body.removeChild(ta);done();
}
function toggleLanes(){LANES=!LANES;render()}
function toggleDone(){HIDEDONE=!HIDEDONE;render()}
function setF(p){FILTER=p;render()}

function render(){
  document.getElementById('lanetog').className='pill'+(LANES?' on':'');
  document.getElementById('donetog').className='pill'+(HIDEDONE?' on':'');
  const f=document.getElementById('filters');
  f.innerHTML=`<span class="pill ${FILTER?'':'on'}" onclick="setF(null)">all</span>`+
    S.projects.map(p=>{const n=S.tasks.filter(t=>t.project===p&&t.status!=='done').length;
      return n||FILTER===p?`<span class="pill ${FILTER===p?'on':''}" onclick="setF('${p}')">${p} ${n}</span>`:''}).join('');
  const vis=S.tasks.filter(t=>(!FILTER||t.project===FILTER)&&!(HIDEDONE&&t.status==='done')&&qMatch(t.id,t.title));
  const b=document.getElementById('board');b.innerHTML='';
  const groups=LANES?[...new Set(vis.map(t=>t.project))].sort():[null];
  for(const g of groups){
    const items=vis.filter(t=>g===null||t.project===g);
    const lane=document.createElement('div');lane.className='lane';
    lane.innerHTML=(g?`<h2>${g}<span class="c">${items.filter(t=>t.status!=='done').length} open</span></h2>`:'')+
      `<div class="cols">`+S.columns.map(([k,l])=>{
        const its=items.filter(t=>t.status===k&&!t.parent_task_id);
        return `<div class="col" data-s="${k}"><h3><span>${l}</span><span>${its.length}</span></h3>${its.map(card).join('')}</div>`
      }).join('')+`</div>`;
    b.appendChild(lane);
  }
  b.querySelectorAll('.col').forEach(col=>{
    col.ondragover=e=>{e.preventDefault();col.classList.add('drag')};
    col.ondragleave=()=>col.classList.remove('drag');
    col.ondrop=async e=>{e.preventDefault();col.classList.remove('drag');
      const id=e.dataTransfer.getData('id');if(!id)return;
      const r=await api('update',{id,status:col.dataset.s,actor:ME});
      if(r.spawned)alert('Auto-created follow-on: '+r.spawned);
      load()};
  });
  const gs=S.goals.filter(x=>(!FILTER||x.project===FILTER)&&qMatch(x.id,x.description));
  document.getElementById('goals').innerHTML='<h2>Active goals</h2>'+(gs.length?gs.map(x=>`
    <div class="goal"><div><b>${esc(x.description)}</b></div>
    <div class="meta"><span class="tag copyid" title="Click to copy — pastes as 'goal &lt;id&gt;'" onclick="copyId('${x.id}',this,event,'goal')">id ${x.id.slice(0,8)}</span>
      <span class="tag">${x.category||''}</span>
      <span class="tag">owner ${x.owner}</span><span class="tag">decides ${x.decision_owner||x.owner}</span>
      <span class="tag">p${(+x.priority).toFixed(2)}</span>${x.blocked_by?'<span class="tag blk">blocked</span>':''}</div>
    ${x.next_action?`<div class="n"><b>next:</b> ${esc(x.next_action)}</div>`:''}</div>`).join('')
    :'<div class="goal"><div class="n">none</div></div>');
}

function card(t){
  const nc=t.comments.length, wm=t.work_minutes;
  const overdue=t.due_date && t.status!=='done' && t.due_date < new Date().toISOString().slice(0,10);
  return `<div class="card ${AGENTS.includes(t.owner)?'own-agent':'own-human'}" draggable="true"
     ondragstart="event.dataTransfer.setData('id','${t.id}')" onclick="openTask('${t.id}')">
    <div class="t">${esc(t.title)}</div>
    ${t.detail?`<div class="det">${esc(t.detail).slice(0,140)}${t.detail.length>140?'…':''}</div>`:''}
    <div class="meta"><span class="tag copyid" title="Click to copy — pastes as 'task &lt;id&gt;'" onclick="copyId('${t.id}',this,event,'task')">id ${t.id.slice(0,8)}</span>
      ${LANES?'':`<span class="tag">${t.project}</span>`}
      <span class="tag">${t.owner}</span>
      ${t.due_date?`<span class="tag due">${overdue?'⚠ ':''}${t.due_date}</span>`:''}
      ${t.sub_total?`<span class="tag ${t.sub_blocked?'blk':(t.sub_done===t.sub_total?'cm':'')}">${t.sub_done}/${t.sub_total}${t.sub_blocked?' ⚠':''}</span>`:''}
      ${nc?`<span class="tag cm">${nc} 💬</span>`:''}
      ${wm?`<span class="tag wk">${wm}m</span>`:''}
      ${t.blocked_on?`<span class="tag blk">${esc(t.blocked_on)}</span>`:''}
    </div></div>`;
}

function openTask(id){OPEN=id;renderDialog(id);if(!dlg.open)dlg.showModal()}
function renderDialog(id){
  const t=S.tasks.find(x=>x.id===id);if(!t){dlg.close();OPEN=null;return}
  const opt=(a,v)=>a.map(x=>`<option ${x===v?'selected':''}>${x}</option>`).join('');
  document.getElementById('dlgbody').innerHTML=`
    <label>Title</label><input id="e_title" value="${esc(t.title).replace(/"/g,'&quot;')}">
    <label>Detail</label><textarea id="e_detail" rows="3">${esc(t.detail||'')}</textarea>
    <div class="row">
      <div><label>Project</label><select id="e_project">${opt(S.projects,t.project)}</select></div>
      <div><label>Owner</label><select id="e_owner">${opt(OWNERS,t.owner)}</select></div>
      <div><label>Status</label><select id="e_status">${S.columns.map(([k,l])=>`<option value="${k}" ${k===t.status?'selected':''}>${l}</option>`).join('')}</select></div>
    </div>
    <div class="row">
      <div><label>Blocked on</label><input id="e_blocked" value="${esc(t.blocked_on||'')}"></div>
      <div><label>Due</label><input id="e_due" type="date" value="${t.due_date||''}"></div>
    </div>
    <label>When done, auto-create</label><input id="e_spawn" placeholder="e.g. User testing on the PIN gate" value="${esc(t.on_done_create||'')}">
    <div style="display:flex;gap:8px;margin:14px 0 4px">
      <button class="primary" onclick="saveTask('${t.id}')">Save</button>
      <button onclick="dlg.close();OPEN=null">Close</button>
      <button style="margin-left:auto;border-color:var(--blkline)" onclick="delTask('${t.id}')">Delete task</button>
    </div>

    <label>Subtasks ${t.sub_total?`(${t.sub_done}/${t.sub_total}${t.sub_blocked?', '+t.sub_blocked+' blocked':''})`:''}</label>
    <div id="subs">${t.subtasks.map(k=>`
      <div class="cm ${k.status==='blocked'?'feedback':'note'}" style="display:flex;gap:8px;align-items:center;padding:5px 8px">
        <input type="checkbox" style="width:auto" ${k.status==='done'?'checked':''} onchange="subToggle('${k.id}',this.checked)">
        <span style="flex:1;${k.status==='done'?'opacity:.55;text-decoration:line-through':''}">${esc(k.title)}</span>
        <span class="tag">${k.owner}</span>
        ${k.status==='blocked'?`<span class="tag blk">blocked</span>`:''}
        <span class="x" onclick="delTask('${k.id}',true)">&times;</span></div>`).join('')||'<div class="ev">none</div>'}</div>
    <div class="row" style="margin-top:6px">
      <input id="s_title" placeholder="new subtask…" onkeydown="if(event.key==='Enter')addSub('${t.id}')">
      <select id="s_owner" style="flex:0 0 100px">${OWNERS.map(o=>'<option>'+o+'</option>').join('')}</select>
      <button style="flex:0 0 70px" onclick="addSub('${t.id}')">Add</button>
    </div>

    <label>Add note / feedback / work</label>
    <div class="row">
      <select id="c_kind" style="flex:0 0 120px">
        <option value="note">note</option><option value="feedback">feedback</option><option value="work">work</option>
      </select>
      <input id="c_min" placeholder="minutes (work)" style="flex:0 0 130px">
      <select id="c_author" style="flex:0 0 100px">${OWNERS.map(o=>'<option>'+o+'</option>').join('')}</select>
    </div>
    <textarea id="c_body" rows="2" placeholder="What happened / what you think…"></textarea>
    <div style="margin-top:6px"><button class="primary" onclick="addComment('${t.id}')">Add</button>
      ${t.work_minutes?`<span class="tag wk" style="margin-left:8px">${t.work_minutes} min logged</span>`:''}</div>

    <div class="thread">
      ${t.comments.slice().reverse().map(c=>`
        <div class="cm ${c.kind}"><div class="h"><b>${c.author}</b><span>${c.kind}</span>
          ${c.minutes?`<span>${c.minutes}m</span>`:''}<span>${(c.created_at||'').slice(0,16)}</span>
          <span class="x" onclick="delComment(${c.id})">&times;</span></div>
          <div class="b">${esc(c.body)}</div></div>`).join('') || '<div class="ev">no notes yet</div>'}
      ${t.events.length?'<div class="ev" style="margin-top:8px;opacity:.7"><b>history</b></div>':''}
      ${t.events.map(e=>`<div class="ev">${(e.created_at||'').slice(5,16)} · ${e.actor} · ${e.field}: ${esc(e.old_value||'—')} → ${esc(e.new_value||'—')}</div>`).join('')}
    </div>`;
}
async function saveTask(id){
  await api('update',{id,actor:ME,title:e_title.value,detail:e_detail.value,project:e_project.value,
    owner:e_owner.value,status:e_status.value,blocked_on:e_blocked.value,
    due_date:e_due.value||null,on_done_create:e_spawn.value});
  load();
}
async function addComment(id){
  if(!c_body.value.trim())return;
  await api('comment',{task_id:id,author:c_author.value,kind:c_kind.value,
    body:c_body.value,minutes:c_min.value});
  c_body.value='';c_min.value='';load();
}
async function delComment(cid){await api('uncomment',{id:cid});load()}
async function subToggle(id,done){await api('update',{id,status:done?'done':'todo',actor:ME});load()}
async function addSub(pid){
  const t=S.tasks.find(x=>x.id===pid); if(!s_title.value.trim())return;
  await api('create',{title:s_title.value,project:t.project,owner:s_owner.value,
                      parent_task_id:pid,actor:ME,source:'subtask'});
  s_title.value='';load();
}
async function delTask(id,isSub){if(confirm('Delete this task and its notes?')){await api('delete',{id});if(!isSub){dlg.close();OPEN=null}load()}}
function openNew(){
  OPEN=null;
  document.getElementById('dlgbody').innerHTML=`
    <label>Title</label><input id="n_title">
    <label>Detail</label><textarea id="n_detail" rows="3"></textarea>
    <div class="row">
      <div><label>Project</label><select id="n_project">${S.projects.map(p=>`<option ${p===FILTER?'selected':''}>${p}</option>`).join('')}</select></div>
      <div><label>Owner</label><select id="n_owner">${OWNERS.map(o=>'<option>'+o+'</option>').join('')}</select></div>
      <div><label>Due</label><input id="n_due" type="date"></div>
    </div>
    <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
      <button onclick="dlg.close()">Cancel</button>
      <button class="primary" onclick="createTask()">Create</button></div>`;
  dlg.showModal();
}
async function createTask(){
  await api('create',{title:n_title.value,detail:n_detail.value,project:n_project.value,
    owner:n_owner.value,due_date:n_due.value||null,actor:ME});
  dlg.close();load();
}
load();setInterval(()=>{if(!dlg.open)load()},20000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth_ok(self):
        if check_auth(self.headers.get("Authorization", "")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Board"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._auth_ok():
            return
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._send(200, PAGE.replace("/*__CONFIG__*/", _config_js()),
                              "text/html; charset=utf-8")
        if p == "/api/state":
            return self._send(200, json.dumps(api_state(), default=str))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not self._auth_ok():
            return
        p = urlparse(self.path).path.rsplit("/", 1)[-1]
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n) or b"{}")
        fn = ROUTES.get(p)
        if not fn:
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            return self._send(200, json.dumps(fn(d), default=str))
        except Exception as e:
            return self._send(500, json.dumps({"ok": False, "error": str(e)}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--host", default="127.0.0.1",
                help="loopback by default; auth is off unless BOARD_PASS is set")
    a = ap.parse_args()
    with db() as c:
        t = c.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"]
        g = c.execute("SELECT count(*) AS n FROM goals").fetchone()["n"]
    print(f"Board — {g} goals, {t} tasks — http://{a.host}:{a.port}/")
    print(f"  auth: {'ENABLED (user ' + BOARD_USER + ')' if BOARD_PASS else '** DISABLED — set BOARD_PASS **'}")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
