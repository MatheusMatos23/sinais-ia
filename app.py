"""
app.py — Sinais IA · Scanner multi-estratégia com backtest (uso próprio).

Você escolhe a ESTRATÉGIA (A/B/C/D) e o TIMEFRAME; o sistema varre os ativos de
mercado aberto e mostra as entradas daquela estratégia. O painel de desempenho
roda o backtest das 4 estratégias (hoje e últimos dias) para você escolher.

O mesmo código (strategies.py) roda ao vivo e no backtest — sem discrepância.
Regra: entrada na ABERTURA da vela seguinte; acerto pela COR da vela
(COMPRA vence se fechar verde; VENDA vence se fechar vermelha).
"""
from __future__ import annotations
import math
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

from strategies import (STRATEGIES, add_indicators, score_of, classify,
                        backtest, MIN_SCORE, wilson_ci, breakeven, verdict)

socket.setdefaulttimeout(8)
st.set_page_config(page_title="Sinais IA · Estratégias", page_icon="⚡", layout="wide")

ASSETS = [
    {"name": "EUR/USD", "yf": "EURUSD=X", "cur": ["EUR", "USD"], "type": "fx"},
    {"name": "GBP/USD", "yf": "GBPUSD=X", "cur": ["GBP", "USD"], "type": "fx"},
    {"name": "USD/JPY", "yf": "USDJPY=X", "cur": ["USD", "JPY"], "type": "fx"},
    {"name": "AUD/USD", "yf": "AUDUSD=X", "cur": ["AUD", "USD"], "type": "fx"},
    {"name": "USD/CAD", "yf": "USDCAD=X", "cur": ["USD", "CAD"], "type": "fx"},
    {"name": "USD/CHF", "yf": "USDCHF=X", "cur": ["USD", "CHF"], "type": "fx"},
    {"name": "NZD/USD", "yf": "NZDUSD=X", "cur": ["NZD", "USD"], "type": "fx"},
    {"name": "EUR/JPY", "yf": "EURJPY=X", "cur": ["EUR", "JPY"], "type": "fx"},
    {"name": "BTC/USD", "yf": "BTC-USD", "cur": [], "type": "crypto"},
    {"name": "ETH/USD", "yf": "ETH-USD", "cur": [], "type": "crypto"},
]
SESSIONS = {"Sydney": (21, 6), "Tóquio": (23, 8), "Londres": (7, 16), "Nova York": (12, 21)}
CUR_SESS = {"AUD": "Sydney", "NZD": "Sydney", "JPY": "Tóquio", "EUR": "Londres",
            "GBP": "Londres", "CHF": "Londres", "USD": "Nova York", "CAD": "Nova York"}
TF_LABEL = {"1": "1 min", "5": "5 min", "15": "15 min"}
TF_YF = {"1": "1m", "5": "5m", "15": "15m"}
TF_PERIOD = {"1m": "7d", "5m": "1mo", "15m": "1mo"}


def market_open(d):
    wd, h = d.weekday(), d.hour
    if wd == 5:
        return False
    if wd == 6 and h < 21:
        return False
    if wd == 4 and h >= 21:
        return False
    return True


def in_win(h, s, e):
    return (s <= h < e) if s <= e else (h >= s or h < e)


def active_sessions(d):
    return [k for k, (s, e) in SESSIONS.items() if in_win(d.hour, s, e)] if market_open(d) else []


def pair_open(a, d):
    return True if a["type"] == "crypto" else any(CUR_SESS.get(c) in set(active_sessions(d)) for c in a["cur"])


def candle_key(m):
    return int(math.floor(datetime.now(timezone.utc).timestamp() / 60.0 / m))


# ---------------------- dados (1 download por ativo, cache por vela) ----------
def _dl(yf_symbol, interval):
    try:
        import yfinance as yf
        df = yf.download(yf_symbol, interval=interval, period=TF_PERIOD[interval],
                         progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].astype(float).dropna()
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        return df
    except Exception:
        return None


def get_data(assets, interval, minutes):
    cache = st.session_state.setdefault("ohlc", {})
    ck = candle_key(minutes)
    todo = [a for a in assets if (a["yf"], interval, ck) not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            futs = {ex.submit(_dl, a["yf"], interval): a for a in todo}
            for f in as_completed(futs):
                cache[(futs[f]["yf"], interval, ck)] = f.result()
    return {a["name"]: cache.get((a["yf"], interval, ck)) for a in assets}


# ==============================  CSS  ======================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700;800&display=swap');
.stApp{background:radial-gradient(1200px 560px at 12% -8%,#182c60 0%,rgba(6,9,20,0) 55%),
 radial-gradient(920px 480px at 100% -6%,#0b3d4c 0%,rgba(6,9,20,0) 52%),linear-gradient(#070a17,#04060e);
 color:#eaf0ff;font-family:'Inter',sans-serif;}
.stApp:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.28;
 background-image:linear-gradient(rgba(120,150,255,.05) 1px,transparent 1px),
 linear-gradient(90deg,rgba(120,150,255,.05) 1px,transparent 1px);background-size:46px 46px;}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:1rem;max-width:1180px;position:relative;z-index:1}
.brand{display:flex;align-items:center;gap:12px}
.brand .logo{font-size:1.5rem;filter:drop-shadow(0 0 12px rgba(0,245,176,.6))}
.brand .name{font-size:1.5rem;font-weight:900;background:linear-gradient(90deg,#8ff8db,#4fd2ff 55%,#c8a0ff);
 -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.brand .live{font-size:.58rem;font-weight:800;letter-spacing:2px;color:#052018;
 background:linear-gradient(90deg,#00f5b0,#22d3ee);padding:4px 11px;border-radius:999px}
.sess{margin:12px 0 4px;font-size:.8rem;color:#9db0d6;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.pill{padding:4px 11px;border-radius:999px;font-size:.72rem;font-weight:700;border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.04)}
.pill.ses{color:#7ee9ff;border-color:rgba(34,211,238,.35)}
.pill.on{color:#7ef7d6;border-color:rgba(0,245,176,.35)}
.hero-sig{position:relative;border-radius:26px;padding:30px 38px;margin-top:6px;min-height:196px;
 display:flex;flex-direction:column;justify-content:center;overflow:hidden;
 background:linear-gradient(155deg,rgba(255,255,255,.07),rgba(255,255,255,.015));
 border:1px solid rgba(255,255,255,.10);backdrop-filter:blur(16px)}
.hero-sig .tag{position:absolute;top:18px;right:22px;font-size:.6rem;font-weight:800;letter-spacing:2px;
 padding:5px 12px;border-radius:999px;background:rgba(255,255,255,.06);color:#b7c4e6;border:1px solid rgba(255,255,255,.12)}
.hero-sig .pair{font-size:1.3rem;font-weight:700;color:#e6edff;letter-spacing:2px}
.hero-sig .dir{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:4.2rem;line-height:1;
 margin:.35rem 0 .3rem;display:flex;align-items:center;gap:16px}
.hero-sig .arw{font-size:2.9rem}
.hero-sig .sub{font-size:.8rem;letter-spacing:3px;color:#8ea2c8;font-weight:700}
.buy .dir{color:#00f5b0;text-shadow:0 0 40px rgba(0,245,176,.5)}
.sell .dir{color:#ff3b6b;text-shadow:0 0 40px rgba(255,59,107,.45)}
.wait .dir{color:#9fb0d4;font-size:2.5rem;text-shadow:none}
.hero-sig.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.32),0 26px 70px rgba(0,245,176,.09)}
.hero-sig.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.30),0 26px 70px rgba(255,59,107,.09)}
.hero-sig.wait{box-shadow:inset 0 0 0 1px rgba(255,255,255,.07)}
.hero-sig .glow{position:absolute;right:-70px;top:-70px;width:300px;height:300px;border-radius:50%;filter:blur(80px);opacity:.42}
.buy .glow{background:#00f5b0}.sell .glow{background:#ff008c}.wait .glow{background:#3b4a70;opacity:.22}
.fbars{display:flex;gap:8px;margin-top:16px;align-items:center}
.fbars .b{width:48px;height:10px;border-radius:6px;background:rgba(255,255,255,.10)}
.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee);box-shadow:0 0 18px rgba(0,245,176,.5)}
.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c);box-shadow:0 0 18px rgba(255,59,107,.45)}
.flabel{margin-left:12px;font-size:.74rem;letter-spacing:3px;font-weight:800;color:#b3bede}
.gtitle{margin:22px 0 10px;font-size:1rem;font-weight:800;color:#dbe6ff}
.gtitle small{color:#8496bd;font-weight:600;font-size:.78rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}
.card{border-radius:18px;padding:16px 17px;background:rgba(255,255,255,.04);
 border:1px solid rgba(255,255,255,.09);transition:transform .18s,border-color .18s}
.card:hover{transform:translateY(-4px);border-color:rgba(255,255,255,.22)}
.card .p{font-size:1rem;font-weight:700;color:#e6edff}
.card .d{font-family:'JetBrains Mono',monospace;font-size:1.5rem;font-weight:800;margin:.28rem 0 .3rem}
.card.buy .d{color:#00f5b0}.card.sell .d{color:#ff3b6b}
.card.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.20)}
.card.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.18)}
.cb{display:flex;gap:5px;align-items:center}
.cb .b{width:22px;height:6px;border-radius:4px;background:rgba(255,255,255,.10)}
.card.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee)}
.card.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c)}
.cb .lab{margin-left:6px;font-size:.62rem;letter-spacing:1.5px;font-weight:800;color:#9aa8cc}
/* tabela de desempenho */
.perf{width:100%;border-collapse:separate;border-spacing:0 8px;font-size:.86rem}
.perf th{text-align:left;font-size:.66rem;letter-spacing:1.6px;color:#8496bd;font-weight:800;padding:0 14px}
.perf td{background:rgba(255,255,255,.04);border-top:1px solid rgba(255,255,255,.07);
 border-bottom:1px solid rgba(255,255,255,.07);padding:13px 14px}
.perf tr td:first-child{border-left:1px solid rgba(255,255,255,.07);border-radius:12px 0 0 12px}
.perf tr td:last-child{border-right:1px solid rgba(255,255,255,.07);border-radius:0 12px 12px 0}
.perf tr.best td{background:rgba(0,245,176,.09);border-color:rgba(0,245,176,.30)}
.perf .nm{font-weight:700;color:#e6edff}
.perf .wr{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:1.05rem}
.perf .good{color:#00f5b0}.perf .bad{color:#ff7a95}.perf .mid{color:#cbd6f0}
.perf .n{color:#8496bd;font-size:.76rem}
.badge{font-size:.58rem;font-weight:800;letter-spacing:1.4px;padding:3px 9px;border-radius:999px;
 background:rgba(0,245,176,.16);color:#7ef7d6;border:1px solid rgba(0,245,176,.35);margin-left:8px}
.badge.neutral{background:rgba(255,255,255,.06);color:#a9b7d8;border-color:rgba(255,255,255,.18)}
.warn{font-size:.68rem;color:#e8c07a;margin-left:8px}
.ci{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:#7f8fb3;margin-left:4px}
.verd{font-size:.6rem;font-weight:800;letter-spacing:1px;padding:2px 8px;border-radius:999px;margin-left:6px}
.v-good{background:rgba(0,245,176,.14);color:#7ef7d6;border:1px solid rgba(0,245,176,.32)}
.v-bad{background:rgba(255,59,107,.12);color:#ff8fa6;border:1px solid rgba(255,59,107,.30)}
.v-mid{background:rgba(232,192,122,.12);color:#e8c07a;border:1px solid rgba(232,192,122,.30)}
.rule{margin-top:18px;font-size:.73rem;color:#8697bd;background:rgba(255,255,255,.03);
 border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:10px 15px;line-height:1.5}
.rule b{color:#b9c6e8}
.footline{margin-top:14px;text-align:center;font-size:.68rem;color:#5c6a8e}
section[data-testid="stSidebar"]{background:rgba(9,13,26,.72)}
div[role="radiogroup"]{gap:8px}
div[role="radiogroup"] label{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.10);
 padding:8px 16px;border-radius:12px;font-weight:700}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("#### ⚡ Sinais IA")
    show_closed = st.toggle("Incluir pares fora de sessão", value=False)
    payout_lbl = st.radio("Payout", ["80%", "90%"], index=0, horizontal=True)
    PAYOUT = 0.80 if payout_lbl == "80%" else 0.90
    BE = breakeven(PAYOUT) * 100
    st.caption(f"Breakeven com payout {payout_lbl}: **{BE:.2f}%**. "
               "O painel só afirma vantagem se todo o intervalo ficar acima disso.")

st.markdown('<div class="brand"><span class="logo">⚡</span><span class="name">Sinais IA</span>'
            '<span class="live">MULTI-ESTRATÉGIA</span></div>', unsafe_allow_html=True)

c1, c2 = st.columns([1, 1.5])
with c1:
    tf_label = st.radio("TF", ["1 min", "5 min", "15 min"], index=1, horizontal=True,
                        label_visibility="collapsed")
    TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]
with c2:
    strat_name = st.selectbox("Estratégia", list(STRATEGIES), index=0, label_visibility="collapsed")

st_autorefresh(interval=15000, key="auto")
now = datetime.now(timezone.utc)
interval, minutes = TF_YF[TF], int(TF)

open_assets = [a for a in ASSETS if pair_open(a, now)]
scan_list = ASSETS if show_closed else open_assets
data = get_data(scan_list, interval, minutes)

# ---------------- scanner da estratégia escolhida ----------------
entries = []
for a in scan_list:
    df = data.get(a["name"])
    if df is None or len(df) < 60:
        continue
    closed = df.iloc[:-1]                      # descarta a vela em formação
    d = add_indicators(closed)
    sc = score_of(strat_name, d, interval)
    last = float(sc.iloc[-1]) if len(sc) else 0.0
    r = classify(last)
    if r:
        entries.append({"a": a, "dir": r[0], "force": r[1], "score": abs(last)})
entries.sort(key=lambda e: e["score"], reverse=True)

# ---------------- backtest das 4 estratégias (hoje / período) ----------------
def run_perf():
    today = now.date()
    out = {}
    for name in STRATEGIES:
        agg = {"hoje": [0, 0], "per": [0, 0]}
        for a in scan_list:
            df = data.get(a["name"])
            if df is None or len(df) < 80:
                continue
            d = add_indicators(df)
            sc = score_of(name, d, interval)
            r_all = backtest(d, sc)
            agg["per"][0] += r_all["trades"]; agg["per"][1] += r_all["wins"]
            mask = d.index.date == today
            if mask.any():
                dd = d[mask]
                r_day = backtest(dd, sc[mask])
                agg["hoje"][0] += r_day["trades"]; agg["hoje"][1] += r_day["wins"]
        out[name] = agg
    return out


perf_cache = st.session_state.setdefault("perf", {})
pk = (interval, candle_key(minutes), len(scan_list))
if pk not in perf_cache:
    perf_cache.clear()
    perf_cache[pk] = run_perf()
perf = perf_cache[pk]

# ---------------- sessões + contador ----------------
if market_open(now):
    ses = "".join(f'<span class="pill ses">🟢 {s}</span>' for s in active_sessions(now))
    op = "".join(f'<span class="pill on">{a["name"]}</span>' for a in open_assets if a["type"] == "fx")
    st.markdown(f'<div class="sess"><b>Sessões:</b> {ses} <b style="margin-left:6px">Varredura:</b> {op or "—"} '
                f'<span class="pill on">+cripto</span></div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="sess">🔴 <b>Forex fechado</b> — varrendo apenas cripto (24/7).</div>',
                unsafe_allow_html=True)

components.html(f"""
<div style="font-family:'JetBrains Mono',monospace;color:#eaf0ff;background:rgba(255,255,255,.05);
 border:1px solid rgba(255,255,255,.10);border-radius:16px;padding:13px 20px;display:flex;
 align-items:center;gap:20px">
 <div style="font-size:.64rem;letter-spacing:2px;color:#93a4c8">PRÓXIMA VELA · {TF_LABEL[TF]}</div>
 <div id="clk" style="font-size:2rem;font-weight:800;letter-spacing:3px">--:--</div>
 <div id="ent"></div><div style="flex:1"></div>
 <div style="height:9px;flex:0 0 260px;border-radius:6px;background:rgba(255,255,255,.10);overflow:hidden">
  <div id="pb" style="height:100%;width:0%;background:linear-gradient(90deg,#00f5b0,#22d3ee)"></div></div>
</div>
<style>@import url('https://fonts.googleapis.com/css2?family=Inter:wght@800&family=JetBrains+Mono:wght@800&display=swap');
@keyframes np{{0%{{transform:scale(.96);opacity:.6}}50%{{transform:scale(1.05);opacity:1;box-shadow:0 0 28px rgba(0,245,176,.75)}}100%{{transform:scale(.96);opacity:.6}}}}</style>
<script>var TF={int(TF)};function t(){{var n=Date.now()/1000,per=TF*60,pos=n%per,left=per-pos,
m=Math.floor(left/60),s=Math.floor(left%60);document.getElementById('clk').textContent=(m<10?'0':'')+m+':'+(s<10?'0':'')+s;
document.getElementById('pb').style.width=((pos/per)*100).toFixed(1)+'%';
document.getElementById('ent').innerHTML=pos<12?'<span style="font-family:Inter,sans-serif;font-weight:800;letter-spacing:1px;padding:6px 14px;border-radius:999px;color:#04120d;background:linear-gradient(90deg,#00f5b0,#22d3ee);animation:np 1s infinite">● NOVA ENTRADA</span>':'';}}
t();setInterval(t,1000);</script>
""", height=68)

FL = {"FRACA": "FRACA", "MEDIA": "MÉDIA", "FORTE": "FORTE"}
NB = {"FRACA": 1, "MEDIA": 2, "FORTE": 3}


def bars(f, cls=""):
    n = NB.get(f, 0)
    return "".join(f'<span class="b {"on" if i < n else ""}"></span>' for i in range(3))


def hero(e):
    cls = "buy" if e["dir"] == "COMPRA" else "sell"
    arw = "▲" if e["dir"] == "COMPRA" else "▼"
    return (f'<div class="hero-sig {cls}"><div class="glow"></div><div class="tag">MELHOR ENTRADA</div>'
            f'<div class="pair">{e["a"]["name"]}</div>'
            f'<div class="dir"><span class="arw">{arw}</span> {e["dir"]}</div>'
            f'<div class="fbars">{bars(e["force"])}<span class="flabel">FORÇA {FL[e["force"]]}</span></div></div>')


def hero_empty(name):
    return ('<div class="hero-sig wait"><div class="glow"></div><div class="tag">SCANNER</div>'
            f'<div class="pair">{name}</div>'
            '<div class="dir"><span class="arw">◵</span> NENHUMA ENTRADA</div>'
            '<div class="sub">AGUARDANDO SETUP — PRÓXIMA VELA</div></div>')


def card(e):
    cls = "buy" if e["dir"] == "COMPRA" else "sell"
    arw = "▲" if e["dir"] == "COMPRA" else "▼"
    return (f'<div class="card {cls}"><div class="p">{e["a"]["name"]}</div>'
            f'<div class="d">{arw} {e["dir"]}</div>'
            f'<div class="cb">{bars(e["force"])}<span class="lab">{FL[e["force"]]}</span></div></div>')


if entries:
    st.markdown(hero(entries[0]), unsafe_allow_html=True)
    st.markdown(f'<div class="gtitle">Outras entradas · {strat_name} · {TF_LABEL[TF]} '
                f'<small>({len(entries)} no total)</small></div>', unsafe_allow_html=True)
    if entries[1:]:
        st.markdown(f'<div class="grid">{"".join(card(e) for e in entries[1:])}</div>', unsafe_allow_html=True)
else:
    st.markdown(hero_empty(strat_name), unsafe_allow_html=True)
    st.markdown(f'<div class="gtitle">Sem entrada para <b>{strat_name}</b> agora '
                '<small>— troque a estratégia ou aguarde a próxima vela</small></div>',
                unsafe_allow_html=True)

# ---------------- painel de desempenho ----------------
VERD_STYLE = {
    "acima": ("v-good", "acima do breakeven"),
    "abaixo": ("v-bad", "abaixo do breakeven"),
    "inconclusivo": ("v-mid", "não conclusivo"),
    "sem dados": ("v-mid", "sem dados"),
}


def wr_cell(n, w):
    """Taxa + intervalo de confiança 95% (Wilson) + veredito contra o breakeven."""
    if n == 0:
        return '<span class="wr mid">—</span><br><span class="n">0 ops</span>'
    p, lo, hi = wilson_ci(w, n)
    v = verdict(w, n, PAYOUT)
    vcls, vtxt = VERD_STYLE[v]
    cls = "good" if v == "acima" else ("bad" if v == "abaixo" else "mid")
    return (f'<span class="wr {cls}">{p*100:.1f}%</span> '
            f'<span class="ci">IC95 {lo*100:.0f}–{hi*100:.0f}%</span><br>'
            f'<span class="n">{n} ops</span> <span class="verd {vcls}">{vtxt}</span>')


ranked = sorted(STRATEGIES, key=lambda k: (perf[k]["per"][1] / perf[k]["per"][0]) if perf[k]["per"][0] else 0,
                reverse=True)
top = ranked[0] if perf[ranked[0]]["per"][0] else None
# só destaca se houver vantagem estatística de verdade
top_proven = bool(top) and verdict(perf[top]["per"][1], perf[top]["per"][0], PAYOUT) == "acima"

rows = ""
for name in STRATEGIES:
    p = perf[name]
    is_top = (name == top)
    badge = ""
    if is_top:
        badge = ('<span class="badge">VANTAGEM COMPROVADA</span>' if top_proven
                 else '<span class="badge neutral">MAIOR TAXA · não comprovada</span>')
    sel = ' · <span class="n">em uso</span>' if name == strat_name else ""
    rows += (f'<tr class="{"best" if (is_top and top_proven) else ""}">'
             f'<td class="nm">{name}{badge}{sel}</td>'
             f'<td>{wr_cell(*p["hoje"])}</td>'
             f'<td>{wr_cell(*p["per"])}</td></tr>')

st.markdown(f'<div class="gtitle">Desempenho das estratégias · {TF_LABEL[TF]} '
            f'<small>(medido nos candles reais, entrada na abertura da vela seguinte)</small></div>',
            unsafe_allow_html=True)
st.markdown(f'<table class="perf"><tr><th>ESTRATÉGIA</th><th>HOJE</th>'
            f'<th>PERÍODO ({TF_PERIOD[interval]})</th></tr>{rows}</table>', unsafe_allow_html=True)

st.markdown(f'<div class="rule"><b>Como ler:</b> a taxa é medida operando toda vez que a estratégia '
            f'dispara, entrando na <b>abertura da vela seguinte</b>. <b>Acerto:</b> COMPRA vence se a vela '
            f'fechar <b style="color:#00f5b0">verde</b>; VENDA vence se fechar '
            f'<b style="color:#ff3b6b">vermelha</b>.<br>'
            f'<b>IC95</b> é a faixa em que a taxa real provavelmente está. Com o payout de {payout_lbl}, '
            f'o breakeven é <b>{BE:.2f}%</b> — só existe vantagem se <b>toda</b> a faixa ficar acima disso. '
            f'Quando a faixa cruza o breakeven o resultado é <b>não conclusivo</b>: a diferença entre as '
            f'estratégias ainda pode ser acaso, e escolher pela maior taxa é perseguir ruído.</div>',
            unsafe_allow_html=True)

st.markdown('<div class="footline">Uso próprio · não é recomendação financeira</div>', unsafe_allow_html=True)
