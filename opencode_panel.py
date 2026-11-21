#!/usr/bin/env python3
"""
opencode 节点检测面板
- 对每个节点同时测：ping（gstatic 204，就是 FlClash 界面那个数）和 opencode 接口
- 浏览器打开 http://127.0.0.1:8765
- 只用 Python 标准库，直接读 FlClash 的 config.yaml 拿 controller/secret
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CFG = os.environ.get("CLASH_CONFIG") or os.path.expanduser(
    "~/.local/share/com.follow.clash/config.yaml")
AUTO_CONF = os.environ.get("CLASH_AUTO_CONF") or os.path.expanduser(
    "~/.config/clash_auto.json")


def _read_group():
    """代理组名从 clash_auto.json 读，不把机场/订阅信息写进代码"""
    try:
        with open(AUTO_CONF, encoding="utf-8") as f:
            return json.load(f).get("group") or ""
    except (OSError, ValueError):
        return ""


GROUP = _read_group()
PING_URL = "http://www.gstatic.com/generate_204"
OC_URL = "https://models.opencode.ai/experimental/v2.0/api.json"
PORT = 8765
SKIP_TYPE = ("Selector", "URLTest", "Fallback", "LoadBalance")


def _read_cfg():
    s = open(CFG, encoding="utf-8").read()
    secret = re.search(r'^secret:\s*"([^"]*)"', s, re.M).group(1)
    ctrl = re.search(r'^external-controller:\s*"([^"]*)"', s, re.M).group(1)
    return ctrl, secret


CTRL, SECRET = _read_cfg()
API = "http://" + CTRL


def api(path, method="GET", body=None, timeout=30):
    req = urllib.request.Request(
        API + path, method=method,
        headers={"Authorization": "Bearer " + SECRET,
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def delay(name, url, timeout):
    q = urllib.parse.quote(name, safe="")
    u = urllib.parse.quote(url, safe="")
    try:
        return api(f"/proxies/{q}/delay?url={u}&timeout={timeout}").get("delay")
    except Exception:
        return None


STATE = {"ts": 0, "rows": [], "running": False, "error": None, "current": None}
LOCK = threading.Lock()


def scan():
    with LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
        STATE["error"] = None
    try:
        if not GROUP:
            raise RuntimeError(
                '未配置代理组名：请在 ~/.config/clash_auto.json 里设置 "group"')
        d = api("/proxies")["proxies"]
        nodes = [n for n in d[GROUP]["all"]
                 if d.get(n, {}).get("type") not in SKIP_TYPE]

        def job(n):
            return {"node": n,
                    "ping": delay(n, PING_URL, 5000),
                    "oc": delay(n, OC_URL, 8000)}

        with ThreadPoolExecutor(max_workers=6) as ex:
            rows = list(ex.map(job, nodes))
        cur = api("/proxies/" + urllib.parse.quote(GROUP, safe="")).get("now")
        with LOCK:
            STATE.update(ts=time.time(), rows=rows, current=cur, running=False)
    except Exception as e:                                   # noqa: BLE001
        with LOCK:
            STATE.update(running=False, error=str(e))


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>opencode 节点检测</title><style>
:root{--bg:#111318;--card:#1a1e26;--fg:#e6e9ef;--dim:#8b93a7;--ok:#35d07f;--warn:#f5a623;--bad:#e5484d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
button{background:#252b36;color:var(--fg);border:1px solid #333b4a;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{background:#2e3644}
button:disabled{opacity:.5;cursor:default}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #232833;font-variant-numeric:tabular-nums}
th{color:var(--dim);font-weight:500;font-size:12px;position:sticky;top:0;background:var(--bg)}
td.n{font-family:ui-monospace,monospace;font-size:13px}
.num{width:90px}
.badge{font-size:12px;padding:2px 8px;border-radius:99px;white-space:nowrap}
.ok{background:rgba(53,208,127,.14);color:var(--ok)}
.warn{background:rgba(245,166,35,.14);color:var(--warn)}
.bad{background:rgba(229,72,77,.14);color:var(--bad)}
tr:hover td{background:#1d222b}
.cur{color:var(--ok)}
.small{font-size:12px;color:var(--dim)}
.pill{background:#232833;border-radius:6px;padding:4px 10px;font-size:12px;color:var(--dim)}
</style></head><body><div class="wrap">
<h1>opencode 节点检测</h1>
<div class="sub">参照 <code>models.opencode.ai/experimental/v2.0/api.json</code>　·　时间是真实 HTTPS 往返，不是 ping</div>
<div class="bar">
  <button id="refresh">重新检测</button>
  <label class="pill"><input type="checkbox" id="auto" checked> 自动刷新（60s）</label>
  <span class="pill" id="stamp">-</span>
  <span class="pill" id="cur">-</span>
</div>
<table><thead><tr><th>节点</th><th class="num">ping</th><th class="num">opencode</th><th>判定</th><th></th></tr></thead>
<tbody id="tb"><tr><td colspan="5" class="small">加载中…</td></tr></tbody></table>
</div><script>
const tb=document.getElementById('tb');
function ms(v){return v? v+'ms' : '超时'}
function cls(r){ if(r.ping&&r.oc) return ['ok','可用']; if(r.ping) return ['warn','ping通 / oc不通']; if(r.oc) return ['warn','ping超时 / oc可用']; return ['bad','不可用'] }
async function load(){
  const r=await fetch('/api/data'); const d=await r.json();
  document.getElementById('stamp').textContent = d.running? '检测中…' : (d.ts? '更新于 '+new Date(d.ts*1000).toLocaleTimeString() : '未检测');
  document.getElementById('cur').textContent = d.current? ('当前: '+d.current) : '';
  document.getElementById('refresh').disabled = !!d.running;
  if(d.error){ tb.innerHTML='<tr><td colspan="5" class="small">错误: '+d.error+'</td></tr>'; return }
  const rows=(d.rows||[]).slice().sort((a,b)=>{
    const pa=a.ping&&a.oc?0:a.ping||a.oc?1:2, pb=b.ping&&b.oc?0:b.ping||b.oc?1:2;
    if(pa!==pb) return pa-pb;
    return (a.oc||1e9)-(b.oc||1e9);
  });
  tb.innerHTML=rows.map(x=>{
    const [c,t]=cls(x);
    const isCur = d.current===x.node;
    return `<tr><td class="n ${isCur?'cur':''}">${x.node}</td>
      <td class="num">${ms(x.ping)}</td><td class="num">${ms(x.oc)}</td>
      <td><span class="badge ${c}">${t}</span></td>
      <td><button data-n="${encodeURIComponent(x.node)}" ${isCur?'disabled':''}>切换</button></td></tr>`
  }).join('') || '<tr><td colspan="5" class="small">没有节点</td></tr>';
  tb.querySelectorAll('button[data-n]').forEach(b=>b.onclick=async()=>{
    b.disabled=true;
    await fetch('/api/select',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:decodeURIComponent(b.dataset.n)})});
    load();
  });
}
async function refresh(){
  const b=document.getElementById('refresh'); b.disabled=true;
  await fetch('/api/refresh',{method:'POST'}); setTimeout(load,800);
}
document.getElementById('refresh').onclick=refresh;
setInterval(()=>{ if(document.getElementById('auto').checked) load() }, 60000);
load();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/data"):
            with LOCK:
                snap = dict(STATE)
            return self._send(200, json.dumps(snap, ensure_ascii=False))
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path.startswith("/api/refresh"):
            threading.Thread(target=scan, daemon=True).start()
            return self._send(200, '{"ok":true}')
        if self.path.startswith("/api/select"):
            name = body.get("name")
            try:
                api("/proxies/" + urllib.parse.quote(GROUP, safe=""),
                    "PUT", {"name": name})
                return self._send(200, '{"ok":true}')
            except Exception as e:                            # noqa: BLE001
                return self._send(500, json.dumps({"error": str(e)}))
        self._send(404, '{"error":"not found"}')


if __name__ == "__main__":
    threading.Thread(target=scan, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"opencode 检测面板: http://127.0.0.1:{PORT}")
    srv.serve_forever()
