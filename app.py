"""
app.py — Sinais IA (uso próprio). Painel premium de sinais, rápido e robusto.

Sinal: rating de Análise Técnica do TradingView (`tradingview_ta`). Robustez:
timeout global de socket (não pendura), poucos retries, cache de "último valor
bom" por vela em session_state, buscas em PARALELO (ThreadPoolExecutor) e
FALLBACK por yfinance (EMA/RSI/MACD/Bollinger no servidor).

Regra: só mostra COMPRA/VENDA quando há ENTRADA (força FRACA/MÉDIA/FORTE); em
estado neutro mostra "AGUARDANDO". Entradas na virada da vela (contador).
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

socket.setdefaulttimeout(6)  # nenhuma requisição pendura a UI

st.set_page_config(page_title="Sinais IA", page_icon="⚡", layout="wide")

ASSETS = [
    {"name": "EUR/USD", "tv": "FX_IDC:EURUSD", "scr": "forex", "yf": "EURUSD=X", "cur": ["EUR", "USD"], "type": "fx"},
    {"name": "GBP/USD", "tv": "FX_IDC:GBPUSD", "scr": "forex", "yf": "GBPUSD=X", "cur": ["GBP", "USD"], "type": "fx"},
    {"name": "USD/JPY", "tv": "FX_IDC:USDJPY", "scr": "forex", "yf": "USDJPY=X", "cur": ["USD", "JPY"], "type": "fx"},
    {"name": "AUD/USD", "tv": "FX_IDC:AUDUSD", "scr": "forex", "yf": "AUDUSD=X", "cur": ["AUD", "USD"], "type": "fx"},
    {"name": "USD/CAD", "tv": "FX_IDC:USDCAD", "scr": "forex", "yf": "USDCAD=X", "cur": ["USD", "CAD"], "type": "fx"},
    {"name": "USD/CHF", "tv": "FX_IDC:USDCHF", "scr": "forex", "yf": "USDCHF=X", "cur": ["USD", "CHF"], "type": "fx"},
    {"name": "NZD/USD", "tv": "FX_IDC:NZDUSD", "scr": "forex", "yf": "NZDUSD=X", "cur": ["NZD", "USD"], "type": "fx"},
    {"name": "EUR/JPY", "tv": "FX_IDC:EURJPY", "scr": "forex", "yf": "EURJPY=X", "cur": ["EUR", "JPY"], "type": "fx"},
    {"name": "BTC/USD", "tv": "BINANCE:BTCUSDT", "scr": "crypto", "yf": "BTC-USD", "cur": [], "type": "crypto"},
    {"name": "ETH/USD", "tv": "BINANCE:ETHUSDT", "scr": "crypto", "yf": "ETH-USD", "cur": [], "type": "crypto"},
]
BY_TV = {a["tv"]: a for a in ASSETS}
SESSIONS = {"Sydney": (21, 6), "Tóquio": (23, 8), "Londres": (7, 16), "Nova York": (12, 21)}
CUR_SESS = {"AUD": "Sydney", "NZD": "Sydney", "JPY": "Tóquio", "EUR": "Londres",
            "GBP": "Londres", "CHF": "Londres", "USD": "Nova York", "CAD": "Nova York"}
INTERVAL_NAME = {"1": "INTERVAL_1_MINUTE", "5": "INTERVAL_5_MINUTES", "15": "INTERVAL_15_MINUTES"}
HIGHER = {"1": "5", "5": "15", "15": "15"}
TF_YF = {"1": "1m", "5": "5m", "15": "15m"}
TF_LABEL = {"1": "1 min", "5": "5 min", "15": "15 min"}


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


def candle_key(tf_min):
    return int(math.floor(datetime.now(timezone.utc).timestamp() / 60.0 / tf_min))


# ---------------------- TradingView (paralelo + last-good) ----------------------
def _tv_once(screener, interval_name, symbols):
    from tradingview_ta import get_multiple_analysis, Interval
    iv = getattr(Interval, interval_name)
    res = get_multiple_analysis(screener=screener, interval=iv, symbols=list(symbols))
    return {k: (v.summary if v else None) for k, v in res.items()}


def _fetch_screener(screener, interval_name, symbols):
    for attempt in range(2):            # poucos retries -> não trava
        try:
            return _tv_once(screener, interval_name, symbols)
        except Exception:
            time.sleep(0.3)
    return {}


def tv_all(interval_name, ck):
    """Busca 1x por vela; screeners em paralelo; mantém último valor bom."""
    store = st.session_state.setdefault("tv_store", {})
    meta = st.session_state.setdefault("tv_ck", {})
    prev = dict(store.get(interval_name, {}))
    if meta.get(interval_name) == ck and prev:
        return prev
    fx = tuple(a["tv"] for a in ASSETS if a["type"] == "fx")
    cr = tuple(a["tv"] for a in ASSETS if a["type"] == "crypto")
    merged = dict(prev)
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_fetch_screener, scr, interval_name, syms): scr
                for scr, syms in (("forex", fx), ("crypto", cr))}
        for f in as_completed(futs):
            for k, v in (f.result() or {}).items():
                if v:
                    merged[k] = v
    store[interval_name] = merged
    meta[interval_name] = ck
    return merged


def classify_summary(summary):
    if not summary:
        return None
    rec = summary.get("RECOMMENDATION", "NEUTRAL")
    buy, sell, neu = summary.get("BUY", 0), summary.get("SELL", 0), summary.get("NEUTRAL", 0)
    total = max(buy + sell + neu, 1)
    if rec == "NEUTRAL":
        return ("WAIT", None, None)
    direc = "COMPRA" if "BUY" in rec else "VENDA"
    if rec in ("STRONG_BUY", "STRONG_SELL"):
        forca = "FORTE"
    else:
        forca = "MEDIO" if abs(buy - sell) / total >= 0.22 else "FRACO"
    return ("ENTRY", direc, forca)


# ---------------------- Fallback yfinance (paralelo, sem st nas threads) --------
def _yf_compute(yf_symbol, yf_interval):
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np
        period = {"1m": "1d", "5m": "5d", "15m": "5d"}[yf_interval]
        df = yf.download(yf_symbol, interval=yf_interval, period=period,
                         progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty or len(df) < 30:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        c = df["Close"].astype(float)
        ema9 = c.ewm(span=9, adjust=False).mean()
        ema21 = c.ewm(span=21, adjust=False).mean()
        d = c.diff()
        gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi = (100 - 100/(1 + gain/loss.replace(0, np.nan))).fillna(50)
        hist = (c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean())
        hist = hist - hist.ewm(span=9, adjust=False).mean()
        mid = c.rolling(20).mean()
        comps = [1.0 if ema9.iloc[-1] > ema21.iloc[-1] else -1.0,
                 max(-1.0, min(1.0, (rsi.iloc[-1] - 50) / 20.0)),
                 1.0 if hist.iloc[-1] > 0 else -1.0]
        if not math.isnan(mid.iloc[-1]):
            comps.append(1.0 if c.iloc[-1] > mid.iloc[-1] else -1.0)
        return max(-1.0, min(1.0, sum(comps) / len(comps)))
    except Exception:
        return None


def classify_score(score):
    if score is None:
        return None
    m = abs(score)
    if m < 0.15:
        return ("WAIT", None, None)
    direc = "COMPRA" if score > 0 else "VENDA"
    forca = "FORTE" if m >= 0.6 else ("MEDIO" if m >= 0.35 else "FRACO")
    return ("ENTRY", direc, forca)


def fill_yf(missing_assets, TF):
    """Baixa os faltantes em paralelo; cacheia em session_state por vela."""
    cache = st.session_state.setdefault("yf_cache", {})
    ck = candle_key(int(TF))
    todo = [a for a in missing_assets if (a["yf"], TF_YF[TF], ck) not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            futs = {ex.submit(_yf_compute, a["yf"], TF_YF[TF]): a for a in todo}
            for f in as_completed(futs):
                a = futs[f]
                cache[(a["yf"], TF_YF[TF], ck)] = f.result()
    return {a["tv"]: classify_score(cache.get((a["yf"], TF_YF[TF], ck))) for a in missing_assets}


def apply_conf(res, higher_res):
    if not res or res[0] != "ENTRY" or not higher_res or higher_res[0] != "ENTRY":
        return res
    _, d, f = res
    _, hd, _ = higher_res
    if hd == d and f == "MEDIO":
        f = "FORTE"
    elif hd != d and f == "FORTE":
        f = "MEDIO"
    return ("ENTRY", d, f)


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
.sess{margin:14px 0 4px;font-size:.82rem;color:#9db0d6;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.pill{padding:4px 11px;border-radius:999px;font-size:.73rem;font-weight:700;border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.04)}
.pill.ses{color:#7ee9ff;border-color:rgba(34,211,238,.35)}
.pill.on{color:#7ef7d6;border-color:rgba(0,245,176,.35)}
/* HERO */
.hero-sig{position:relative;border-radius:26px;padding:34px 40px;margin-top:6px;min-height:230px;
  display:flex;flex-direction:column;justify-content:center;overflow:hidden;
  background:linear-gradient(155deg,rgba(255,255,255,.07),rgba(255,255,255,.015));
  border:1px solid rgba(255,255,255,.10);backdrop-filter:blur(16px);
  transition:box-shadow .3s ease,border-color .3s ease}
.hero-sig .pair{font-size:1.35rem;font-weight:700;color:#e6edff;letter-spacing:2px}
.hero-sig .dir{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:5rem;line-height:1;margin:.5rem 0 .3rem;display:flex;align-items:center;gap:18px}
.hero-sig .arw{font-size:3.4rem}
.hero-sig .sub{font-size:.82rem;letter-spacing:3px;color:#8ea2c8;font-weight:700}
.buy .dir{color:#00f5b0;text-shadow:0 0 40px rgba(0,245,176,.5)}
.sell .dir{color:#ff3b6b;text-shadow:0 0 40px rgba(255,59,107,.45)}
.wait .dir{color:#9fb0d4;font-size:3.4rem;text-shadow:none}
.hero-sig.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.32),0 26px 70px rgba(0,245,176,.09)}
.hero-sig.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.30),0 26px 70px rgba(255,59,107,.09)}
.hero-sig.wait{box-shadow:inset 0 0 0 1px rgba(255,255,255,.07)}
.hero-sig .glow{position:absolute;right:-70px;top:-70px;width:300px;height:300px;border-radius:50%;filter:blur(80px);opacity:.45}
.buy .glow{background:#00f5b0}.sell .glow{background:#ff008c}.wait .glow{background:#3b4a70;opacity:.25}
.fbars{display:flex;gap:8px;margin-top:20px;align-items:center}
.fbars .b{width:52px;height:11px;border-radius:6px;background:rgba(255,255,255,.10)}
.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee);box-shadow:0 0 18px rgba(0,245,176,.5)}
.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c);box-shadow:0 0 18px rgba(255,59,107,.45)}
.flabel{margin-left:14px;font-size:.78rem;letter-spacing:3px;font-weight:800;color:#b3bede}
/* grid */
.gtitle{margin:26px 0 12px;font-size:1.05rem;font-weight:800;color:#dbe6ff;letter-spacing:.5px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:15px}
.card{position:relative;border-radius:18px;padding:17px 18px;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.09);backdrop-filter:blur(10px);
  transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.card:hover{transform:translateY(-5px);border-color:rgba(255,255,255,.24);box-shadow:0 16px 40px rgba(0,0,0,.35)}
.card .p{font-size:1.02rem;font-weight:700;color:#e6edff;letter-spacing:.5px}
.card .d{font-family:'JetBrains Mono',monospace;font-size:1.6rem;font-weight:800;margin:.3rem 0 .35rem;display:flex;align-items:center;gap:8px}
.card.buy .d{color:#00f5b0;text-shadow:0 0 16px rgba(0,245,176,.5)}
.card.sell .d{color:#ff3b6b;text-shadow:0 0 16px rgba(255,59,107,.45)}
.card.wait .d{color:#93a4c8;font-size:1.1rem;text-shadow:none}
.card.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.20)}
.card.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.18)}
.cb{display:flex;gap:5px;margin-top:2px;align-items:center}
.cb .b{width:24px;height:7px;border-radius:4px;background:rgba(255,255,255,.10)}
.card.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee)}
.card.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c)}
.cb .lab{margin-left:7px;font-size:.64rem;letter-spacing:1.5px;font-weight:800;color:#9aa8cc}
.footline{margin-top:30px;text-align:center;font-size:.72rem;color:#5c6a8e}
section[data-testid="stSidebar"]{background:rgba(9,13,26,.72);backdrop-filter:blur(8px)}
div[role="radiogroup"]{gap:8px}
div[role="radiogroup"] label{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.10);
  padding:8px 18px;border-radius:12px;font-weight:700;transition:background .15s}
div[role="radiogroup"] label:hover{background:rgba(255,255,255,.09)}
.stSelectbox div[data-baseweb="select"]>div{background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.12)}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("#### ⚡ Sinais IA")
    show_closed = st.toggle("Mostrar pares fora de sessão", value=False)
    st.caption("O timeframe define o tamanho da vela.")

st.markdown('<div class="brand"><span class="logo">⚡</span>'
            '<span class="name">Sinais IA</span>'
            '<span class="live">TEMPO REAL</span></div>', unsafe_allow_html=True)

c1, c2 = st.columns([1.1, 1.6])
with c1:
    tf_label = st.radio("Timeframe", ["1 min", "5 min", "15 min"], index=1, horizontal=True,
                        label_visibility="collapsed")
    TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]
with c2:
    sel_name = st.selectbox("Ativo", [a["name"] for a in ASSETS], index=0, label_visibility="collapsed")

st_autorefresh(interval=15000, key="auto")
now = datetime.now(timezone.utc)

# dados (cache por vela): timeframe atual + maior (confluência)
cur = tv_all(INTERVAL_NAME[TF], candle_key(int(TF)))
hi = tv_all(INTERVAL_NAME[HIGHER[TF]], candle_key(int(HIGHER[TF])))

open_assets = [a for a in ASSETS if pair_open(a, now)]
show_list = open_assets if not show_closed else ASSETS
sel = next(a for a in ASSETS if a["name"] == sel_name)

# resolve sinais: TradingView -> (fallback yfinance só para faltantes, em paralelo)
resolved = {}
needed = {sel["tv"]: sel}
for a in show_list:
    needed[a["tv"]] = a
missing = []
for tv, a in needed.items():
    r = classify_summary(cur.get(tv))
    if r is None:
        missing.append(a)
    else:
        resolved[tv] = r
if missing:
    yf_res = fill_yf(missing, TF)
    for tv, r in yf_res.items():
        resolved[tv] = r


def sig_of(a):
    r = resolved.get(a["tv"])
    if r and r[0] == "ENTRY":
        r = apply_conf(r, classify_summary(hi.get(a["tv"])))
    return r


# sessões
if market_open(now):
    ses = "".join(f'<span class="pill ses">🟢 {s}</span>' for s in active_sessions(now))
    op = "".join(f'<span class="pill on">{a["name"]}</span>' for a in open_assets if a["type"] == "fx")
    st.markdown(f'<div class="sess"><b>Sessões:</b> {ses} <b style="margin-left:6px">Abertos:</b> {op or "—"}</div>',
                unsafe_allow_html=True)
else:
    st.markdown('<div class="sess">🔴 <b>Forex fechado</b> — apenas cripto (24/7).</div>', unsafe_allow_html=True)

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


def _bars(forca, w):
    fill = {"FRACO": 1, "MEDIO": 2, "FORTE": 3}.get(forca, 0)
    return "".join(f'<span class="b {"on" if i < fill else ""}"></span>' for i in range(3))


def hero_html(a, r):
    if not r or r[0] == "WAIT":
        return (f'<div class="hero-sig wait"><div class="glow"></div>'
                f'<div class="pair">{a["name"]}</div>'
                f'<div class="dir"><span class="arw">◵</span> AGUARDANDO</div>'
                f'<div class="sub">SEM ENTRADA NO MOMENTO</div></div>')
    _, d, f = r
    cls = "buy" if d == "COMPRA" else "sell"
    arrow = "▲" if d == "COMPRA" else "▼"
    fl = {"FRACO": "FRACA", "MEDIO": "MÉDIA", "FORTE": "FORTE"}[f]
    return (f'<div class="hero-sig {cls}"><div class="glow"></div>'
            f'<div class="pair">{a["name"]}</div>'
            f'<div class="dir"><span class="arw">{arrow}</span> {d}</div>'
            f'<div class="fbars">{_bars(f, 1)}<span class="flabel">FORÇA {fl}</span></div></div>')


def card_html(a, r):
    if not r or r[0] == "WAIT":
        return (f'<div class="card wait"><div class="p">{a["name"]}</div>'
                f'<div class="d">◵ aguardando</div>'
                f'<div class="cb"><span class="lab">SEM ENTRADA</span></div></div>')
    _, d, f = r
    cls = "buy" if d == "COMPRA" else "sell"
    arrow = "▲" if d == "COMPRA" else "▼"
    fl = {"FRACO": "FRACA", "MEDIO": "MÉDIA", "FORTE": "FORTE"}[f]
    return (f'<div class="card {cls}"><div class="p">{a["name"]}</div>'
            f'<div class="d">{arrow} {d}</div>'
            f'<div class="cb">{_bars(f, 1)}<span class="lab">{fl}</span></div></div>')


st.markdown(hero_html(sel, sig_of(sel)), unsafe_allow_html=True)

st.markdown(f'<div class="gtitle">Ativos · {TF_LABEL[TF]}</div>', unsafe_allow_html=True)
cards = "".join(card_html(a, sig_of(a)) for a in show_list)
st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)

st.markdown('<div class="footline">Uso próprio · não é recomendação financeira</div>', unsafe_allow_html=True)
