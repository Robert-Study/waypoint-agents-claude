"""Installable app shell for Waypoint (v5 node N1). Serves /app/ from the engine origin so Edge can
install it as an app: own window, taskbar pin, desktop notifications fed by the existing /api/stream.

Routes (GET only, no state changes): /app/, /app/manifest.webmanifest, /app/sw.js, /app/icon-256.png,
/app/icon-128.png. Icons come from ``plugin/assets`` when that folder exists; otherwise (the Waypoint
Core export has no plugin/) a plain icon is drawn in code, so the page, manifest and icons always answer.
The service worker never caches anything: every request goes to the network, so /api/ and the SSE
stream are never stored. Touches nothing in the dsh install.
"""
from __future__ import annotations

import json
import struct
import zlib
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[2] / "plugin" / "assets"
DSH_URL = "http://127.0.0.1:3080"

MANIFEST = {
    "name": "Waypoint", "short_name": "Waypoint", "start_url": "/app/", "scope": "/app/",
    "display": "standalone", "background_color": "#14161a", "theme_color": "#14161a",
    "icons": [{"src": "/app/icon-256.png", "sizes": "256x256", "type": "image/png", "purpose": "any"},
              {"src": "/app/icon-128.png", "sizes": "128x128", "type": "image/png"}],
}

SW = """// Waypoint shell worker: network only. Nothing is cached, so /api/* and the SSE stream stay live.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => { e.respondWith(fetch(e.request)); });
"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Waypoint</title><link rel="manifest" href="/app/manifest.webmanifest"><link rel="icon" href="/app/icon-128.png">
<style>body{margin:0;font:15px/1.5 Segoe UI,system-ui,sans-serif;background:#14161a;color:#e6e8eb}
main{max-width:640px;margin:0 auto;padding:24px 16px}h1{font-size:20px;margin:0 0 4px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#777;margin-right:6px}.on .dot{background:#3fb950}
a.btn,button{background:#2b6cb0;color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;text-decoration:none;cursor:pointer;margin:6px 8px 6px 0;display:inline-block}
ul{list-style:none;padding:0}li{padding:6px 0;border-top:1px solid #2a2e35}small{color:#9aa0a8}
.chip{background:#2a2e35;border-radius:10px;padding:0 7px;margin-left:6px;font-size:12px;color:#9aa0a8}
button.hb{background:#2a2e35;padding:2px 10px;margin:0 0 0 8px;font-size:12px}#pj ul{margin:4px 0 0 14px}details{margin-top:2px}
button.sec{background:transparent;color:#e6e8eb;border:1px solid #3a3f47;padding:5px 12px;margin:0 8px 6px 0}
#ag select,#ag input{background:#1b1e23;color:#e6e8eb;border:1px solid #3a3f47;border-radius:6px;padding:4px 6px;font:inherit;margin:0 6px 6px 0}
#agt{margin:6px 0 0}.ar{padding:4px 0;border-top:1px solid #2a2e35;font-size:13px;line-height:1.35}
.ar .g{display:inline-block;width:16px;text-align:center;font-weight:600}.ar .t{font-weight:600}
.ar .m{color:#9aa0a8;margin-left:8px;font-size:12px}.ar .cb{display:inline-block;width:56px;height:4px;background:#2a2e35;border-radius:2px;vertical-align:middle;margin:0 4px 0 8px}
.ar .cb i{display:block;height:4px;border-radius:2px}.ar .ll{color:#7d838c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
.ar a{color:#7aa7d9;font-size:12px;margin-left:8px}.ar.ch{margin-left:16px}#agm{color:#9aa0a8;font-size:13px;margin:2px 0 6px}</style></head><body><main>
<h1>Waypoint</h1><div id="st"><span class="dot"></span><span id="stt">Connecting to the engine...</span></div>
<p><a class="btn" href="__DSH__" target="_blank" rel="noopener">Open the Waypoint board</a>
<button id="nb" type="button">Turn on desktop notifications</button></p>
<p><small id="sum"></small></p><h2 style="font-size:15px">Projects</h2><ul id="pj"></ul><section id="ag"><h2 style="font-size:15px">Agents</h2>
<p><small>Start workers from the Waypoint pane in Claude (Agents tab). This page only shows them.</small></p>
<div id="agm"></div><div id="agt"></div></section>
<h2 style="font-size:15px">Live events</h2><ul id="ev"></ul></main>
<script>
if('serviceWorker' in navigator)navigator.serviceWorker.register('/app/sw.js',{scope:'/app/'}).catch(()=>{});
const $=id=>document.getElementById(id),ev=$('ev');
function note(t){const li=document.createElement('li');li.textContent=new Date().toLocaleTimeString()+'  '+t;ev.prepend(li);while(ev.children.length>30)ev.lastChild.remove();
 if(window.Notification&&Notification.permission==='granted')try{new Notification('Waypoint',{body:t,icon:'/app/icon-128.png',tag:t})}catch(e){}}
$('nb').onclick=()=>window.Notification?Notification.requestPermission().then(p=>{$('nb').textContent=p==='granted'?'Notifications on':'Notifications blocked'}):0;
if(window.Notification&&Notification.permission==='granted')$('nb').textContent='Notifications on';
let es,first=true;function open(){es=new EventSource('/api/stream');
 es.onopen=()=>{document.body.classList.add('on');$('stt').textContent='Live'};
 es.onerror=()=>{document.body.classList.remove('on');$('stt').textContent='Engine unreachable, retrying'};
 es.addEventListener('state',m=>{try{const b=JSON.parse(m.data);const n=(b.nodes||b.rows||[]);const pj=b.project;const nm=b.title||(pj&&typeof pj==='object'?(pj.title||pj.id):pj)||'';$('sum').textContent=nm+(n.length?' · '+n.length+' nodes':'');}catch(e){}if(first)first=false;});
 for(const k of['run','pool','question'])es.addEventListener(k,m=>{let d={};try{d=JSON.parse(m.data)}catch(e){}note(k+': '+(d.title||d.node||d.id||d.message||m.data).toString().slice(0,120))});}
open();

// Projects + History: read-only. The web app is frozen apart from History (Owner asked for it on 5 Nov 2026).
function el(t,txt,cls){const e=document.createElement(t);if(txt!=null)e.textContent=String(txt);if(cls)e.className=cls;return e}
function renderHistory(box,h){box.textContent='';
 const vs=h.versions||[],sk=h.skipped||[],ul=el('ul');
 for(const v of vs){const c=v.counts||{};ul.append(el('li','v'+v.version+'  '+(v.title||v.project)+'  ('+(c.done||0)+' done, '+(c.open||0)+' open, '+(c.skipped||0)+' skipped)'))}
 box.append(ul);
 if(sk.length){box.append(el('small','Skipped ideas'));const su=el('ul');
  for(const k of sk){const li=el('li',(k.title||k.id)+'  v'+k.version+(k.reason?' - '+k.reason:''));
   if(k.goal){const d=el('details'),sm=el('summary','Goal');d.append(sm,el('div',k.goal));li.append(d)}su.append(li)}
  box.append(su)}}
function projectRow(p,fam){const li=el('li'),t=p.family_title||p.title||p.id;li.append(el('span',t));
 if(p.version)li.append(el('span','v'+p.version,'chip'));
 if(fam&&p.versions>1){const b=el('button','History','hb'),box=el('div');b.type='button';let loaded=false;
  b.onclick=()=>{if(box.hidden=!box.hidden,loaded||box.hidden)return;loaded=true;box.textContent='Loading...';
   fetch('/api/history?family='+encodeURIComponent(fam)).then(r=>r.json()).then(h=>renderHistory(box,h)).catch(()=>{loaded=false;box.textContent='History unavailable'})};
  box.hidden=true;li.append(b,box)}
 return li}
function loadProjects(){fetch('/api/projects').then(r=>r.json()).then(d=>{const list=Array.isArray(d)?d:(d.projects||[]),best={},out=[];
  for(const p of list){if(!p.family){out.push(p);continue}const o=best[p.family];if(!o||(p.version||0)>(o.version||0))best[p.family]=p}
  const ul=$('pj');ul.textContent='';
  for(const p of out)ul.append(projectRow(p,null));
  for(const f of Object.keys(best))ul.append(projectRow(best[f],f))}).catch(()=>{$('pj').textContent='Projects unavailable'})}
loadProjects();

// Agents: live tree from /api/trace/tree (SSE 'trace' event, 15 s poll as fallback) (read-only; dispatch lives in the mod).
const GL={running:'●',idle:'○',waiting:'◐',done:'✓',error:'✕',ended:'–'},GC={running:'#0a84ff',idle:'#8e8e93',waiting:'#ff9f0a',done:'#34c759',error:'#ff453a',ended:'#8e8e93'};
function kfmt(n){n=Number(n)||0;return n>=1000?(n/1000).toFixed(n>=10000?0:1)+'k':String(n)}
function agentRow(a,child){const r=el('div',null,'ar'+(child?' ch':'')),g=el('span',GL[a.status]||'·','g');g.style.color=GC[a.status]||'#8e8e93';g.title=a.status||'';
 r.append(g,el('span',a.title||a.id,'t'));
 if(a.tool)r.append(el('span',a.tool,'m'));
 const tk=(a.tokens_in||0)+(a.tokens_out||0);if(tk)r.append(el('span',kfmt(tk)+' tokens','m'));
 if(a.context&&a.context_window){const pc=Math.min(100,Math.round(100*a.context/a.context_window)),cb=el('span',null,'cb'),f=el('i');
  f.style.width=pc+'%';f.style.background=pc>=85?GC.error:pc>=65?GC.waiting:GC.running;cb.append(f);r.append(cb,el('span',pc+'%','m'))}
 if(a.transcript){const l=el('a','transcript');l.href='file:///'+String(a.transcript).split(String.fromCharCode(92)).join('/');l.title=a.transcript;l.target='_blank';l.rel='noopener';r.append(l)}
 if(a.last_line)r.append(el('div',a.last_line,'ll'));
 return r}
function renderTree(d){const box=$('agt'),rows=d.agents||[],by={},kids={};box.textContent='';
 for(const a of rows)by[a.id]=a;
 for(const a of rows){if(a.parent&&by[a.parent])(kids[a.parent]=kids[a.parent]||[]).push(a)}
 const roots=rows.filter(a=>!(a.parent&&by[a.parent])),seen={};
 function walk(a,depth){if(seen[a.id])return;seen[a.id]=1;box.append(agentRow(a,depth>0));for(const k of kids[a.id]||[])walk(k,depth+1)}
 for(const a of roots)walk(a,0);
 if(!rows.length)box.append(el('small','No agents have reported yet.'))}
let agTimer=null;
function loadTree(){fetch('/api/trace/tree').then(r=>r.json()).then(renderTree).catch(()=>{$('agt').textContent='Agents unavailable'})}
function soon(){if(agTimer)return;agTimer=setTimeout(()=>{agTimer=null;loadTree()},400)}
es.addEventListener('trace',soon);setInterval(loadTree,15000);loadTree();
</script></body></html>""".replace("__DSH__", DSH_URL)


def _raw(h, ctype, body, cache="no-store"):
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", cache)
    h.end_headers()
    h.wfile.write(data)


def g_page(h, q):
    _raw(h, "text/html; charset=utf-8", PAGE)


def g_manifest(h, q):
    _raw(h, "application/manifest+json", json.dumps(MANIFEST))


def g_sw(h, q):
    _raw(h, "text/javascript; charset=utf-8", SW)


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


@lru_cache(maxsize=4)
def fallback_icon(size) -> bytes:
    """A plain Waypoint icon (dark rounded square, green waypoint dot) as PNG bytes, stdlib only."""
    bg, dot = (0x14, 0x16, 0x1A, 0xFF), (0x3F, 0xB9, 0x50, 0xFF)
    r_corner, c, r_dot = size * 0.18, (size - 1) / 2, size * 0.22
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # filter type: none
        for x in range(size):
            dx = max(r_corner - x, 0, x - (size - 1 - r_corner))
            dy = max(r_corner - y, 0, y - (size - 1 - r_corner))
            if dx * dx + dy * dy > r_corner * r_corner:
                row += b"\x00\x00\x00\x00"  # transparent outside the rounded corner
            elif (x - c) ** 2 + (y - c) ** 2 <= r_dot * r_dot:
                row += bytes(dot)
            else:
                row += bytes(bg)
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + _chunk(b"IEND", b""))


def _icon(size):
    def fn(h, q):
        p = ASSETS / f"icon-{size}.png"
        data = p.read_bytes() if p.is_file() else fallback_icon(size)
        _raw(h, "image/png", data, "max-age=3600")
    return fn


ROUTES = {"GET /app/": g_page,"GET /app/manifest.webmanifest": g_manifest, "GET /app/sw.js": g_sw,
          "GET /app/icon-256.png": _icon(256), "GET /app/icon-128.png": _icon(128)}
