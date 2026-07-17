"""
app.py — Sinais IA · SCANNER DE OPORTUNIDADES (uso próprio) · alta convicção.

Não escolhe ativo: varre os ativos de mercado aberto e mostra SÓ entradas que
passam em TODOS os gates (poucas ou nenhuma na maior parte do tempo — o normal).

GATES (todos obrigatórios):
  1) Confluência de 3 timeframes: o TF base + os 2 maiores relevantes, todos
     não-neutros e no MESMO lado.  1m→1m/5m/15m · 5m→5m/15m/1h · 15m→15m/1h/4h.
  2) Convicção FORTE: rating STRONG (STRONG_BUY/STRONG_SELL) no TF base E em
     pelo menos um dos maiores. Ratings fracos não passam.
  3) Volatilidade/momentum: velas recentes com corpo real (não-doji) e movimento
     líquido na direção do sinal. Mercado parado/lateral não gera entrada.
  4) Dedupe de correlacionados do dólar: se vários pares apontam o mesmo lado do
     USD, mostra só o mais forte.

Acerto (regra clara): COMPRA vence se a vela do timeframe fechar VERDE (fecha >
abre); VENDA vence se fechar VERMELHA (fecha < abre). Entrada no INÍCIO da vela.

Fonte: rating do TradingView (`tradingview_ta`) + OHLC do yfinance para o gate de
volatilidade. Timeout global, retries curtos, cache por vela, buscas paralelas.
"""
from __future__ import annotations
import math
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

socket.setdefaulttimeout(6)
st.set_page_config(page_title="Sinais IA · Scanner", page_icon="⚡", layout="wide")

ASSETS = [
    {"name": "EUR/USD", "tv": "FX_IDC:EURUSD", "yf": "EURUSD=X", "cur": ["EUR", "USD"], "type": "fx"},
    {"name": "GBP/USD", "tv": "FX_IDC:GBPUSD", "yf": "GBPUSD=X", "cur": ["GBP", "USD"], "type": "fx"},
    {"name": "USD/JPY", "tv": "FX_IDC:USDJPY", "yf": "USDJPY=X", "cur": ["USD", "JPY"], "type": "fx"},
    {"name": "AUD/USD", "tv": "FX_IDC:AUDUSD", "yf": "AUDUSD=X", "cur": ["AUD", "USD"], "type": "fx"},
    {"name": "USD/CAD", "tv": "FX_IDC:USDCAD", "yf": "USDCAD=X", "cur": ["USD", "CAD"], "type": "fx"},
    {"name": "USD/CHF", "tv": "FX_IDC:USDCHF", "yf": "USDCHF=X", "cur": ["USD", "CHF"], "type": "fx"},
    {"name": "NZD/USD", "tv": "FX_IDC:NZDUSD", "yf": "NZDUSD=X", "cur": ["NZD", "USD"], "type": "fx"},
    {"name": "EUR/JPY", "tv": "FX_IDC:EURJPY", "yf": "EURJPY=X", "cur": ["EUR", "JPY"], "type": "fx"},
    {"name": "BTC/USD", "tv": "BINANCE:BTCUSDT", "yf": "BTC-USD", "cur": [], "type": "crypto"},
    {"name": "ETH/USD", "tv": "BINANCE:ETHUSDT", "yf": "ETH-USD", "cur": [], "type": "crypto"},
]
SESSIONS = {"Sydney": (21, 6), "Tóquio": (23, 8), "Londres": (7, 16), "Nova York": (12, 21)}
CUR_SESS = {"AUD": "Sydney", "NZD": "Sydney", "JPY": "Tóquio", "EUR": "Londres",
            "GBP": "Londres", "CHF": "Londres", "USD": "Nova York", "CAD": "Nova York"}
TF_LABEL = {"1": "1 min", "5": "5 min", "15": "15 min"}
TF_YF = {"1": "1m", "5": "5m", "15": "15m"}
CONFLUENCE = {
    "1": ["INTERVAL_1_MINUTE", "INTERVAL_5_MINUTES", "INTERVAL_15_MINUTES"],
    "5": ["INTERVAL_5_MINUTES", "INTERVAL_15_MINUTES", "INTERVAL_1_HOUR"],
    "15": ["INTERVAL_15_MINUTES", "INTERVAL_1_HOUR", "INTERVAL_4_HOURS"],
}
INTV_MIN = {"INTERVAL_1_MINUTE": 1, "INTERVAL_5_MINUTES": 5, "INTERVAL_15_MINUTES": 15,
            "INTERVAL_1_HOUR": 60, "INTERVAL_4_HOURS": 240}
INTV_SHORT = {"INTERVAL_1_MINUTE": "1m", "INTERVAL_5_MINUTES": "5m", "INTERVAL_15_MINUTES": "15m",
              "INTERVAL_1_HOUR": "1h", "INTERVAL_4_HOURS": "4h"}
REC_CONV = {"STRONG_BUY": 2, "BUY": 1, "NEUTRAL": 0, "SELL": -1, "STRONG_SELL": -2}

# Gate 3 — limiares de volatilidade/momentum
MIN_BODY_PCT = 0.38     # corpo médio / range médio (baixo = dojis -> reprova)
MIN_DIR_STRENGTH = 0.14  # movimento líquido / (n * range médio) (baixo = lateral)


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


# ---------------------- TradingView (paralelo, last-good por vela) ----------------------
def _tv_once(screener, interval_name, symbols):
    from tradingview_ta import get_multiple_analysis, Interval
    res = get_multiple_analysis(screener=screener, interval=getattr(Interval, interval_name), symbols=list(symbols))
    return {k: (v.summary if v else None) for k, v in res.items()}


def _fetch_screener(screener, interval_name, symbols):
    for _ in range(2):
        try:
            return _tv_once(screener, interval_name, symbols)
        except Exception:
            time.sleep(0.3)
    return {}


def tv_all(interval_name, ck):
    store = st.session_state.setdefault("tv_store", {})
    meta = st.session_state.setdefault("tv_ck", {})
    prev = dict(store.get(interval_name, {}))
    if meta.get(interval_name) == ck and prev:
        return prev
    fx = tuple(a["tv"] for a in ASSETS if a["type"] == "fx")
    cr = tuple(a["tv"] for a in ASSETS if a["type"] == "crypto")
    merged = dict(prev)
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_fetch_screener, scr, interval_name, syms) for scr, syms in (("forex", fx), ("crypto", cr))]
        for f in as_completed(futs):
            for k, v in (f.result() or {}).items():
                if v:
                    merged[k] = v
    store[interval_name] = merged
    meta[interval_name] = ck
    return merged


def conv_at(interval_name):
    """Convicção {-2..2} por ativo (TradingView). None se sem rating."""
    tv = tv_all(interval_name, candle_key(INTV_MIN[interval_name]))
    out = {}
    for a in ASSETS:
        s = tv.get(a["tv"])
        out[a["tv"]] = REC_CONV.get(s.get("RECOMMENDATION", "NEUTRAL"), 0) if s else None
    return out


# ---------------------- Gate 3: volatilidade/momentum (yfinance OHLC) ----------------------
def _mom_compute(yf_symbol, yf_interval):
    try:
        import yfinance as yf
        import pandas as pd
        period = {"1m": "1d", "5m": "5d", "15m": "5d"}[yf_interval]
        df = yf.download(yf_symbol, interval=yf_interval, period=period,
                         progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty or len(df) < 12:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        o = df["Open"].astype(float); h = df["High"].astype(float)
        l = df["Low"].astype(float); c = df["Close"].astype(float)
        n = 10
        body = (c - o).tail(n)
        rng = (h - l).tail(n)
        avg_rng = float(rng.mean())
        if avg_rng <= 0:
            return None
        body_pct = float(body.abs().mean()) / avg_rng
        net = float(body.sum())
        dir_strength = abs(net) / (n * avg_rng)
        return (body_pct, dir_strength, 1 if net > 0 else -1)
    except Exception:
        return None


def momentum_pass(cands, yf_interval, minutes):
    cache = st.session_state.setdefault("mom_cache", {})
    ck = candle_key(minutes)
    todo = [c for c in cands if (c["a"]["yf"], yf_interval, ck) not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
            futs = {ex.submit(_mom_compute, c["a"]["yf"], yf_interval): c for c in todo}
            for f in as_completed(futs):
                cache[(futs[f]["a"]["yf"], yf_interval, ck)] = f.result()
    ok = []
    for c in cands:
        m = cache.get((c["a"]["yf"], yf_interval, ck))
        if not m:                       # None -> não verificável -> reprova (conservador)
            continue
        body_pct, dir_strength, move_dir = m
        want = 1 if c["dir"] == "COMPRA" else -1
        if body_pct >= MIN_BODY_PCT and dir_strength >= MIN_DIR_STRENGTH and move_dir == want:
            ok.append(c)
    return ok


# ---------------------- Gate 4: dedupe de correlacionados do dólar ----------------------
def usd_side(a, direction):
    if a["type"] != "fx" or "USD" not in a["cur"]:
        return None
    if a["cur"][0] == "USD":            # USD é base (USD/JPY, USD/CAD, USD/CHF)
        return "USD+" if direction == "COMPRA" else "USD-"
    return "USD-" if direction == "COMPRA" else "USD+"  # USD é cotação (EUR/USD...)


# ==========================================================================
# CSS
# ==========================================================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700;800&display=swap');
.stApp{background:
  radial-gradient(1200px 560px at 12% -8%, #182c60 0%, rgba(6,9,20,0) 55%),
  radial-gradient(920px 480px at 100% -6%, #0b3d4c 0%, rgba(6,9,20,0) 52%),
  linear-gradient(#070a17,#04060e);color:#eaf0ff;font-family:'Inter',sans-serif;}
.stApp:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.30;
  background-image:linear-gradient(rgba(120,150,255,.05) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(120,150,255,.05) 1px,transparent 1px);background-size:46px 46px;}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:1rem;max-width:1180px;position:relative;z-index:1}
.brand{display:flex;align-items:center;gap:12px}
.brand .logo{font-size:1.55rem;filter:drop-shadow(0 0 12px rgba(0,245,176,.6))}
.brand .name{font-size:1.55rem;font-weight:900;letter-spacing:.5px;
  background:linear-gradient(90deg,#8ff8db,#4fd2ff 55%,#c8a0ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.brand .live{font-size:.6rem;font-weight:800;letter-spacing:2px;color:#052018;
  background:linear-gradient(90deg,#00f5b0,#22d3ee);padding:4px 11px;border-radius:999px}
.brand .scan{font-size:.6rem;font-weight:700;letter-spacing:2px;color:#9db0d6;margin-left:2px}
.sess{margin:14px 0 4px;font-size:.82rem;color:#9db0d6;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.pill{padding:4px 11px;border-radius:999px;font-size:.73rem;font-weight:700;border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.04)}
.pill.ses{color:#7ee9ff;border-color:rgba(34,211,238,.35)}
.pill.on{color:#7ef7d6;border-color:rgba(0,245,176,.35)}
.hero-sig{position:relative;border-radius:26px;padding:32px 40px;margin-top:6px;min-height:210px;
  display:flex;flex-direction:column;justify-content:center;overflow:hidden;
  background:linear-gradient(155deg,rgba(255,255,255,.07),rgba(255,255,255,.015));
  border:1px solid rgba(255,255,255,.10);backdrop-filter:blur(16px);transition:box-shadow .3s,border-color .3s}
.hero-sig .tag{position:absolute;top:20px;right:24px;font-size:.62rem;font-weight:800;letter-spacing:2px;
  padding:5px 12px;border-radius:999px;background:rgba(255,255,255,.06);color:#b7c4e6;border:1px solid rgba(255,255,255,.12)}
.hero-sig .pair{font-size:1.35rem;font-weight:700;color:#e6edff;letter-spacing:2px}
.hero-sig .dir{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:4.6rem;line-height:1;margin:.4rem 0 .3rem;display:flex;align-items:center;gap:18px}
.hero-sig .arw{font-size:3.1rem}
.hero-sig .sub{font-size:.82rem;letter-spacing:3px;color:#8ea2c8;font-weight:700}
.buy .dir{color:#00f5b0;text-shadow:0 0 40px rgba(0,245,176,.5)}
.sell .dir{color:#ff3b6b;text-shadow:0 0 40px rgba(255,59,107,.45)}
.wait .dir{color:#9fb0d4;font-size:2.7rem;text-shadow:none}
.hero-sig.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.32),0 26px 70px rgba(0,245,176,.09)}
.hero-sig.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.30),0 26px 70px rgba(255,59,107,.09)}
.hero-sig.wait{box-shadow:inset 0 0 0 1px rgba(255,255,255,.07)}
.hero-sig .glow{position:absolute;right:-70px;top:-70px;width:300px;height:300px;border-radius:50%;filter:blur(80px);opacity:.45}
.buy .glow{background:#00f5b0}.sell .glow{background:#ff008c}.wait .glow{background:#3b4a70;opacity:.22}
.fbars{display:flex;gap:8px;margin-top:18px;align-items:center}
.fbars .b{width:52px;height:11px;border-radius:6px;background:rgba(255,255,255,.10)}
.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee);box-shadow:0 0 18px rgba(0,245,176,.5)}
.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c);box-shadow:0 0 18px rgba(255,59,107,.45)}
.flabel{margin-left:14px;font-size:.78rem;letter-spacing:3px;font-weight:800;color:#b3bede}
.gtitle{margin:24px 0 12px;font-size:1.05rem;font-weight:800;color:#dbe6ff;letter-spacing:.5px}
.gtitle small{color:#8496bd;font-weight:600;font-size:.8rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:15px}
.card{position:relative;border-radius:18px;padding:17px 18px;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.09);backdrop-filter:blur(10px);transition:transform .18s,border-color .18s,box-shadow .18s}
.card:hover{transform:translateY(-5px);border-color:rgba(255,255,255,.24);box-shadow:0 16px 40px rgba(0,0,0,.35)}
.card .p{font-size:1.02rem;font-weight:700;color:#e6edff;letter-spacing:.5px}
.card .d{font-family:'JetBrains Mono',monospace;font-size:1.6rem;font-weight:800;margin:.3rem 0 .35rem;display:flex;align-items:center;gap:8px}
.card.buy .d{color:#00f5b0;text-shadow:0 0 16px rgba(0,245,176,.5)}
.card.sell .d{color:#ff3b6b;text-shadow:0 0 16px rgba(255,59,107,.45)}
.card.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.20)}
.card.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.18)}
.cb{display:flex;gap:5px;margin-top:2px;align-items:center}
.cb .b{width:24px;height:7px;border-radius:4px;background:rgba(255,255,255,.10)}
.card.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee)}
.card.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c)}
.cb .lab{margin-left:7px;font-size:.64rem;letter-spacing:1.5px;font-weight:800;color:#9aa8cc}
.rule{margin-top:20px;font-size:.74rem;color:#8697bd;background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:10px 15px;line-height:1.5}
.rule b{color:#b9c6e8}
.footline{margin-top:16px;text-align:center;font-size:.7rem;color:#5c6a8e}
section[data-testid="stSidebar"]{background:rgba(9,13,26,.72);backdrop-filter:blur(8px)}
div[role="radiogroup"]{gap:8px}
div[role="radiogroup"] label{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.10);
  padding:8px 18px;border-radius:12px;font-weight:700;transition:background .15s}
div[role="radiogroup"] label:hover{background:rgba(255,255,255,.09)}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("#### ⚡ Sinais IA")
    show_closed = st.toggle("Incluir pares fora de sessão", value=False)
    st.caption("Scanner de alta convicção: 3 timeframes + força + movimento real.")

st.markdown('<div class="brand"><span class="logo">⚡</span>'
            '<span class="name">Sinais IA</span>'
            '<span class="live">SCANNER</span>'
            '<span class="scan">· ALTA CONVICÇÃO</span></div>', unsafe_allow_html=True)

tf_label = st.radio("Timeframe", ["1 min", "5 min", "15 min"], index=1, horizontal=True,
                    label_visibility="collapsed")
TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]

st_autorefresh(interval=15000, key="auto")
now = datetime.now(timezone.utc)
open_assets = [a for a in ASSETS if pair_open(a, now)]
scan_list = ASSETS if show_closed else open_assets

# ---- pipeline dos gates ----
names = CONFLUENCE[TF]
layers = [conv_at(n) for n in names]        # convicção nos 3 timeframes

candidates = []
for a in scan_list:
    vals = [layers[i].get(a["tv"]) for i in range(3)]
    if any(v is None or v == 0 for v in vals):          # gate 1: todos não-neutros
        continue
    if not (all(v > 0 for v in vals) or all(v < 0 for v in vals)):   # gate 1: mesmo lado
        continue
    if abs(vals[0]) < 2 or not any(abs(v) >= 2 for v in vals[1:]):   # gate 2: STRONG base + 1 maior
        continue
    candidates.append({"a": a, "dir": "COMPRA" if vals[0] > 0 else "VENDA",
                       "rank": sum(abs(v) for v in vals), "vals": vals})

survivors = momentum_pass(candidates, TF_YF[TF], int(TF)) if candidates else []  # gate 3

# gate 4: dedupe do dólar (mais forte por lado)
survivors.sort(key=lambda o: o["rank"], reverse=True)
seen, opps = set(), []
for o in survivors:
    side = usd_side(o["a"], o["dir"])
    if side is not None:
        if side in seen:
            continue
        seen.add(side)
    opps.append(o)

# sessões
if market_open(now):
    ses = "".join(f'<span class="pill ses">🟢 {s}</span>' for s in active_sessions(now))
    op = "".join(f'<span class="pill on">{a["name"]}</span>' for a in open_assets if a["type"] == "fx")
    st.markdown(f'<div class="sess"><b>Sessões:</b> {ses} <b style="margin-left:6px">Varredura:</b> {op or "—"} '
                f'<span class="pill on">+cripto</span></div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="sess">🔴 <b>Forex fechado</b> — varrendo apenas cripto (24/7).</div>',
                unsafe_allow_html=True)

# contador de vela
components.html(f"""
<div style="font-family:'JetBrains Mono',monospace;color:#eaf0ff;background:rgba(255,255,255,.05);
 border:1px solid rgba(255,255,255,.10);border-radius:16px;padding:14px 22px;display:flex;
 align-items:center;gap:22px;backdrop-filter:blur(10px)">
 <div style="font-size:.66rem;letter-spacing:2px;color:#93a4c8">PRÓXIMA VELA · {TF_LABEL[TF]}</div>
 <div id="clk" style="font-size:2.2rem;font-weight:800;letter-spacing:3px">--:--</div>
 <div id="ent"></div><div style="flex:1"></div>
 <div style="height:9px;flex:0 0 280px;border-radius:6px;background:rgba(255,255,255,.10);overflow:hidden">
   <div id="pb" style="height:100%;width:0%;background:linear-gradient(90deg,#00f5b0,#22d3ee)"></div></div>
</div>
<style>@import url('https://fonts.googleapis.com/css2?family=Inter:wght@800&family=JetBrains+Mono:wght@800&display=swap');
@keyframes np{{0%{{transform:scale(.96);opacity:.6}}50%{{transform:scale(1.05);opacity:1;box-shadow:0 0 28px rgba(0,245,176,.75)}}100%{{transform:scale(.96);opacity:.6}}}}</style>
<script>var TF={int(TF)};function t(){{var n=Date.now()/1000,per=TF*60,pos=n%per,left=per-pos,
m=Math.floor(left/60),s=Math.floor(left%60);document.getElementById('clk').textContent=(m<10?'0':'')+m+':'+(s<10?'0':'')+s;
document.getElementById('pb').style.width=((pos/per)*100).toFixed(1)+'%';
document.getElementById('ent').innerHTML=pos<12?'<span style="font-family:Inter,sans-serif;font-weight:800;letter-spacing:1px;padding:7px 16px;border-radius:999px;color:#04120d;background:linear-gradient(90deg,#00f5b0,#22d3ee);animation:np 1s infinite">● NOVA ENTRADA</span>':'';}}
t();setInterval(t,1000);</script>
""", height=72)

FL = {5: "MÉDIA", 6: "FORTE"}


def _bars(rank):
    fill = 3 if rank >= 6 else 2
    return "".join(f'<span class="b {"on" if i < fill else ""}"></span>' for i in range(3))


def hero_entry(o):
    a, d = o["a"], o["dir"]
    cls = "buy" if d == "COMPRA" else "sell"
    arw = "▲" if d == "COMPRA" else "▼"
    return (f'<div class="hero-sig {cls}"><div class="glow"></div><div class="tag">MELHOR ENTRADA</div>'
            f'<div class="pair">{a["name"]}</div>'
            f'<div class="dir"><span class="arw">{arw}</span> {d}</div>'
            f'<div class="fbars">{_bars(o["rank"])}<span class="flabel">FORÇA {FL.get(o["rank"], "FORTE")}</span></div></div>')


def hero_empty():
    return ('<div class="hero-sig wait"><div class="glow"></div><div class="tag">SCANNER</div>'
            '<div class="pair">MERCADO</div>'
            '<div class="dir"><span class="arw">◵</span> NENHUMA ENTRADA</div>'
            '<div class="sub">AGUARDANDO CONFLUÊNCIA — PRÓXIMA VELA</div></div>')


def card_entry(o):
    a, d = o["a"], o["dir"]
    cls = "buy" if d == "COMPRA" else "sell"
    arw = "▲" if d == "COMPRA" else "▼"
    return (f'<div class="card {cls}"><div class="p">{a["name"]}</div>'
            f'<div class="d">{arw} {d}</div>'
            f'<div class="cb">{_bars(o["rank"])}<span class="lab">{FL.get(o["rank"], "FORTE")}</span></div></div>')


if opps:
    st.markdown(hero_entry(opps[0]), unsafe_allow_html=True)
    rest = opps[1:]
    st.markdown(f'<div class="gtitle">Outras entradas · {TF_LABEL[TF]} '
                f'<small>({len(opps)} no total)</small></div>', unsafe_allow_html=True)
    if rest:
        st.markdown(f'<div class="grid">{"".join(card_entry(o) for o in rest)}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="sess">Só há esta entrada de alta convicção no momento.</div>', unsafe_allow_html=True)
else:
    st.markdown(hero_empty(), unsafe_allow_html=True)
    st.markdown('<div class="gtitle">Nenhuma entrada de alta convicção agora '
                '<small>— o normal é ter poucas por vez</small></div>', unsafe_allow_html=True)

conf = " + ".join(INTV_SHORT[n] for n in names)
st.markdown(f'<div class="rule"><b>Como funciona:</b> só vira entrada quem tem confluência '
            f'<b>{conf}</b> no mesmo lado, com rating forte e movimento real (sem mercado parado). '
            f'<b>Acerto:</b> COMPRA vence se a vela do timeframe fechar <b style="color:#00f5b0">verde</b> '
            f'(fecha acima da abertura); VENDA vence se fechar <b style="color:#ff3b6b">vermelha</b> '
            f'(fecha abaixo). Entre no <b>início da vela</b>.</div>', unsafe_allow_html=True)

st.markdown('<div class="footline">Uso próprio · não é recomendação financeira</div>', unsafe_allow_html=True)
