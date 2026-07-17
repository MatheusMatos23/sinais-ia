"""
app.py — Sinais IA · SCANNER DE OPORTUNIDADES (uso próprio).

Não escolhe ativo: varre TODOS os ativos de mercado aberto e mostra SÓ os que
têm ENTRADA agora, ordenados do mais forte para o mais fraco.

Critério de ENTRADA (confluência real, para reduzir sinais):
  - direção não-neutra no timeframe escolhido E no timeframe imediatamente maior;
  - os dois apontando o MESMO lado.
Quem não cumpre não aparece. Se ninguém cumpre -> "NENHUMA ENTRADA".

Força = soma das convicções dos dois timeframes:
  2 (ambos leves) = FRACA · 3 (um forte) = MÉDIA · 4 (ambos fortes) = FORTE.

Fonte: rating de Análise Técnica do TradingView (`tradingview_ta`), com timeout
global, retries curtos, cache por vela e fallback yfinance (indicadores no
servidor). Buscas em paralelo.
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
BY_TV = {a["tv"]: a for a in ASSETS}
SESSIONS = {"Sydney": (21, 6), "Tóquio": (23, 8), "Londres": (7, 16), "Nova York": (12, 21)}
CUR_SESS = {"AUD": "Sydney", "NZD": "Sydney", "JPY": "Tóquio", "EUR": "Londres",
            "GBP": "Londres", "CHF": "Londres", "USD": "Nova York", "CAD": "Nova York"}
INTERVAL_NAME = {"1": "INTERVAL_1_MINUTE", "5": "INTERVAL_5_MINUTES", "15": "INTERVAL_15_MINUTES"}
HIGHER = {"1": "5", "5": "15", "15": "15"}
TF_YF = {"1": "1m", "5": "5m", "15": "15m"}
TF_LABEL = {"1": "1 min", "5": "5 min", "15": "15 min"}
REC_CONV = {"STRONG_BUY": 2, "BUY": 1, "NEUTRAL": 0, "SELL": -1, "STRONG_SELL": -2}


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


# ---------------------- TradingView (paralelo + last-good por vela) ----------------------
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


# ---------------------- Fallback yfinance ----------------------
def _yf_conv(yf_symbol, yf_interval):
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
        sc = max(-1.0, min(1.0, sum(comps) / len(comps)))
        if sc >= 0.6:
            return 2
        if sc >= 0.3:
            return 1
        if sc <= -0.6:
            return -2
        if sc <= -0.3:
            return -1
        return 0
    except Exception:
        return None


def fill_yf(missing_assets, yf_interval, minutes):
    cache = st.session_state.setdefault("yf_cache", {})
    ck = candle_key(minutes)
    todo = [a for a in missing_assets if (a["yf"], yf_interval, ck) not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            futs = {ex.submit(_yf_conv, a["yf"], yf_interval): a for a in todo}
            for f in as_completed(futs):
                cache[(futs[f]["yf"], yf_interval, ck)] = f.result()
    return {a["tv"]: cache.get((a["yf"], yf_interval, ck)) for a in missing_assets}


def conv_map(tf_digit, cover):
    """Convicção {-2..2} por ativo no timeframe tf_digit (TradingView -> yfinance)."""
    interval_name, minutes = INTERVAL_NAME[tf_digit], int(tf_digit)
    tv = tv_all(interval_name, candle_key(minutes))
    out, missing = {}, []
    for a in cover:
        s = tv.get(a["tv"])
        if s is None:
            missing.append(a)
        else:
            out[a["tv"]] = REC_CONV.get(s.get("RECOMMENDATION", "NEUTRAL"), 0)
    if missing:
        yf = fill_yf(missing, TF_YF[tf_digit], minutes)
        for a in missing:
            out[a["tv"]] = yf.get(a["tv"])
    return out


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
.brand .scan{font-size:.62rem;font-weight:700;letter-spacing:2px;color:#9db0d6;margin-left:2px}
.sess{margin:14px 0 4px;font-size:.82rem;color:#9db0d6;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.pill{padding:4px 11px;border-radius:999px;font-size:.73rem;font-weight:700;border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.04)}
.pill.ses{color:#7ee9ff;border-color:rgba(34,211,238,.35)}
.pill.on{color:#7ef7d6;border-color:rgba(0,245,176,.35)}
/* HERO (melhor entrada) */
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
.wait .dir{color:#9fb0d4;font-size:2.9rem;text-shadow:none}
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
/* grade */
.gtitle{margin:26px 0 12px;font-size:1.05rem;font-weight:800;color:#dbe6ff;letter-spacing:.5px}
.gtitle small{color:#8496bd;font-weight:600;font-size:.8rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:15px}
.card{position:relative;border-radius:18px;padding:17px 18px;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.09);backdrop-filter:blur(10px);
  transition:transform .18s,border-color .18s,box-shadow .18s}
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
.footline{margin-top:30px;text-align:center;font-size:.72rem;color:#5c6a8e}
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
    st.caption("Scanner: só entradas com confluência de timeframes.")

st.markdown('<div class="brand"><span class="logo">⚡</span>'
            '<span class="name">Sinais IA</span>'
            '<span class="live">SCANNER</span>'
            '<span class="scan">· CONFLUÊNCIA MULTI-TIMEFRAME</span></div>', unsafe_allow_html=True)

tf_label = st.radio("Timeframe", ["1 min", "5 min", "15 min"], index=1, horizontal=True,
                    label_visibility="collapsed")
TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]

st_autorefresh(interval=15000, key="auto")
now = datetime.now(timezone.utc)

open_assets = [a for a in ASSETS if pair_open(a, now)]
cover = ASSETS if show_closed else open_assets
scan_list = ASSETS if show_closed else open_assets

# convicção no TF escolhido e no maior
cur = conv_map(TF, cover)
hi = conv_map(HIGHER[TF], cover)

# monta oportunidades (confluência real)
opps = []
for a in scan_list:
    c, h = cur.get(a["tv"]), hi.get(a["tv"])
    if not c or not h:            # None ou 0 -> sem direção -> fora
        continue
    if (c > 0) != (h > 0):        # timeframes discordam -> fora
        continue
    strength = abs(c) + abs(h)    # 2..4
    opps.append({
        "a": a, "dir": "COMPRA" if c > 0 else "VENDA",
        "force": {2: "FRACO", 3: "MEDIO", 4: "FORTE"}[strength],
        "rank": strength, "conv": abs(c) + abs(h),
    })
opps.sort(key=lambda o: (o["rank"], o["conv"]), reverse=True)

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


def _bars(force):
    fill = {"FRACO": 1, "MEDIO": 2, "FORTE": 3}.get(force, 0)
    return "".join(f'<span class="b {"on" if i < fill else ""}"></span>' for i in range(3))


FL = {"FRACO": "FRACA", "MEDIO": "MÉDIA", "FORTE": "FORTE"}


def hero_entry(o):
    a, d, f = o["a"], o["dir"], o["force"]
    cls = "buy" if d == "COMPRA" else "sell"
    arw = "▲" if d == "COMPRA" else "▼"
    return (f'<div class="hero-sig {cls}"><div class="glow"></div>'
            f'<div class="tag">MELHOR ENTRADA</div>'
            f'<div class="pair">{a["name"]}</div>'
            f'<div class="dir"><span class="arw">{arw}</span> {d}</div>'
            f'<div class="fbars">{_bars(f)}<span class="flabel">FORÇA {FL[f]}</span></div></div>')


def hero_empty():
    return ('<div class="hero-sig wait"><div class="glow"></div>'
            '<div class="tag">SCANNER</div>'
            '<div class="pair">MERCADO</div>'
            '<div class="dir"><span class="arw">◵</span> NENHUMA ENTRADA</div>'
            '<div class="sub">AGUARDANDO CONFLUÊNCIA — PRÓXIMA VELA</div></div>')


def card_entry(o):
    a, d, f = o["a"], o["dir"], o["force"]
    cls = "buy" if d == "COMPRA" else "sell"
    arw = "▲" if d == "COMPRA" else "▼"
    return (f'<div class="card {cls}"><div class="p">{a["name"]}</div>'
            f'<div class="d">{arw} {d}</div>'
            f'<div class="cb">{_bars(f)}<span class="lab">{FL[f]}</span></div></div>')


# render
if opps:
    st.markdown(hero_entry(opps[0]), unsafe_allow_html=True)
    rest = opps[1:]
    st.markdown(f'<div class="gtitle">Outras entradas · {TF_LABEL[TF]} '
                f'<small>({len(opps)} no total)</small></div>', unsafe_allow_html=True)
    if rest:
        st.markdown(f'<div class="grid">{"".join(card_entry(o) for o in rest)}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="sess">Só há esta entrada confluente no momento.</div>', unsafe_allow_html=True)
else:
    st.markdown(hero_empty(), unsafe_allow_html=True)
    st.markdown('<div class="gtitle">Nenhuma entrada confluente agora '
                '<small>— o normal é ter poucas por vez</small></div>', unsafe_allow_html=True)

st.markdown('<div class="footline">Uso próprio · não é recomendação financeira</div>', unsafe_allow_html=True)
