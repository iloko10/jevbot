"""Small web dashboard (Flask). Runs in a background thread next to the bot."""
import json
import time

from flask import Flask, jsonify, request

from .store import reader

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jev Paper Bot</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--mute:#8b93a7;--good:#3ecf8e;--bad:#ff6b6b;--line:#262b36}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px} .mute{color:var(--mute)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.k{color:var(--mute);font-size:12px}.v{font-size:20px;font-weight:600;margin-top:2px}
.good{color:var(--good)}.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px;border-bottom:1px solid var(--line);text-align:left}
pre{white-space:pre-wrap;font-size:12px;margin:0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:800px){.two{grid-template-columns:1fr}}
</style></head><body><main>
<h1>Jev Paper Market-Maker · <span id="coin"></span></h1>
<div class="mute" id="sub">loading…</div>
<div class="grid" id="tiles"></div>
<div class="card"><canvas id="eq" height="90"></canvas></div>
<div class="card" style="margin-top:10px"><div class="k">Fees check: does each spread earn more than it pays? (same live market, same decisions)</div>
<div style="overflow-x:auto"><table id="feetab"></table></div>
<div class="mute" style="font-size:12px;margin-top:6px">1 bps = 1 cent per $100 traded. "Earned" is before fees. The book wins only if Earned &gt; Fees.
Fee tier follows Hyperliquid's real schedule (lower fees after $5M / $25M / $100M of 14-day volume).</div></div>
<div class="two" style="margin-top:10px">
 <div class="card"><div class="k">Latest judgments</div><pre id="ans"></pre></div>
 <div class="card"><div class="k">Recent fills (main book)</div><table id="fills"></table></div>
</div>
<div class="card" style="margin-top:10px"><div class="k">Events (kill switch, Jev down/up)</div><table id="events"></table></div>
</main><script>
let chart;
const f=(x,d=2)=>x==null?'–':Number(x).toFixed(d);
async function load(){
 const s=await (await fetch('api/status')).json();
 document.getElementById('coin').textContent=s.coin;
 const L=s.last||{},B=s.book||{},S=s.stats||{};
 document.getElementById('sub').textContent=`mode: ${s.judge} · decisions: ${s.mode||'–'} · ticks: ${s.ticks} · up ${(s.uptime_s/3600).toFixed(1)}h · PAPER TRADING (no real money)`;
 const t=[['Equity $',f(B.equity)],['Total PnL $',f(B.pnl_total),B.pnl_total>=0],['Today PnL $',f(B.daily_pnl),B.daily_pnl>=0],
 ['Fees paid $',f(B.fees)],['Inventory',f(B.inventory,4)],['Drawdown $',f(B.drawdown)],['Action',L.action||'–'],
 ['Bid / Ask',`${f(L.bid_px,0)} / ${f(L.ask_px,0)}`],['Mid',f(L.mid,1)],['Decision ms',f(L.latency_ms,1)],
 ['Fills / posted',`${S.fills||0} / ${S.posted||0}`],['Rejects',S.rejects||0]];
 document.getElementById('tiles').innerHTML=t.map(([k,v,g])=>`<div class="card"><div class="k">${k}</div><div class="v ${g===true?'good':g===false?'bad':''}">${v}</div></div>`).join('');
 document.getElementById('ans').textContent=(L.reason?('why: '+L.reason+'\\n\\n'):'')+JSON.stringify(L.answers||{},(k,v)=>typeof v==='number'?Math.round(v*1000)/1000:v,1);
 const bk=s.books||[];
 const verdict=b=>!b.fills?'<span class="mute">no fills yet</span>':b.net_bps>0?'<span class="good">beats fees</span>':b.earned_bps>0?'<span class="bad">loses to fees</span>':'<span class="bad">loses even before fees</span>';
 document.getElementById('feetab').innerHTML='<tr><th>Book</th><th>Spread</th><th>Fills</th><th>Volume $</th><th>Earned bps</th><th>Fees bps</th><th>Net bps</th><th>PnL $</th><th>Fee tier</th><th>Result</th></tr>'+
  bk.map(b=>`<tr><td>${b.book}${b.killed?' <span class="bad">(stopped today)</span>':''}</td><td>${b.half_spread_bps} bps</td><td>${b.fills}</td><td>${Math.round(b.volume).toLocaleString()}</td><td>${f(b.earned_bps)}</td><td>${f(b.fee_bps)}</td><td class="${b.net_bps>0?'good':b.net_bps<0?'bad':''}">${f(b.net_bps)}</td><td class="${b.pnl>=0?'good':'bad'}">${f(b.pnl)}</td><td>${b.tier} (${f(b.maker_fee_bps,2)} bps)</td><td>${verdict(b)}</td></tr>`).join('');
 const fl=await (await fetch('api/fills?n=15')).json();
 document.getElementById('fills').innerHTML='<tr><th>time</th><th>side</th><th>px</th><th>size</th><th>maker</th></tr>'+fl.map(x=>`<tr><td>${new Date(x.ts*1000).toLocaleTimeString()}</td><td>${x.side}</td><td>${f(x.px,0)}</td><td>${f(x.sz,4)}</td><td>${x.maker?'yes':'TAKER'}</td></tr>`).join('');
 const ev=await (await fetch('api/events')).json();
 document.getElementById('events').innerHTML=ev.length?ev.map(x=>`<tr><td>${new Date(x.ts*1000).toLocaleString()}</td><td>${x.kind}</td><td>${x.detail}</td></tr>`).join(''):'<tr><td class="mute">none</td></tr>';
 const se=await (await fetch('api/series?hours=6')).json();
 const lab=se.map(x=>new Date(x.ts*1000).toLocaleTimeString()),eq=se.map(x=>x.equity);
 if(!chart){chart=new Chart(document.getElementById('eq'),{type:'line',data:{labels:lab,datasets:[{label:'Equity (6h)',data:eq,borderColor:'#3ecf8e',pointRadius:0,borderWidth:1.5}]},options:{animation:false,plugins:{legend:{labels:{color:'#8b93a7'}}},scales:{x:{ticks:{color:'#8b93a7',maxTicksLimit:8}},y:{ticks:{color:'#8b93a7'}}}}});}
 else{chart.data.labels=lab;chart.data.datasets[0].data=eq;chart.update();}
}
load();setInterval(load,3000);
</script></body></html>"""


def create_app(engine, cfg):
    app = Flask(__name__)
    db_path = cfg.db_path

    def q(sql, args=()):
        con = reader(db_path)
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    @app.get("/")
    def index():
        return PAGE

    @app.get("/healthz")
    def healthz():
        last = engine.status.get("last") or {}
        age = time.time() - last.get("ts", 0)
        ok = age < 30
        return jsonify(ok=ok, last_tick_age_s=round(age, 1)), (200 if ok else 503)

    @app.get("/api/status")
    def status():
        s = dict(engine.status)
        s["uptime_s"] = time.time() - s["started"]
        s["coin"] = cfg.coin
        s["judge"] = f"Jev ({cfg.jev_model})" if cfg.use_jev else "stub rules (no Jev key yet)"
        return jsonify(json.loads(json.dumps(s, default=str)))

    @app.get("/api/series")
    def series():
        hours = float(request.args.get("hours", 6))
        rows = q("SELECT ts, equity, mid, inventory FROM decisions WHERE ts > ? ORDER BY ts",
                 (time.time() - hours * 3600,))
        step = max(1, len(rows) // 600)  # downsample to ~600 points
        return jsonify(rows[::step])

    @app.get("/api/fills")
    def fills():
        n = int(request.args.get("n", 50))
        book = request.args.get("book", "main")
        return jsonify(q("SELECT * FROM fills WHERE book = ? ORDER BY ts DESC LIMIT ?", (book, n)))

    @app.get("/api/books")
    def books():
        """Fee check history: one row per book per minute."""
        hours = float(request.args.get("hours", 24))
        return jsonify(q("SELECT * FROM book_stats WHERE ts > ? ORDER BY ts", (time.time() - hours * 3600,)))

    @app.get("/api/events")
    def events():
        return jsonify(q("SELECT * FROM events ORDER BY ts DESC LIMIT 20"))

    return app


def serve(engine, cfg):
    app = create_app(engine, cfg)
    app.run(host="0.0.0.0", port=cfg.port, debug=False, use_reloader=False, threaded=True)
