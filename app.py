"""
app.py — Sinais IA · scanner multi-estratégia com backtest integrado.

Layout de produto:
  · Barra de status compacta (mercado, sessões, contador de vela)
  · Aba SINAIS      — operar: melhor entrada + grade de entradas
  · Aba DESEMPENHO  — analisar: backtest das estratégias com IC e veredito
  · Painel lateral  — todos os ajustes agrupados por seção

O mesmo código (strategies.py) roda ao vivo e no backtest.
Regra: entrada na ABERTURA da vela seguinte; acerto pela COR da vela.
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
                        backtest, wilson_ci, breakeven, verdict)

socket.setdefaulttimeout(8)
st.set_page_config(page_title="Sinais IA", page_icon="⚡", layout="wide",
                   initial_sidebar_state="expanded")

ASSETS = [
    {"name": "EUR/USD", "yf": "EURUSD=X", "cur": ["EUR", "USD"], "type": "fx", "voz": "Euro Dólar"},
    {"name": "GBP/USD", "yf": "GBPUSD=X", "cur": ["GBP", "USD"], "type": "fx", "voz": "Libra Dólar"},
    {"name": "USD/JPY", "yf": "USDJPY=X", "cur": ["USD", "JPY"], "type": "fx", "voz": "Dólar Iene"},
    {"name": "AUD/USD", "yf": "AUDUSD=X", "cur": ["AUD", "USD"], "type": "fx", "voz": "Dólar Australiano"},
    {"name": "USD/CAD", "yf": "USDCAD=X", "cur": ["USD", "CAD"], "type": "fx", "voz": "Dólar Canadense"},
    {"name": "USD/CHF", "yf": "USDCHF=X", "cur": ["USD", "CHF"], "type": "fx", "voz": "Dólar Franco"},
    {"name": "NZD/USD", "yf": "NZDUSD=X", "cur": ["NZD", "USD"], "type": "fx", "voz": "Dólar Neozelandês"},
    {"name": "EUR/JPY", "yf": "EURJPY=X", "cur": ["EUR", "JPY"], "type": "fx", "voz": "Euro Iene"},
    {"name": "BTC/USD", "yf": "BTC-USD", "cur": [], "type": "crypto", "voz": "Bitcoin"},
    {"name": "ETH/USD", "yf": "ETH-USD", "cur": [], "type": "crypto", "voz": "Ethereum"},
]
SESSIONS = {"Sydney": (21, 6), "Tóquio": (23, 8), "Londres": (7, 16), "Nova York": (12, 21)}
CUR_SESS = {"AUD": "Sydney", "NZD": "Sydney", "JPY": "Tóquio", "EUR": "Londres",
            "GBP": "Londres", "CHF": "Londres", "USD": "Nova York", "CAD": "Nova York"}
TF_LABEL = {"1": "1 min", "5": "5 min", "15": "15 min"}
TF_YF = {"1": "1m", "5": "5m", "15": "15m"}
TF_PERIOD = {"1m": "7d", "5m": "1mo", "15m": "1mo"}
FORCE_ORDER = {"FRACA": 1, "MEDIA": 2, "FORTE": 3}
FL = {"FRACA": "FRACA", "MEDIA": "MÉDIA", "FORTE": "FORTE"}


def market_open(d):
    wd, h = d.weekday(), d.hour
    if wd == 5 or (wd == 6 and h < 21) or (wd == 4 and h >= 21):
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


# ============================== ESTILO ==============================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@600;800&display=swap');
:root{--buy:#00e5a0;--sell:#ff4d6d;--ink:#e8eeff;--mut:#8fa0c4;--line:rgba(255,255,255,.08);
 --surf:rgba(255,255,255,.035);--surf2:rgba(255,255,255,.06);}
.stApp{background:radial-gradient(1100px 520px at 10% -10%,#15224a 0%,rgba(8,11,20,0) 60%),
 radial-gradient(800px 420px at 95% -5%,#0a2f3d 0%,rgba(8,11,20,0) 55%),linear-gradient(#080b14,#05070e);
 color:var(--ink);font-family:'Inter',sans-serif;}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:1.2rem;padding-bottom:3rem;max-width:1120px}
h1,h2,h3{font-weight:700}
/* ---- topbar ---- */
.topbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
 background:var(--surf);border:1px solid var(--line);border-radius:16px;padding:14px 20px;margin-bottom:6px}
.tb-brand{display:flex;align-items:center;gap:9px;font-weight:800;font-size:1.05rem;letter-spacing:.2px}
.tb-brand .dot{width:8px;height:8px;border-radius:50%;background:var(--buy);box-shadow:0 0 10px var(--buy)}
.tb-sep{width:1px;height:26px;background:var(--line)}
.tb-item{display:flex;flex-direction:column;gap:2px}
.tb-k{font-size:.6rem;letter-spacing:1.6px;color:var(--mut);font-weight:700}
.tb-v{font-size:.85rem;font-weight:700}
.chip{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.68rem;font-weight:700;
 border:1px solid var(--line);background:var(--surf2);color:#a9bbdd;margin-right:5px}
.chip.live{color:#052018;background:linear-gradient(90deg,#00e5a0,#22d3ee);border:0}
.chip.off{color:#ff9db0;border-color:rgba(255,77,109,.3)}
/* ---- hero ---- */
.hero{position:relative;border-radius:20px;padding:28px 32px;overflow:hidden;margin-top:4px;
 background:linear-gradient(150deg,rgba(255,255,255,.06),rgba(255,255,255,.015));
 border:1px solid var(--line)}
.hero .tag{position:absolute;top:18px;right:20px;font-size:.58rem;font-weight:800;letter-spacing:1.8px;
 padding:5px 11px;border-radius:999px;background:var(--surf2);color:#b3c1e0;border:1px solid var(--line)}
.hero .pair{font-size:1.15rem;font-weight:700;letter-spacing:1.5px;color:#dbe4ff}
.hero .dir{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:3.6rem;line-height:1.05;
 margin:.3rem 0 .1rem;display:flex;align-items:center;gap:14px}
.hero .arw{font-size:2.4rem}
.hero .sub{font-size:.75rem;letter-spacing:2.4px;color:var(--mut);font-weight:700}
.buy .dir{color:var(--buy)} .sell .dir{color:var(--sell)} .wait .dir{color:#93a4c8;font-size:2.2rem}
.hero.buy{box-shadow:inset 0 0 0 1px rgba(0,229,160,.26)}
.hero.sell{box-shadow:inset 0 0 0 1px rgba(255,77,109,.24)}
.hero .glow{position:absolute;right:-80px;top:-80px;width:280px;height:280px;border-radius:50%;filter:blur(80px);opacity:.30}
.buy .glow{background:var(--buy)} .sell .glow{background:var(--sell)} .wait .glow{background:#33406b;opacity:.18}
/* ---- barras de força ---- */
.fb{display:flex;gap:6px;align-items:center;margin-top:14px}
.fb .b{width:44px;height:9px;border-radius:5px;background:rgba(255,255,255,.10)}
.buy .b.on{background:var(--buy)} .sell .b.on{background:var(--sell)}
.fb .lbl{margin-left:11px;font-size:.7rem;letter-spacing:2.2px;font-weight:800;color:#aab8da}
/* ---- chips de estratégia ---- */
.strats{margin-top:14px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.sc{font-size:.64rem;font-weight:600;padding:4px 10px;border-radius:8px;
 background:var(--surf2);color:#c0cdec;border:1px solid var(--line)}
.conf{font-size:.58rem;font-weight:800;letter-spacing:1.2px;padding:4px 10px;border-radius:8px;
 background:rgba(0,229,160,.14);color:#7df0c6;border:1px solid rgba(0,229,160,.32)}
/* ---- grade ---- */
.sect{margin:26px 0 12px;font-size:.72rem;font-weight:800;letter-spacing:2px;color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:13px}
.card{border-radius:14px;padding:16px 17px;background:var(--surf);border:1px solid var(--line);
 transition:transform .16s ease,border-color .16s ease}
.card:hover{transform:translateY(-3px);border-color:rgba(255,255,255,.20)}
.card .p{font-size:.95rem;font-weight:700;color:#dfe7ff}
.card .d{font-family:'JetBrains Mono',monospace;font-size:1.35rem;font-weight:800;margin:.25rem 0 .3rem}
.card.buy .d{color:var(--buy)} .card.sell .d{color:var(--sell)}
.card.buy{box-shadow:inset 0 0 0 1px rgba(0,229,160,.16)}
.card.sell{box-shadow:inset 0 0 0 1px rgba(255,77,109,.14)}
.card .fb .b{width:22px;height:6px} .card .fb .lbl{margin-left:6px;font-size:.6rem;letter-spacing:1.4px}
.card .strats{margin-top:9px} .card .sc{font-size:.58rem;padding:3px 8px}
/* ---- tabela ---- */
.perf{width:100%;border-collapse:separate;border-spacing:0 7px;font-size:.85rem}
.perf th{text-align:left;font-size:.6rem;letter-spacing:1.6px;color:var(--mut);font-weight:800;padding:0 14px}
.perf td{background:var(--surf);border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:12px 14px}
.perf tr td:first-child{border-left:1px solid var(--line);border-radius:11px 0 0 11px}
.perf tr td:last-child{border-right:1px solid var(--line);border-radius:0 11px 11px 0}
.perf tr.on td{background:rgba(0,229,160,.06)}
.perf .nm{font-weight:700;color:#e2eaff}
.perf .wr{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:1rem}
.good{color:var(--buy)} .bad{color:#ff8fa6} .mid{color:#ccd6f0}
.ci{font-family:'JetBrains Mono',monospace;font-size:.68rem;color:#7f8fb3;margin-left:4px}
.n{color:var(--mut);font-size:.72rem}
.verd{font-size:.58rem;font-weight:800;letter-spacing:.8px;padding:2px 8px;border-radius:6px;margin-left:6px}
.v-good{background:rgba(0,229,160,.13);color:#7df0c6;border:1px solid rgba(0,229,160,.3)}
.v-bad{background:rgba(255,77,109,.11);color:#ff8fa6;border:1px solid rgba(255,77,109,.28)}
.v-mid{background:rgba(232,192,122,.11);color:#e8c07a;border:1px solid rgba(232,192,122,.28)}
.tagmini{font-size:.55rem;font-weight:800;letter-spacing:1px;padding:2px 7px;border-radius:6px;
 background:var(--surf2);color:#a9b7d8;border:1px solid var(--line);margin-left:7px}
.note{margin-top:16px;font-size:.72rem;color:#8697bd;background:var(--surf);
 border:1px solid var(--line);border-radius:12px;padding:12px 16px;line-height:1.55}
.note b{color:#b9c6e8}
.foot{margin-top:26px;text-align:center;font-size:.66rem;color:#55638a}
/* ---- widgets ---- */
section[data-testid="stSidebar"]{background:rgba(8,11,20,.86);border-right:1px solid var(--line)}
section[data-testid="stSidebar"] .block-container{padding-top:1.2rem}
div[role="radiogroup"]{gap:6px}
div[role="radiogroup"] label{background:var(--surf);border:1px solid var(--line);
 padding:6px 13px;border-radius:9px;font-weight:600;font-size:.82rem}
.stTabs [data-baseweb="tab-list"]{gap:4px;border-bottom:1px solid var(--line)}
.stTabs [data-baseweb="tab"]{background:transparent;border-radius:9px 9px 0 0;padding:9px 18px;
 font-weight:700;font-size:.85rem;color:var(--mut)}
.stTabs [aria-selected="true"]{background:var(--surf);color:var(--ink)}
</style>
""", unsafe_allow_html=True)

# ============================== AJUSTES (sidebar) ==============================
with st.sidebar:
    st.markdown('<div class="tb-brand"><span class="dot"></span>Sinais IA</div>', unsafe_allow_html=True)
    st.caption("Scanner multi-estratégia")
    st.divider()

    st.markdown("**Operação**")
    tf_label = st.radio("Timeframe", ["1 min", "5 min", "15 min"], index=1, horizontal=True)
    TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]
    default_sel = [k for k in ("G · Fade vela extrema", "J · Z-score forte", "K · Reversão dupla")
                   if k in STRATEGIES]
    sel_strats = st.multiselect("Estratégias ativas", list(STRATEGIES), default=default_sel,
                                placeholder="Escolha uma ou mais")
    if not sel_strats:
        sel_strats = default_sel or [list(STRATEGIES)[0]]
    min_force = st.select_slider("Força mínima", options=["FRACA", "MÉDIA", "FORTE"], value="FRACA")
    only_conf = st.toggle("Só entradas com 2+ estratégias", value=False)

    st.divider()
    st.markdown("**Áudio**")
    audio_on = st.toggle("🔊 Aviso por voz na entrada", value=False)
    if audio_on:
        st.caption("O navegador exige um clique antes de liberar som — use o botão na aba Sinais.")

    st.divider()
    st.markdown("**Análise**")
    payout_lbl = st.radio("Payout da corretora", ["80%", "90%"], index=0, horizontal=True)
    PAYOUT = 0.80 if payout_lbl == "80%" else 0.90
    BE = breakeven(PAYOUT) * 100
    st.caption(f"Breakeven: **{BE:.2f}%** — só há vantagem se todo o intervalo ficar acima disso.")

    st.divider()
    with st.expander("Avançado"):
        show_closed = st.toggle("Incluir pares fora de sessão", value=False)
        auto_on = st.toggle("Atualização automática", value=True)
        every = st.slider("Intervalo (s)", 10, 60, 15, step=5, disabled=not auto_on)

if auto_on:
    st_autorefresh(interval=every * 1000, key="auto")

now = datetime.now(timezone.utc)
interval, minutes = TF_YF[TF], int(TF)
open_assets = [a for a in ASSETS if pair_open(a, now)]
scan_list = ASSETS if show_closed else open_assets
data = get_data(scan_list, interval, minutes)

# ============================== SCANNER ==============================
agg = {}
for a in scan_list:
    df = data.get(a["name"])
    if df is None or len(df) < 60:
        continue
    d = add_indicators(df.iloc[:-1])           # descarta a vela em formação
    for nm in sel_strats:
        sc = score_of(nm, d, interval)
        last = float(sc.iloc[-1]) if len(sc) else 0.0
        r = classify(last)
        if not r:
            continue
        key = (a["name"], r[0])
        e = agg.setdefault(key, {"a": a, "dir": r[0], "force": r[1], "score": abs(last), "strats": []})
        e["strats"].append(nm)
        e["score"] = max(e["score"], abs(last))
        if FORCE_ORDER[r[1]] > FORCE_ORDER[e["force"]]:
            e["force"] = r[1]

entries = list(agg.values())
minf = {"FRACA": 1, "MÉDIA": 2, "FORTE": 3}[min_force]
entries = [e for e in entries if FORCE_ORDER[e["force"]] >= minf]
if only_conf:
    entries = [e for e in entries if len(e["strats"]) > 1]
entries.sort(key=lambda e: (len(e["strats"]), FORCE_ORDER[e["force"]], e["score"]), reverse=True)


# ============================== DESEMPENHO ==============================
def run_perf():
    today = now.date()
    out = {}
    for name in STRATEGIES:
        acc = {"hoje": [0, 0], "per": [0, 0]}
        for a in scan_list:
            df = data.get(a["name"])
            if df is None or len(df) < 80:
                continue
            d = add_indicators(df)
            sc = score_of(name, d, interval)
            r = backtest(d, sc)
            acc["per"][0] += r["trades"]; acc["per"][1] += r["wins"]
            m = d.index.date == today
            if m.any():
                rd = backtest(d[m], sc[m])
                acc["hoje"][0] += rd["trades"]; acc["hoje"][1] += rd["wins"]
        out[name] = acc
    return out


pc = st.session_state.setdefault("perf", {})
pk = (interval, candle_key(minutes), len(scan_list))
if pk not in pc:
    pc.clear(); pc[pk] = run_perf()
perf = pc[pk]

# ============================== TOPBAR ==============================
if market_open(now):
    sess = "".join(f'<span class="chip live">{s}</span>' for s in active_sessions(now))
    status = f'<span class="chip live">MERCADO ABERTO</span>'
else:
    sess = '<span class="chip off">fim de semana</span>'
    status = '<span class="chip off">FOREX FECHADO</span>'
st.markdown(f"""
<div class="topbar">
  <div class="tb-brand"><span class="dot"></span>Sinais IA</div>
  <div class="tb-sep"></div>
  <div class="tb-item"><span class="tb-k">STATUS</span><span class="tb-v">{status}</span></div>
  <div class="tb-item"><span class="tb-k">SESSÕES</span><span class="tb-v">{sess}</span></div>
  <div class="tb-item"><span class="tb-k">TIMEFRAME</span><span class="tb-v">{TF_LABEL[TF]}</span></div>
  <div class="tb-item"><span class="tb-k">ESTRATÉGIAS</span><span class="tb-v">{len(sel_strats)} ativa(s)</span></div>
  <div class="tb-item"><span class="tb-k">VARREDURA</span><span class="tb-v">{len(scan_list)} ativos</span></div>
</div>""", unsafe_allow_html=True)

components.html(f"""
<div style="font-family:'JetBrains Mono',monospace;color:#e8eeff;background:rgba(255,255,255,.04);
 border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:12px 20px;display:flex;
 align-items:center;gap:18px">
 <div style="font-size:.6rem;letter-spacing:1.8px;color:#8fa0c4;font-family:Inter,sans-serif;font-weight:700">PRÓXIMA VELA</div>
 <div id="clk" style="font-size:1.7rem;font-weight:800;letter-spacing:2px">--:--</div>
 <div id="ent"></div><div style="flex:1"></div>
 <div style="height:7px;flex:0 0 240px;border-radius:5px;background:rgba(255,255,255,.09);overflow:hidden">
  <div id="pb" style="height:100%;width:0%;background:linear-gradient(90deg,#00e5a0,#22d3ee)"></div></div>
</div>
<style>@import url('https://fonts.googleapis.com/css2?family=Inter:wght@700;800&family=JetBrains+Mono:wght@800&display=swap');
@keyframes np{{0%{{opacity:.55}}50%{{opacity:1}}100%{{opacity:.55}}}}</style>
<script>var TF={int(TF)};function t(){{var n=Date.now()/1000,per=TF*60,pos=n%per,left=per-pos,
m=Math.floor(left/60),s=Math.floor(left%60);document.getElementById('clk').textContent=(m<10?'0':'')+m+':'+(s<10?'0':'')+s;
document.getElementById('pb').style.width=((pos/per)*100).toFixed(1)+'%';
document.getElementById('ent').innerHTML=pos<12?'<span style="font-family:Inter,sans-serif;font-weight:800;font-size:.7rem;letter-spacing:1px;padding:5px 12px;border-radius:999px;color:#04120d;background:linear-gradient(90deg,#00e5a0,#22d3ee);animation:np 1s infinite">● NOVA ENTRADA</span>':'';}}
t();setInterval(t,1000);</script>""", height=60)

tab_sig, tab_perf = st.tabs(["📡  Sinais", "📊  Desempenho"])


def bars(f):
    n = FORCE_ORDER.get(f, 0)
    return "".join(f'<span class="b {"on" if i < n else ""}"></span>' for i in range(3))


def _short(nm):
    return nm.split("·")[0].strip()


def chips(e, big=False):
    n = len(e["strats"])
    c = f'<span class="conf">{n} ESTRATÉGIAS CONCORDAM</span>' if n > 1 else ""
    if big:
        c += "".join(f'<span class="sc">{s}</span>' for s in e["strats"])
    else:
        c += "".join(f'<span class="sc">{_short(s)}</span>' for s in e["strats"])
    return f'<div class="strats">{c}</div>'


# ============================== ABA SINAIS ==============================
with tab_sig:
    if entries:
        e = entries[0]
        cls = "buy" if e["dir"] == "COMPRA" else "sell"
        arw = "▲" if e["dir"] == "COMPRA" else "▼"
        st.markdown(f"""<div class="hero {cls}"><div class="glow"></div>
          <div class="tag">MELHOR ENTRADA</div>
          <div class="pair">{e["a"]["name"]}</div>
          <div class="dir"><span class="arw">{arw}</span> {e["dir"]}</div>
          <div class="fb">{bars(e["force"])}<span class="lbl">FORÇA {FL[e["force"]]}</span></div>
          {chips(e, big=True)}</div>""", unsafe_allow_html=True)
        rest = entries[1:]
        st.markdown(f'<div class="sect">OUTRAS ENTRADAS · {len(entries)} NO TOTAL</div>',
                    unsafe_allow_html=True)
        if rest:
            cards = ""
            for e in rest:
                cls = "buy" if e["dir"] == "COMPRA" else "sell"
                arw = "▲" if e["dir"] == "COMPRA" else "▼"
                cards += (f'<div class="card {cls}"><div class="p">{e["a"]["name"]}</div>'
                          f'<div class="d">{arw} {e["dir"]}</div>'
                          f'<div class="fb">{bars(e["force"])}<span class="lbl">{FL[e["force"]]}</span></div>'
                          f'{chips(e)}</div>')
            st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)
        else:
            st.caption("Esta é a única entrada no momento.")
    else:
        st.markdown("""<div class="hero wait"><div class="glow"></div>
          <div class="tag">SCANNER</div>
          <div class="pair">MERCADO</div>
          <div class="dir"><span class="arw">◵</span> NENHUMA ENTRADA</div>
          <div class="sub">AGUARDANDO SETUP — PRÓXIMA VELA</div></div>""", unsafe_allow_html=True)
        st.caption("Nenhum ativo atende aos critérios agora. Ter poucas ou nenhuma entrada é o normal.")

    if audio_on:
        if entries:
            top = entries[0]
            ests = ", ".join(_short(s) for s in top["strats"])
            pl = "estratégias" if len(top["strats"]) > 1 else "estratégia"
            fala = (f"Entrada agora. {top['a']['voz']}. {top['dir']}. "
                    f"{pl} {ests}. Força {FL[top['force']].lower()}.")
        else:
            fala = ""
        components.html(f"""
        <div style="font-family:Inter,sans-serif;margin-top:6px">
          <button id="u" style="background:rgba(0,229,160,.12);color:#7df0c6;
            border:1px solid rgba(0,229,160,.3);border-radius:9px;padding:7px 14px;
            font-weight:700;cursor:pointer;font-size:.75rem">🔊 Ativar / testar voz</button>
          <span id="s" style="color:#8697bd;font-size:.7rem;margin-left:9px"></span>
        </div>
        <script>
        var TF={int(TF)}, FALA={fala!r};
        function say(t){{try{{var u=new SpeechSynthesisUtterance(t);u.lang='pt-BR';u.rate=1.05;
          window.speechSynthesis.cancel();window.speechSynthesis.speak(u);}}catch(e){{}}}}
        document.getElementById('u').onclick=function(){{sessionStorage.setItem('voz','1');
          say('Voz ativada.');document.getElementById('s').textContent='voz ativada';}};
        if(sessionStorage.getItem('voz')==='1')document.getElementById('s').textContent='voz ativada';
        (function(){{if(!FALA)return;if(sessionStorage.getItem('voz')!=='1')return;
          var per=TF*60,n=Date.now()/1000,pos=n%per,c=Math.floor(n/per);
          if(pos<12&&sessionStorage.getItem('dito')!=String(c)){{
            sessionStorage.setItem('dito',String(c));say(FALA);}}}})();
        </script>""", height=44)

# ============================== ABA DESEMPENHO ==============================
with tab_perf:
    VS = {"acima": ("v-good", "acima do breakeven"), "abaixo": ("v-bad", "abaixo do breakeven"),
          "inconclusivo": ("v-mid", "não conclusivo"), "sem dados": ("v-mid", "sem dados")}

    def cell(n, w):
        if n == 0:
            return '<span class="wr mid">—</span><br><span class="n">0 ops</span>'
        p, lo, hi = wilson_ci(w, n)
        v = verdict(w, n, PAYOUT)
        vc, vt = VS[v]
        cls = "good" if v == "acima" else ("bad" if v == "abaixo" else "mid")
        return (f'<span class="wr {cls}">{p*100:.1f}%</span>'
                f'<span class="ci">IC95 {lo*100:.0f}–{hi*100:.0f}%</span><br>'
                f'<span class="n">{n} ops</span><span class="verd {vc}">{vt}</span>')

    ranked = sorted(STRATEGIES, key=lambda k: (perf[k]["per"][1] / perf[k]["per"][0]) if perf[k]["per"][0] else 0,
                    reverse=True)
    top = ranked[0] if perf[ranked[0]]["per"][0] else None
    proven = bool(top) and verdict(perf[top]["per"][1], perf[top]["per"][0], PAYOUT) == "acima"

    rows = ""
    for name in STRATEGIES:
        p = perf[name]
        tag = ""
        if name == top:
            tag = ('<span class="tagmini">VANTAGEM COMPROVADA</span>' if proven
                   else '<span class="tagmini">MAIOR TAXA · não comprovada</span>')
        on = ' <span class="tagmini">em uso</span>' if name in sel_strats else ""
        rows += (f'<tr class="{"on" if name in sel_strats else ""}">'
                 f'<td class="nm">{name}{tag}{on}</td>'
                 f'<td>{cell(*p["hoje"])}</td><td>{cell(*p["per"])}</td></tr>')

    st.markdown(f'<div class="sect">DESEMPENHO · {TF_LABEL[TF]} · PAYOUT {payout_lbl} '
                f'· BREAKEVEN {BE:.2f}%</div>', unsafe_allow_html=True)
    st.markdown(f'<table class="perf"><tr><th>ESTRATÉGIA</th><th>HOJE</th>'
                f'<th>PERÍODO ({TF_PERIOD[interval]})</th></tr>{rows}</table>', unsafe_allow_html=True)
    st.markdown('<div class="note"><b>Como ler:</b> a taxa é medida operando toda vez que a estratégia '
                'dispara, entrando na <b>abertura da vela seguinte</b>. Acerto pela cor da vela: COMPRA '
                'vence se fechar <b style="color:#00e5a0">verde</b>, VENDA se fechar '
                '<b style="color:#ff4d6d">vermelha</b>; empate devolve a aposta.<br>'
                '<b>IC95</b> é a faixa onde a taxa real provavelmente está. Só existe vantagem se '
                '<b>toda</b> a faixa ficar acima do breakeven — quando ela cruza, o resultado é '
                '<b>não conclusivo</b> e escolher pela maior taxa é perseguir ruído.</div>',
                unsafe_allow_html=True)

st.markdown('<div class="foot">Uso próprio · não é recomendação financeira</div>', unsafe_allow_html=True)
