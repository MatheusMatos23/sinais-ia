"""
app.py — Sinais IA (uso próprio). Painel premium de sinais.

Sinal: rating de Análise Técnica do TradingView via `tradingview_ta` (servidor).
Robustez: retry/backoff + cache de "último valor bom" em session_state + FALLBACK
por yfinance (EMA/RSI/MACD/Bollinger calculados no servidor). O app não cai em
"indisponível": quando o mercado está aberto, sempre há sinal real.

Regra de exibição: só mostra COMPRA/VENDA quando há ENTRADA (força FRACO/MÉDIO/
FORTE). Em estado neutro mostra "AGUARDANDO ENTRADA" (cinza, sem direção).
Entradas indicadas na virada da vela (contador regressivo + destaque).
"""
from __future__ import annotations
import math
import time
from datetime import datetime, timezone

import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Sinais IA", page_icon="⚡", layout="wide")

# --------------------------------------------------------------------------
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
    if not market_open(d):
        return []
    return [k for k, (s, e) in SESSIONS.items() if in_win(d.hour, s, e)]


def pair_open(a, d):
    if a["type"] == "crypto":
        return True
    return any(CUR_SESS.get(c) in set(active_sessions(d)) for c in a["cur"])


def candle_key(tf_min):
    return int(math.floor(datetime.now(timezone.utc).timestamp() / 60.0 / tf_min))


# --------------------------------------------------------------------------
# Fonte 1: TradingView (tradingview_ta) — robusto, com retry e last-good
# --------------------------------------------------------------------------
def _tv_batch_once(screener, interval_name, symbols):
    from tradingview_ta import get_multiple_analysis, Interval
    iv = getattr(Interval, interval_name)
    res = get_multiple_analysis(screener=screener, interval=iv, symbols=list(symbols))
    return {k: (v.summary if v else None) for k, v in res.items()}


def tv_all(interval_name, ck):
    """Busca (1x por vela) todos os símbolos; mantém 'último valor bom' na sessão."""
    store = st.session_state.setdefault("tv_store", {})
    meta = st.session_state.setdefault("tv_ck", {})
    prev = dict(store.get(interval_name, {}))
    if meta.get(interval_name) == ck and prev:
        return prev  # já buscado nesta vela
    fx = tuple(a["tv"] for a in ASSETS if a["type"] == "fx")
    cr = tuple(a["tv"] for a in ASSETS if a["type"] == "crypto")
    merged = dict(prev)
    for scr, syms in (("forex", fx), ("crypto", cr)):
        for attempt in range(3):
            try:
                good = _tv_batch_once(scr, interval_name, syms)
                for k, v in good.items():
                    if v:
                        merged[k] = v  # só sobrescreve com valor válido
                break
            except Exception:
                time.sleep(0.5 * (attempt + 1))
    store[interval_name] = merged
    meta[interval_name] = ck
    return merged


def classify_summary(summary):
    """summary do TradingView -> (estado, direção, força)."""
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
        skew = abs(buy - sell) / total
        forca = "MEDIO" if skew >= 0.22 else "FRACO"
    return ("ENTRY", direc, forca)


# --------------------------------------------------------------------------
# Fonte 2 (fallback): indicadores via yfinance no servidor
# --------------------------------------------------------------------------
@st.cache_data(ttl=50, show_spinner=False)
def yf_score(yf_symbol, yf_interval, ck):
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np
        period = {"1m": "1d", "5m": "5d", "15m": "5d"}[yf_interval]
        df = yf.download(yf_symbol, interval=yf_interval, period=period,
                         progress=False, auto_adjust=False)
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
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100/(1+rs)).fillna(50)
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        sig = macd.ewm(span=9, adjust=False).mean()
        hist = macd - sig
        mid = c.rolling(20).mean()
        i = -1
        comps = []
        comps.append(1.0 if ema9.iloc[i] > ema21.iloc[i] else -1.0)
        comps.append(max(-1.0, min(1.0, (rsi.iloc[i] - 50) / 20.0)))
        comps.append(1.0 if hist.iloc[i] > 0 else -1.0)
        if not math.isnan(mid.iloc[i]):
            comps.append(1.0 if c.iloc[i] > mid.iloc[i] else -1.0)
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


def signal_for(a, cur, hi, TF):
    r = classify_summary(cur.get(a["tv"]))
    if r is None:  # fallback yfinance
        r = classify_score(yf_score(a["yf"], TF_YF[TF], candle_key(int(TF))))
    if r and r[0] == "ENTRY":
        hr = classify_summary(hi.get(a["tv"]))
        r = apply_conf(r, hr)
    return r  # ("ENTRY",dir,forca) | ("WAIT",None,None) | None


# ==========================================================================
# CSS premium
# ==========================================================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700;800&display=swap');
:root{--buy:#00f5b0;--buy2:#22d3ee;--sell:#ff3b6b;--sell2:#ff008c;--wait:#8ea0c0;}
.stApp{background:
  radial-gradient(1100px 520px at 12% -8%, #172a5a 0%, rgba(7,10,22,0) 55%),
  radial-gradient(900px 460px at 100% -6%, #0b3a48 0%, rgba(7,10,22,0) 52%),
  linear-gradient(#070a16,#05070f);
  color:#eaf0ff;font-family:'Inter',sans-serif;}
.stApp:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.35;
  background-image:linear-gradient(rgba(120,150,255,.045) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(120,150,255,.045) 1px,transparent 1px);
  background-size:44px 44px;}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:1.1rem;max-width:1240px;position:relative;z-index:1}
/* brand row */
.brand{display:flex;align-items:center;gap:12px;margin-bottom:2px}
.brand .logo{font-size:1.5rem;filter:drop-shadow(0 0 10px rgba(0,245,176,.6))}
.brand .name{font-size:1.5rem;font-weight:900;letter-spacing:.4px;
  background:linear-gradient(90deg,#8ff8db,#4fd2ff 55%,#c8a0ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.brand .live{font-size:.62rem;font-weight:800;letter-spacing:2px;color:#062018;
  background:linear-gradient(90deg,#00f5b0,#22d3ee);padding:4px 10px;border-radius:999px}
/* sessions */
.sess{margin:12px 0 2px;font-size:.82rem;color:#9db0d6;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.pill{padding:4px 11px;border-radius:999px;font-size:.74rem;font-weight:700;border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.04)}
.pill.ses{color:#7ee9ff;border-color:rgba(34,211,238,.35)}
.pill.on{color:#7ef7d6;border-color:rgba(0,245,176,.35)}
/* HERO signal card */
.hero-sig{position:relative;border-radius:22px;padding:26px 28px;min-height:210px;
  background:linear-gradient(160deg,rgba(255,255,255,.06),rgba(255,255,255,.02));
  border:1px solid rgba(255,255,255,.10);backdrop-filter:blur(14px);overflow:hidden}
.hero-sig .pair{font-size:1.15rem;font-weight:700;color:#dfe8ff;letter-spacing:1px}
.hero-sig .dir{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:3.4rem;line-height:1.05;margin:.4rem 0 .2rem}
.hero-sig .sub{font-size:.8rem;letter-spacing:2px;color:#93a4c8;font-weight:700}
.buy .dir{color:#00f5b0;text-shadow:0 0 26px rgba(0,245,176,.55)}
.sell .dir{color:#ff3b6b;text-shadow:0 0 26px rgba(255,59,107,.5)}
.wait .dir{color:#9fb0d4;font-size:2.4rem;text-shadow:none}
.hero-sig.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.30),0 20px 60px rgba(0,245,176,.08)}
.hero-sig.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.28),0 20px 60px rgba(255,59,107,.08)}
.hero-sig.wait{box-shadow:inset 0 0 0 1px rgba(255,255,255,.06)}
.hero-sig .glow{position:absolute;right:-60px;top:-60px;width:220px;height:220px;border-radius:50%;filter:blur(60px);opacity:.5}
.buy .glow{background:#00f5b0}.sell .glow{background:#ff008c}.wait .glow{background:#3b4a70;opacity:.3}
/* force bars */
.fbars{display:flex;gap:6px;margin-top:14px;align-items:center}
.fbars .b{width:34px;height:9px;border-radius:5px;background:rgba(255,255,255,.10)}
.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee);box-shadow:0 0 14px rgba(0,245,176,.5)}
.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c);box-shadow:0 0 14px rgba(255,59,107,.45)}
.flabel{margin-left:10px;font-size:.72rem;letter-spacing:2px;font-weight:800;color:#aeb9d8}
/* grid cards */
.gtitle{margin:22px 0 10px;font-size:1.02rem;font-weight:800;color:#dbe6ff;letter-spacing:.4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(212px,1fr));gap:14px}
.card{position:relative;border-radius:16px;padding:15px 17px;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.09);backdrop-filter:blur(10px);transition:transform .16s,border-color .16s}
.card:hover{transform:translateY(-4px);border-color:rgba(255,255,255,.2)}
.card .p{font-size:1rem;font-weight:700;color:#e3ebff;letter-spacing:.4px}
.card .d{font-family:'JetBrains Mono',monospace;font-size:1.5rem;font-weight:800;margin:.28rem 0 .3rem}
.card.buy .d{color:#00f5b0;text-shadow:0 0 16px rgba(0,245,176,.5)}
.card.sell .d{color:#ff3b6b;text-shadow:0 0 16px rgba(255,59,107,.45)}
.card.wait .d{color:#93a4c8;font-size:1.05rem;text-shadow:none}
.card.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.22)}
.card.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.20)}
.cb{display:flex;gap:4px;margin-top:2px;align-items:center}
.cb .b{width:20px;height:6px;border-radius:3px;background:rgba(255,255,255,.10)}
.card.buy .b.on{background:linear-gradient(90deg,#00f5b0,#22d3ee)}
.card.sell .b.on{background:linear-gradient(90deg,#ff3b6b,#ff008c)}
.cb .lab{margin-left:6px;font-size:.62rem;letter-spacing:1.5px;font-weight:800;color:#9aa8cc}
.footline{margin-top:26px;text-align:center;font-size:.72rem;color:#5f6d90}
/* streamlit widgets */
section[data-testid="stSidebar"]{background:rgba(10,14,28,.7);backdrop-filter:blur(8px)}
div[role="radiogroup"]{gap:8px}
div[role="radiogroup"] label{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.10);
  padding:7px 16px;border-radius:11px;font-weight:700}
.stSelectbox div[data-baseweb="select"]>div{background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.12)}
</style>
""", unsafe_allow_html=True)

# --------------------------------------------------------------------------
with st.sidebar:
    st.markdown("#### ⚡ Sinais IA")
    show_closed = st.toggle("Mostrar pares fora de sessão", value=False)
    st.caption("Timeframe define o tamanho da vela.")

st.markdown('<div class="brand"><span class="logo">⚡</span>'
            '<span class="name">Sinais IA</span>'
            '<span class="live">TEMPO REAL</span></div>', unsafe_allow_html=True)

c1, c2, c3 = st.columns([1.2, 1.5, 0.9])
with c1:
    tf_label = st.radio("Timeframe", ["1 min", "5 min", "15 min"], index=1, horizontal=True,
                        label_visibility="collapsed")
    TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]
with c2:
    sel_name = st.selectbox("Ativo", [a["name"] for a in ASSETS], index=0,
                            label_visibility="collapsed")
with c3:
    st.write("")

st_autorefresh(interval=10000, key="auto")
now = datetime.now(timezone.utc)

cur = tv_all(INTERVAL_NAME[TF], candle_key(int(TF)))
hi = tv_all(INTERVAL_NAME[HIGHER[TF]], candle_key(int(HIGHER[TF])))

open_assets = [a for a in ASSETS if pair_open(a, now)]
open_fx = [a for a in open_assets if a["type"] == "fx"]

# sessões
if market_open(now):
    ses = "".join(f'<span class="pill ses">🟢 {s}</span>' for s in active_sessions(now))
    op = "".join(f'<span class="pill on">{a["name"]}</span>' for a in open_fx)
    st.markdown(f'<div class="sess"><b>Sessões:</b> {ses} <b style="margin-left:6px">Abertos:</b> {op or "—"}</div>',
                unsafe_allow_html=True)
else:
    st.markdown('<div class="sess">🔴 <b>Forex fechado</b> — apenas cripto (24/7).</div>',
                unsafe_allow_html=True)

# contador de vela + NOVA ENTRADA
components.html(f"""
<div style="font-family:'JetBrains Mono',monospace;color:#eaf0ff;background:rgba(255,255,255,.05);
 border:1px solid rgba(255,255,255,.10);border-radius:16px;padding:14px 20px;display:flex;
 align-items:center;gap:20px;backdrop-filter:blur(10px)">
 <div style="font-size:.68rem;letter-spacing:2px;color:#93a4c8">PRÓXIMA VELA · {TF_LABEL[TF]}</div>
 <div id="clk" style="font-size:2.1rem;font-weight:800;letter-spacing:3px">--:--</div>
 <div id="ent"></div><div style="flex:1"></div>
 <div style="height:9px;flex:0 0 260px;border-radius:6px;background:rgba(255,255,255,.10);overflow:hidden">
   <div id="pb" style="height:100%;width:0%;background:linear-gradient(90deg,#00f5b0,#22d3ee)"></div></div>
</div>
<style>@import url('https://fonts.googleapis.com/css2?family=Inter:wght@800&family=JetBrains+Mono:wght@800&display=swap');
@keyframes np{{0%{{transform:scale(.96);opacity:.6;box-shadow:0 0 0 rgba(0,245,176,.0)}}
50%{{transform:scale(1.04);opacity:1;box-shadow:0 0 26px rgba(0,245,176,.7)}}
100%{{transform:scale(.96);opacity:.6;box-shadow:0 0 0 rgba(0,245,176,.0)}}}}</style>
<script>
var TF={int(TF)};
function t(){{var n=Date.now()/1000,per=TF*60,pos=n%per,left=per-pos;
var m=Math.floor(left/60),s=Math.floor(left%60);
document.getElementById('clk').textContent=(m<10?'0':'')+m+':'+(s<10?'0':'')+s;
document.getElementById('pb').style.width=((pos/per)*100).toFixed(1)+'%';
var e=document.getElementById('ent');
e.innerHTML = pos<12 ? '<span style="font-family:Inter,sans-serif;font-weight:800;letter-spacing:1px;'
 +'padding:7px 15px;border-radius:999px;color:#04120d;background:linear-gradient(90deg,#00f5b0,#22d3ee);'
 +'animation:np 1s infinite">● NOVA ENTRADA</span>' : '';}}
t();setInterval(t,1000);
</script>
""", height=72)


def _bars(forca, n):
    fill = {"FRACO": 1, "MEDIO": 2, "FORTE": 3}.get(forca, 0)
    return "".join(f'<span class="b {"on" if i < fill else ""}"></span>' for i in range(n))


def hero_html(a, r):
    if not r or r[0] == "WAIT":
        return (f'<div class="hero-sig wait"><div class="glow"></div>'
                f'<div class="pair">{a["name"]}</div>'
                f'<div class="dir">◵ AGUARDANDO</div>'
                f'<div class="sub">SEM ENTRADA NO MOMENTO</div></div>')
    _, d, f = r
    cls = "buy" if d == "COMPRA" else "sell"
    arrow = "▲" if d == "COMPRA" else "▼"
    fl = {"FRACO": "FRACA", "MEDIO": "MÉDIA", "FORTE": "FORTE"}[f]
    return (f'<div class="hero-sig {cls}"><div class="glow"></div>'
            f'<div class="pair">{a["name"]}</div>'
            f'<div class="dir">{arrow} {d}</div>'
            f'<div class="fbars">{_bars(f, 3)}<span class="flabel">FORÇA {fl}</span></div></div>')


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
            f'<div class="cb">{_bars(f, 3)}<span class="lab">{fl}</span></div></div>')


# HERO + gráfico
sel = next(a for a in ASSETS if a["name"] == sel_name)
colA, colB = st.columns([1, 1.4])
with colA:
    st.markdown(hero_html(sel, signal_for(sel, cur, hi, TF)), unsafe_allow_html=True)
with colB:
    components.html(f"""
    <div class="tradingview-widget-container" style="height:230px">
      <div class="tradingview-widget-container__widget" style="height:100%"></div>
      <script type="text/javascript" async
        src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js">
      {{"autosize":true,"symbol":"{sel['tv']}","interval":"{TF}","timezone":"Etc/UTC",
       "theme":"dark","style":"1","locale":"br","hide_top_toolbar":false,"hide_side_toolbar":true,
       "allow_symbol_change":false,"backgroundColor":"#0a0f1e","gridColor":"rgba(255,255,255,0.05)"}}
      </script>
    </div>""", height=240)

# grade
st.markdown(f'<div class="gtitle">Ativos · {TF_LABEL[TF]}</div>', unsafe_allow_html=True)
show_list = open_assets if not show_closed else ASSETS
cards = "".join(card_html(a, signal_for(a, cur, hi, TF)) for a in show_list)
st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)

st.markdown('<div class="footline">Uso próprio · não é recomendação financeira</div>',
            unsafe_allow_html=True)
