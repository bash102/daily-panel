"""Jinja2 templates, CSS and JS for the dashboard, kept in Python so they ship
with the package without package-data configuration. Autoescaping is on.
"""

from __future__ import annotations

CSS = r"""
:root{--bg:#fbfaf7;--fg:#222;--muted:#6b6b6b;--card:#fff;--line:#e4e1da;--accent:#2f6fdf;
--ok:#1e8a4c;--warn:#b26a00;--bad:#c0392b;--chip:#f0eee8}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e8e6e1;--muted:#a0a0a0;
--card:#202226;--line:#34363b;--accent:#7aa7ff;--ok:#4cc28a;--warn:#e0a040;--bad:#ff7a6b;
--chip:#2a2c31}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:8px 16px;
position:sticky;top:0;z-index:2}
header .brand{font-weight:700;margin-right:8px}
nav{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center}
nav a{color:var(--fg);text-decoration:none;padding:4px 0}
nav a.on{color:var(--accent);font-weight:600}
nav form{margin:0 0 0 auto}
main{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:.2rem 0 1rem}h2{font-size:1.1rem;margin:1.2rem 0 .5rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.grid .card{margin:0}
.muted{color:var(--muted)}.small{font-size:.875rem}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 8px;
font-size:.8rem;margin:0 4px 2px 0}
.chip.ok{background:transparent;border:1px solid var(--ok)}
.chip.warn{background:transparent;border:1px solid var(--warn)}
.chip.bad{background:transparent;border:1px solid var(--bad)}
.flash{border-left:4px solid var(--accent);padding:8px 12px;background:var(--card);margin:0 0 12px}
.flash.bad{border-color:var(--bad)}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.scroll{overflow-x:auto}
button,.btn{font:inherit;border:1px solid var(--line);background:var(--chip);color:var(--fg);
border-radius:8px;padding:6px 12px;cursor:pointer;text-decoration:none;display:inline-block}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{background:transparent;border-color:var(--bad);color:var(--bad)}
form.inline{display:inline}
label{display:block;margin:8px 0 2px;font-weight:600}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg);width:100%;max-width:360px}
input[type=checkbox]{width:auto}
.row{display:flex;gap:8px;flex-wrap:wrap}.row>*{flex:1 1 120px}
audio{width:100%;max-width:360px;display:block;margin:4px 0}
img.preview{max-width:100%;width:240px;border:1px solid var(--line);background:#fff;display:block;
margin:4px 0}
.bar{background:var(--chip);border-radius:6px;height:14px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--accent)}
.bar.over>span{background:var(--bad)}
.text{white-space:pre-wrap;max-height:16rem;overflow:auto;background:var(--chip);padding:8px;
border-radius:6px;font-size:.92rem}
dl{margin:0}dt{font-weight:600;margin-top:4px}dd{margin:0 0 4px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tabs a{padding:4px 10px;border-radius:999px;background:var(--chip);color:var(--fg);
text-decoration:none}.tabs a.on{background:var(--accent);color:#fff}
.login{max-width:380px;margin:10vh auto}
"""

JS = r"""
(function(){
  function fmt(s){if(s<=0)return "review window over";var m=Math.ceil(s/60),h=Math.floor(m/60);
    m=m%60;if(h&&!m)return h+" h left";return (h?h+" h ":"")+m+" min left";}
  function tick(){var els=document.querySelectorAll("[data-ends]");
    for(var i=0;i<els.length;i++){var e=els[i];var t=Date.parse(e.getAttribute("data-ends"));
      if(!isNaN(t))e.textContent=fmt((t-Date.now())/1000);}}
  tick();setInterval(tick,30000);
  document.addEventListener("submit",function(ev){var f=ev.target;
    var msg=f.getAttribute("data-confirm");if(msg&&!window.confirm(msg))ev.preventDefault();});
})();
"""

BASE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}{{ title or "Zozo box" }}{% endblock %} · Zozo box</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/app.js" defer></script>
</head><body>
{% if authed %}
<header><nav>
<span class="brand">Zozo box</span>
{% for href, name in nav %}<a href="{{ href }}" class="{{ 'on' if active == href }}">{{ name }}{% if href == '/approvals' and nav_pending %} <span class="chip warn">{{ nav_pending }}</span>{% endif %}</a>{% endfor %}
<form method="post" action="/logout"><input type="hidden" name="csrf_token" value="{{ csrf }}"><button>Log out</button></form>
</nav></header>
{% endif %}
<main>
{% if flash %}<div class="flash {{ 'bad' if flash_bad }}">{{ flash }}</div>{% endif %}
{% block body %}{% endblock %}
</main></body></html>
"""

CSRF = '<input type="hidden" name="csrf_token" value="{{ csrf }}">'

MACROS = r"""
{% macro media(rel) -%}
{% if rel %}{% set mk = rel|media_kind %}
{% if mk == 'audio' %}<audio controls preload="none" src="{{ rel|media_url }}"></audio>
{% elif mk == 'image' %}<a href="{{ rel|media_url }}"><img class="preview" loading="lazy" src="{{ rel|media_url }}" alt=""></a>
{% else %}<a href="{{ rel|media_url }}">{{ rel }}</a>{% endif %}
{% endif %}
{%- endmacro %}
{% macro content_status(item) -%}
<span class="chip {{ 'ok' if item.safety.value == 'passed' else ('bad' if item.safety.value == 'failed' else 'warn') }}">safety: {{ item.safety.value }}</span>
<span class="chip {{ 'ok' if item.approval.value in ('approved','auto') else ('bad' if item.approval.value == 'rejected' else 'warn') }}">{{ item.approval.value }}</span>
{% if item.withheld %}<span class="chip bad">withheld</span>{% endif %}
{% if item.superseded %}<span class="chip">superseded</span>{% endif %}
{% if item.served %}<span class="chip">served {{ item.served_day }}</span>{% endif %}
{% if item.completed_at %}<span class="chip ok">heard</span>{% endif %}
{%- endmacro %}
{% macro meta_list(meta) -%}
<dl>{% for k, v in meta.items() %}{% if v not in (None, '', [], {}) %}
<dt>{{ k }}</dt><dd>{% if v is string %}{% if v|length > 120 %}<div class="text">{{ v }}</div>{% else %}{{ v }}{% endif %}{% else %}<div class="text">{{ v|tojson_pretty }}</div>{% endif %}</dd>
{% endif %}{% endfor %}</dl>
{%- endmacro %}
{% macro rec_status(r) -%}
<span class="chip {{ 'ok' if r.status.value == 'live' else ('bad' if r.status.value == 'withheld' else '') }}">{{ r.status.value }}</span>
{% if r.heard_at %}<span class="chip">heard {{ r.heard_at|local }}</span>{% endif %}
{% if r.published_at %}<span class="chip ok">published {{ r.published_at|local }}</span>{% endif %}
{%- endmacro %}
"""

SETUP = r"""{% extends "base.html" %}{% block body %}
<div class="login card"><h1>Welcome</h1>
<p>Choose the parent password for this dashboard. You'll need it on every device.</p>
{% if error %}<div class="flash bad">{{ error }}</div>{% endif %}
<form method="post" action="/setup">""" + CSRF + r"""
<input type="hidden" name="setup_token" value="{{ setup_token or '' }}">
<label for="pw">Password</label><input id="pw" type="password" name="password" autocomplete="new-password" required minlength="8">
<label for="pw2">Repeat it</label><input id="pw2" type="password" name="confirm" autocomplete="new-password" required minlength="8">
<p><button class="primary">Set password</button></p></form></div>
{% endblock %}"""

SETUP_LOCKED = r"""{% extends "base.html" %}{% block body %}
<div class="login card"><h1>Welcome</h1>
<p>Setting the first parent password needs the setup link. On the box, run
<code>sudo cat /var/lib/zozobox/dashboard-setup-token</code> (or look in
<code>journalctl -u zozobox-dashboard</code>) and open
<code>/setup?token=&lt;token&gt;</code> on this address.</p></div>
{% endblock %}"""

LOGIN = r"""{% extends "base.html" %}{% block body %}
<div class="login card"><h1>Zozo box</h1>
{% if error %}<div class="flash bad">{{ error }}</div>{% endif %}
<form method="post" action="/login">""" + CSRF + r"""
<input type="hidden" name="next" value="{{ next }}">
<label for="pw">Parent password</label><input id="pw" type="password" name="password" autocomplete="current-password" required autofocus>
<p><button class="primary">Log in</button></p></form></div>
{% endblock %}"""

HOME = r"""{% extends "base.html" %}{% import "macros.html" as m %}{% block body %}
<h1>Today{% if h.today %} · {{ h.today.strftime('%a %b %d') }}{% endif %}
{% if h.quiet %}<span class="chip">quiet hours</span>{% endif %}</h1>
{% if h.spend and h.spend.over %}<div class="flash bad">Monthly spend cap reached: new content is paused and Zozo gets cached and evergreen content.</div>{% endif %}
{% if h.pending_approvals %}<div class="flash">{{ h.pending_approvals }} item(s) waiting for <a href="/approvals">approval</a>.</div>{% endif %}
<div class="grid">
<div class="card"><h2>Today's content</h2>
{% for row in h.content %}<div><b>{{ row.kind|capitalize }}</b>:
{% if row.item %}{{ row.item.meta.get('title') or row.item.meta.get('letter') or row.item.meta.get('summary') or '' }}
{{ m.content_status(row.item) }}
{% else %}<span class="muted">nothing ready</span>{% endif %}</div>{% else %}<p class="muted">Unavailable.</p>{% endfor %}
</div>
<div class="card"><h2>Print rotation</h2>
{% if h.snapshot %}<p>Next up: <b>{{ h.snapshot.print_item }}</b></p>
{% for it in h.rotation %}<span class="chip {{ 'ok' if it in h.snapshot.printed }}">{{ it }}{{ ' ✓' if it in h.snapshot.printed }}</span>{% endfor %}
<p class="small muted">✓ = printed today</p>
<p>Story heard today: {{ 'yes' if h.snapshot.story_heard else 'not yet' }}</p>
{% else %}<p class="muted">Unavailable.</p>{% endif %}
</div>
<div class="card"><h2>Messages</h2>
<p>Grandma: {% if h.grandma_live %}{{ 'new, not heard yet' if h.grandma_unheard else 'heard' }}{% else %}<span class="muted">no message</span>{% endif %}</p>
<p>Zozo: {% if h.zozo_review %}{% if h.zozo_review.in_review %}in review, <span data-ends="{{ h.zozo_review.ends_at.isoformat() }}">{{ h.zozo_review.remaining_s|remaining }}</span>{% if h.zozo_review.paused %} (paused for quiet hours){% endif %}{% elif h.zozo_review.published %}published to Grandma{% elif h.zozo_review.awaiting_publish %}review over, waiting for the cloud to publish{% else %}{{ h.zozo_review.rec.status.value }}{% endif %}{% else %}<span class="muted">no message</span>{% endif %}</p>
<a class="btn" href="/recordings">Recordings</a>
</div>
<div class="card"><h2>Spend this month</h2>
{% if h.spend %}<p>${{ '%.2f'|format(h.spend.month_to_date_usd) }} of ${{ '%.2f'|format(h.spend.cap_usd) }}</p>
<div class="bar {{ 'over' if h.spend.over }}"><span style="width:{{ (h.spend.fraction*100)|round|int }}%"></span></div>
{% for p, v in h.spend.by_provider.items() %}<span class="chip">{{ p }} ${{ '%.2f'|format(v) }}</span>{% endfor %}
{% else %}<p class="muted">Unavailable.</p>{% endif %}
</div>
<div class="card"><h2>Sync</h2>
{% if h.sync %}{% if h.sync.never %}<p class="warn">The sync client hasn't reported yet. Changes like withholding Zozo's message reach the cloud only once it runs.</p>
{% else %}<p>{% if h.sync.online is sameas false %}<span class="chip bad">offline</span>{% elif h.sync.online %}<span class="chip ok">online</span>{% endif %}
Last good sync: {% if h.sync.last_ok_at %}{{ h.sync.last_ok_at|local }}{% else %}never{% endif %}{% if h.sync.last_run_at %} <span class="small muted">(last try {{ h.sync.last_run_at|local }})</span>{% endif %}</p>
{% if h.sync.stale %}<p class="warn">No successful sync for a while: the cloud isn't getting updates (e.g. withheld messages) until it reconnects.</p>{% endif %}
{% if h.sync.last_error %}<p class="warn small">{{ h.sync.last_error[:300] }}</p>{% endif %}{% endif %}
{% else %}<p class="muted">Sync status unavailable.</p>{% endif %}
{% if h.outbox %}<p>{{ h.outbox.pending }} waiting · {{ h.outbox.inflight }} uploading · {{ h.outbox.done }} done</p>
{% if h.outbox.failing %}<p class="warn">{{ h.outbox.failing }} retrying{% if h.outbox.last_error %}: {{ h.outbox.last_error[:200] }}{% endif %}</p>{% endif %}
{% if h.outbox.oldest_pending_at %}<p class="small muted">Oldest waiting since {{ h.outbox.oldest_pending_at|local }}</p>{% endif %}
{% else %}<p class="muted">Unavailable.</p>{% endif %}
</div>
<div class="card"><h2>Disk</h2>
{% if h.disk %}<p>Media {{ h.disk.media_bytes|size }} · DB {{ h.disk.db_bytes|size }}</p>
<p class="{{ 'bad' if h.disk.free_bytes < 1073741824 else '' }}">Free {{ h.disk.free_bytes|size }}</p>
{% else %}<p class="muted">Unavailable.</p>{% endif %}
</div>
</div>
<h2>Safety checks</h2>
<div class="card">{% if h.safety_events or h.safety_items %}
{% if h.safety_items %}<h3>Items that failed the check (never served)</h3>
<ul>{% for i in h.safety_items %}<li>{{ i.kind.value|capitalize }}: {{ i.meta.get('title') or i.meta.get('letter') or i.meta.get('summary') or i.id }} <span class="small muted">{{ i.created_at|local }}</span>{% if i.safety_note %}: {{ i.safety_note }}{% endif %}
 <a href="/archive/item/{{ i.id }}">view</a></li>{% endfor %}</ul>{% endif %}
{% if h.safety_events %}<h3>Failed attempts (last 14 days)</h3>
<ul>{% for e in h.safety_events %}<li>{{ e.ts|local }}: {{ e.payload.get('kind') }} failed at {{ e.payload.get('stage') }} (attempt {{ e.payload.get('attempt') }}){% if e.payload.get('reasons') %}: {{ e.payload.get('reasons')|join('; ') }}{% endif %}
{% if e.payload.get('item_id') %} <a href="/archive/item/{{ e.payload.get('item_id') }}">view</a>{% endif %}</li>{% endfor %}</ul>{% endif %}
{% else %}<p class="ok">No safety failures.</p>{% endif %}</div>
{% if h.errors %}<h2>Errors (last 24 h)</h2><div class="card"><ul>
{% for e in h.errors %}<li>{{ e.ts|local }}: {{ e.payload.get('kind') }} {{ e.payload.get('item') or '' }} {{ (e.payload.get('error') or '')[:200] }}</li>{% endfor %}</ul></div>{% endif %}
{% endblock %}"""

APPROVALS = r"""{% extends "base.html" %}{% import "macros.html" as m %}{% block body %}
<h1>Approvals</h1>
{% if approval_until %}<p class="muted small">Manual approval is required until {{ approval_until }} (change it in <a href="/settings">Settings</a>).</p>{% endif %}
{% for item in items %}<div class="card" id="item-{{ item.id }}">
<h2>{{ item.kind.value|capitalize }} <span class="small muted">{{ item.created_at|local }}{% if item.for_date %} · for {{ item.for_date }}{% endif %}</span></h2>
{{ m.content_status(item) }}
{% if item.safety_note %}<p class="warn small">{{ item.safety_note }}</p>{% endif %}
{{ m.meta_list(item.meta) }}
{% for key, rel in item.media.items() %}<div><span class="small muted">{{ key }}</span>{{ m.media(rel) }}</div>{% endfor %}
<div class="row" style="margin-top:8px">
<form class="inline" method="post" action="/approvals/{{ item.id }}/approve">""" + CSRF + r"""<button class="primary" {{ 'disabled' if item.safety.value == 'failed' }}>Approve</button></form>
<form class="inline" method="post" action="/approvals/{{ item.id }}/reject">""" + CSRF + r"""<button class="danger">Reject</button></form>
</div></div>
{% else %}<p class="muted">Nothing is waiting for approval.</p>{% endfor %}
{% endblock %}"""

ARCHIVE = r"""{% extends "base.html" %}{% import "macros.html" as m %}{% block body %}
<h1>Archive</h1>
<div class="tabs">{% for k in kinds %}<a href="/archive?kind={{ k }}{% if day %}&day={{ day }}{% endif %}" class="{{ 'on' if k == kind }}">{{ k }}</a>{% endfor %}</div>
<form method="get" action="/archive" class="row"><input type="hidden" name="kind" value="{{ kind }}">
<div><label for="day">Day</label><input id="day" type="date" name="day" value="{{ day or '' }}"></div>
<div style="align-self:end"><button>Show</button> {% if day %}<a class="btn" href="/archive?kind={{ kind }}">All days</a>{% endif %}</div></form>
<p></p>
{% if kind in content_kinds %}
{% for item in items %}<div class="card"><b><a href="/archive/item/{{ item.id }}">{{ item.meta.get('title') or item.meta.get('letter') or item.meta.get('key') or item.meta.get('summary') or item.kind.value }}</a></b>
<span class="small muted">{{ item.created_at|local }}{% if item.for_date %} · for {{ item.for_date }}{% endif %}</span><br>{{ m.content_status(item) }}</div>
{% else %}<p class="muted">Nothing here.</p>{% endfor %}
{% elif kind == 'prints' %}
<div class="grid">{% for e in items %}<div class="card"><b>{{ e.payload.get('item') }}</b>{% if e.payload.get('reprint') %} <span class="chip">reprint</span>{% endif %}
<div class="small muted">{{ e.ts|local }}</div>{{ m.media(e.media) }}
{% if e.media %}<form method="post" action="/reprint/{{ e.id }}">""" + CSRF + r"""<button>Reprint</button></form>{% endif %}</div>
{% else %}<p class="muted">No prints.</p>{% endfor %}</div>
{% elif kind == 'recordings' %}
{% for r in items %}<div class="card"><b>{{ 'Zozo' if r.slot.value == 'zozo_message' else 'Grandma' }}</b> <span class="small muted">{{ r.created_at|local }}{% if r.duration_s %} · {{ r.duration_s|round(1) }} s{% endif %}</span>
{{ m.rec_status(r) }}{{ m.media(r.media) }}
<details><summary class="small">Purge permanently…</summary>
<form method="post" action="/archive/purge">""" + CSRF + r"""<input type="hidden" name="target" value="recording"><input type="hidden" name="item_id" value="{{ r.id }}">
<label>Type PURGE to delete this recording from the box forever</label><input name="confirm" autocomplete="off"><p><button class="danger">Purge</button></p></form></details>
</div>{% else %}<p class="muted">No recordings.</p>{% endfor %}
{% elif kind == 'mood' %}
<div class="scroll"><table><tr><th>When</th><th>Label</th><th>Transcript</th><th>Audio</th></tr>
{% for e in items %}<tr><td>{{ e.ts|local }}</td><td>{{ e.payload.get('label') if e.type == 'mood' else 'pending' }}</td><td>{{ e.payload.get('transcript') or '' }}</td><td>{{ m.media(e.media) }}</td></tr>
{% else %}<tr><td colspan="4" class="muted">No mood entries.</td></tr>{% endfor %}</table></div>
{% endif %}
{% endblock %}"""

ITEM = r"""{% extends "base.html" %}{% import "macros.html" as m %}{% block body %}
<p><a href="/archive?kind={{ item.kind.value }}">← {{ item.kind.value }}</a></p>
<h1>{{ item.kind.value|capitalize }} <span class="small muted">{{ item.created_at|local }}</span></h1>
<div class="card">{{ m.content_status(item) }}
{% if item.safety_note %}<p class="warn small">{{ item.safety_note }}</p>{% endif %}
{{ m.meta_list(item.meta) }}
{% for key, rel in item.media.items() %}<div><span class="small muted">{{ key }}</span>{{ m.media(rel) }}</div>{% endfor %}
</div>
<div class="card"><h2>Purge permanently</h2>
<p class="small">Deletes this item and its files from the box. This can't be undone. (Withholding it instead keeps it in the archive.)</p>
<form method="post" action="/archive/purge">""" + CSRF + r"""<input type="hidden" name="target" value="content"><input type="hidden" name="item_id" value="{{ item.id }}">
<label for="confirm">Type PURGE to confirm</label><input id="confirm" name="confirm" autocomplete="off"><p><button class="danger">Purge</button></p></form></div>
{% endblock %}"""

RECORDINGS = r"""{% extends "base.html" %}{% import "macros.html" as m %}{% block body %}
<h1>Recordings</h1>
<h2>Zozo's message to Grandma</h2>
{% if zozo %}<div class="card">
<div class="small muted">Recorded {{ zozo.rec.created_at|local }}{% if zozo.rec.duration_s %} · {{ zozo.rec.duration_s|round(1) }} s{% endif %}</div>
{{ m.media(zozo.rec.media) }}
{% if zozo.in_review %}<p><b>In review{% if zozo.paused %} (paused for quiet hours){% endif %}:</b> <span data-ends="{{ zozo.ends_at.isoformat() }}">{{ zozo.remaining_s|remaining }}</span>. Grandma gets it around {{ zozo.ends_at|local }} unless you withhold it. <span class="small muted">(The review clock stops during quiet hours.)</span></p>
{% elif zozo.published %}<p>Published to Grandma's line {{ zozo.rec.published_at|local }}. Withholding now unpublishes it.</p>
{% else %}<p>The review window is over but the cloud hasn't reported publishing it yet (it publishes outside quiet hours, once the box has synced). You can still withhold it.</p>{% endif %}
<form method="post" action="/recordings/{{ zozo.rec.id }}/withhold" data-confirm="Withhold this message? Grandma won't get it.">""" + CSRF + r"""<button class="danger">Withhold</button></form>
</div>{% else %}<p class="muted">No live message from Zozo.</p>{% endif %}
<h2>Grandma's message</h2>
{% if grandma %}<div class="card"><div class="small muted">Received {{ grandma.created_at|local }}</div>
{{ m.rec_status(grandma) }}{{ m.media(grandma.media) }}
<form method="post" action="/recordings/{{ grandma.id }}/withhold" data-confirm="Remove this message from the box? Zozo won't hear it.">""" + CSRF + r"""<button class="danger">Withhold</button></form>
</div>{% else %}<p class="muted">No live message from Grandma.</p>{% endif %}
<h2>History</h2>
<div class="scroll"><table><tr><th>When</th><th>Who</th><th>Status</th><th>Listen</th><th></th></tr>
{% for r in history %}<tr><td>{{ r.created_at|local }}</td><td>{{ 'Zozo' if r.slot.value == 'zozo_message' else 'Grandma' }}</td><td>{{ m.rec_status(r) }}</td><td>{{ m.media(r.media) }}</td>
<td>{% if r.status.value != 'withheld' %}<form method="post" action="/recordings/{{ r.id }}/withhold" data-confirm="Withhold this recording?">""" + CSRF + r"""<button class="danger">Withhold</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="5" class="muted">No recordings yet.</td></tr>{% endfor %}</table></div>
{% endblock %}"""

MOOD = r"""{% extends "base.html" %}{% import "macros.html" as m %}{% block body %}
<h1>Mood log</h1>
<div class="card"><h2>Last {{ days }} days (first mood of each day)</h2>
{% for label, n, pct in summary %}<div class="row" style="align-items:center"><div style="flex:0 0 80px">{{ label }}</div>
<div class="bar" style="flex:1 1 auto"><span style="width:{{ pct }}%"></span></div><div style="flex:0 0 60px;text-align:right">{{ n }}</div></div>{% endfor %}
</div>
<div class="scroll"><table><tr><th>Day</th><th>Today's mood</th><th>All answers</th></tr>
{% for d in rows %}<tr><td>{{ d.day.strftime('%a %b %d') }}</td>
<td>{% if d.first %}<b>{{ d.first.payload.get('label') }}</b>{% if d.first_recognized and d.first_recognized.id != d.first.id %}<div class="small muted">first recognized: {{ d.first_recognized.payload.get('label') }}</div>{% endif %}{% else %}<span class="muted">—</span>{% endif %}</td>
<td>{% for e in d.entries %}<div><span class="small muted">{{ e.ts|local('%H:%M') }}</span> {{ e.payload.get('label') }} <span class="small muted">({{ e.payload.get('source') }})</span>
{% if e.payload.get('transcript') %}<div class="small">“{{ e.payload.get('transcript') }}”</div>{% endif %}{{ m.media(e.media) }}</div>{% endfor %}
{% for e in d.pending %}<div><span class="small muted">{{ e.ts|local('%H:%M') }}</span> <span class="chip warn">waiting for recognition</span>{{ m.media(e.media) }}</div>{% endfor %}</td></tr>
{% else %}<tr><td colspan="3" class="muted">No moods logged yet.</td></tr>{% endfor %}</table></div>
{% endblock %}"""

STATS = r"""{% extends "base.html" %}{% block body %}
<h1>Stats <span class="small muted">last {{ days }} days</span></h1>
<div class="scroll"><table><tr><th>Day</th>{% for c in cols %}<th>{{ c }}</th>{% endfor %}<th>quiet presses</th><th>story heard</th><th>prints</th><th>reprints</th></tr>
{% for r in rows %}<tr><td>{{ r.day.strftime('%a %b %d') }}</td>{% for c in cols %}<td>{{ r.presses[c] }}</td>{% endfor %}
<td>{{ r.quiet_presses }}</td><td>{{ '✓' if r.story_heard else '' }}</td><td>{{ r.prints }}</td><td>{{ r.reprints }}</td></tr>{% endfor %}
</table></div>
<p class="small muted">Presses count accepted presses per button role (ignored re-presses excluded).</p>
{% endblock %}"""

REPRINT = r"""{% extends "base.html" %}{% import "macros.html" as m %}{% block body %}
<h1>Reprint</h1>
<p class="small muted">Reprints don't count toward the daily print limit.{% if quiet %} <b>It's quiet hours</b>: tick “print anyway” to print now.{% endif %}</p>
<form method="get" action="/reprint" class="row"><div><label for="day">Day</label><input id="day" type="date" name="day" value="{{ day or '' }}"></div>
<div style="align-self:end"><button>Show</button></div></form><p></p>
<div class="grid">{% for e in items %}<div class="card"><b>{{ e.payload.get('item') }}</b>{% if e.payload.get('reprint') %} <span class="chip">reprint</span>{% endif %}
<div class="small muted">{{ e.ts|local }}</div>{{ m.media(e.media) }}
<form method="post" action="/reprint/{{ e.id }}">""" + CSRF + r"""{% if quiet %}<label class="small"><input type="checkbox" name="force" value="1"> print anyway</label>{% endif %}<button class="primary">Reprint</button></form></div>
{% else %}<p class="muted">No prints yet.</p>{% endfor %}</div>
{% endblock %}"""

SETTINGS = r"""{% extends "base.html" %}{% block body %}
<h1>Settings</h1>
{% if errors %}<div class="flash bad"><ul>{% for e in errors %}<li>{{ e }}</li>{% endfor %}</ul></div>{% endif %}
<form method="post" action="/settings" class="card">""" + CSRF + r"""
<h2>Quiet hours</h2><div class="row">
<div><label for="qs">Start</label><input id="qs" type="time" name="quiet_start" value="{{ f.quiet_start }}" required></div>
<div><label for="qe">End</label><input id="qe" type="time" name="quiet_end" value="{{ f.quiet_end }}" required></div></div>
<label for="tz">Time zone</label><select id="tz" name="timezone">{% for z in zones %}<option {{ 'selected' if z == f.timezone }}>{{ z }}</option>{% endfor %}</select>
<label for="ds">Day starts at</label><input id="ds" type="time" name="day_start" value="{{ f.day_start }}" required>
<h2>Weather location</h2>
<label for="ln">Name</label><input id="ln" name="location_name" value="{{ f.location_name }}" maxlength="100">
<div class="row"><div><label for="lat">Latitude</label><input id="lat" name="location_lat" value="{{ f.location_lat }}" inputmode="decimal"></div>
<div><label for="lon">Longitude</label><input id="lon" name="location_lon" value="{{ f.location_lon }}" inputmode="decimal"></div></div>
<h2>Box</h2>
<label for="rc">Recording cap (seconds, 1–300)</label><input id="rc" type="number" name="rec_cap_s" min="1" max="300" step="1" value="{{ f.rec_cap_s }}" required>
<label for="vol">Volume (0–1, always under the hearing-safe ceiling)</label><input id="vol" type="number" name="volume" min="0" max="1" step="0.05" value="{{ f.volume }}" required>
<h2>Cloud</h2>
<label for="sc">Monthly spend cap (USD)</label><input id="sc" type="number" name="spend_cap_usd" min="0" step="0.5" value="{{ f.spend_cap_usd }}" required>
<label for="rd">Review delay for Zozo's messages (minutes)</label><input id="rd" type="number" name="review_delay_min" min="0" max="10080" step="1" value="{{ f.review_delay_min }}" required>
<label for="au">New content needs approval until (empty = never)</label><input id="au" type="date" name="approval_required_until" value="{{ f.approval_required_until }}">
<p><button class="primary">Save settings</button></p></form>
<form method="post" action="/settings/password" class="card">""" + CSRF + r"""
<h2>Change password</h2>
<label for="cp">Current password</label><input id="cp" type="password" name="current" autocomplete="current-password" required>
<label for="np">New password</label><input id="np" type="password" name="password" autocomplete="new-password" required minlength="8">
<label for="np2">Repeat new password</label><input id="np2" type="password" name="confirm" autocomplete="new-password" required minlength="8">
<p><button>Change password</button></p><p class="small muted">Other devices will be logged out.</p></form>
{% endblock %}"""

ERROR = r"""{% extends "base.html" %}{% block body %}
<h1>{{ heading }}</h1><p>{{ message }}</p><p><a href="/">Back to the dashboard</a></p>
{% endblock %}"""

TEMPLATES: dict[str, str] = {
    "base.html": BASE,
    "macros.html": MACROS,
    "setup.html": SETUP,
    "login.html": LOGIN,
    "home.html": HOME,
    "setup_locked.html": SETUP_LOCKED,
    "approvals.html": APPROVALS,
    "archive.html": ARCHIVE,
    "item.html": ITEM,
    "recordings.html": RECORDINGS,
    "mood.html": MOOD,
    "stats.html": STATS,
    "reprint.html": REPRINT,
    "settings.html": SETTINGS,
    "error.html": ERROR,
}
