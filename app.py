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
import io
import json
import math
import os
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# Fuso de exibição: horário de Brasília. Todo o cálculo interno segue em UTC
# (os candles do yfinance são UTC); só a APRESENTAÇÃO é convertida.
try:
    from zoneinfo import ZoneInfo
    BR_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:                                   # fallback: UTC-3 fixo
    BR_TZ = timezone(timedelta(hours=-3))

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

from strategies import (STRATEGIES, add_indicators, score_of, classify,
                        backtest, wilson_ci, breakeven, verdict)

socket.setdefaulttimeout(8)
st.set_page_config(page_title="Sinais IA", page_icon="⚡", layout="wide",
                   initial_sidebar_state="collapsed")

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


def br(ts):
    """Timestamp UTC (naive ou aware) -> horário de Brasília."""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.tz_convert(BR_TZ)


def hm(ts):
    return br(ts).strftime("%H:%M")


def dhm(ts):
    return br(ts).strftime("%d/%m %H:%M")


def fmt_price(name, v):
    if v is None or not math.isfinite(v):
        return "—"
    if name.startswith(("BTC", "ETH")):
        return f"{v:,.0f}".replace(",", ".")
    return f"{v:.3f}" if "JPY" in name else f"{v:.5f}"


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


def sess_window_br(nome):
    """Janela da sessão convertida para horário de Brasília (ex.: '04:00 – 13:00')."""
    s, e = SESSIONS[nome]
    hoje = datetime.now(timezone.utc).date()
    ini = hm(pd.Timestamp(datetime(hoje.year, hoje.month, hoje.day, s, 0, tzinfo=timezone.utc)))
    fim = hm(pd.Timestamp(datetime(hoje.year, hoje.month, hoje.day, e, 0, tzinfo=timezone.utc)))
    return f"{ini} – {fim}"


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


# ============================== DESIGN SYSTEM ==============================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600;700&display=swap');
:root{
  --bg:#07090E; --surf:#0D111A; --surf2:#121724; --line:rgba(255,255,255,.06);
  --line2:rgba(255,255,255,.11); --ink:#E9EDF5; --ink2:#B6C0D4; --mut:#6F7B93;
  --buy:#00C88A; --buy-dim:rgba(0,200,138,.10); --sell:#FF4A63; --sell-dim:rgba(255,74,99,.09);
  --warn:#D9A441; --r:12px; --r2:16px;
}
.stApp{background:var(--bg);color:var(--ink);
  font-family:'Inter',-apple-system,sans-serif;-webkit-font-smoothing:antialiased;}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:1.5rem;padding-bottom:4rem;max-width:1180px}
.mono{font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums}
hr{border-color:var(--line)}

/* ---------- HEADER ---------- */
.hdr{display:flex;align-items:center;justify-content:space-between;gap:24px;
  background:var(--surf);border:1px solid var(--line);border-radius:var(--r2);
  padding:16px 22px;margin-bottom:18px}
.hdr-l{display:flex;align-items:center;gap:26px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:1rem;letter-spacing:-.01em}
.brand .mk{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,var(--buy),#0EA5C6);
  display:flex;align-items:center;justify-content:center;font-size:.7rem;color:#04150F;font-weight:700}
.meta{display:flex;flex-direction:column;gap:3px}
.meta .k{font-size:.58rem;letter-spacing:.14em;color:var(--mut);font-weight:600;text-transform:uppercase}
.meta .v{font-size:.82rem;font-weight:600;color:var(--ink2)}
.dotstat{display:inline-flex;align-items:center;gap:6px;font-size:.82rem;font-weight:600}
.dotstat i{width:6px;height:6px;border-radius:50%;background:var(--buy);
  box-shadow:0 0 0 3px rgba(0,200,138,.15);font-style:normal}
.dotstat.off i{background:var(--sell);box-shadow:0 0 0 3px rgba(255,74,99,.14)}
.sess-tag{font-size:.7rem;color:var(--ink2);font-weight:500}
.sess-tag+.sess-tag:before{content:"·";margin:0 6px;color:var(--mut)}
/* contador dentro do header */
.cd{display:flex;align-items:center;gap:14px;min-width:230px}
.cd .k{font-size:.58rem;letter-spacing:.14em;color:var(--mut);font-weight:600}
.cd .t{font-family:'IBM Plex Mono',monospace;font-size:1.5rem;font-weight:600;letter-spacing:.02em;
  font-variant-numeric:tabular-nums}
.cd .track{height:3px;flex:1;border-radius:2px;background:rgba(255,255,255,.07);overflow:hidden;min-width:70px}
.cd .fill{height:100%;background:var(--buy);transition:width .9s linear}

/* ---------- CONTROLES ---------- */
.lbl{font-size:.58rem;letter-spacing:.14em;color:var(--mut);font-weight:600;
  text-transform:uppercase;margin-bottom:7px}
div[role="radiogroup"]{gap:3px!important;background:var(--surf);border:1px solid var(--line);
  border-radius:10px;padding:4px;display:inline-flex;align-items:center;
  flex-wrap:nowrap!important;white-space:nowrap}
div[role="radiogroup"] label{background:transparent;border:0;margin:0;
  padding:6px 11px;border-radius:7px;font-weight:600;font-size:.78rem;
  transition:background .15s;cursor:pointer;white-space:nowrap;flex:0 0 auto}
div[role="radiogroup"] label:hover{background:rgba(255,255,255,.04)}
div[role="radiogroup"] label:has(input:checked){background:var(--surf2)}
div[role="radiogroup"] [data-testid="stMarkdownContainer"] p{font-size:.8rem!important;
  font-weight:600!important;margin:0!important}
.stMultiSelect div[data-baseweb="select"]>div{background:var(--surf);border:1px solid var(--line);
  border-radius:10px;min-height:42px}
.stMultiSelect [data-baseweb="tag"]{background:var(--surf2)!important;border:1px solid var(--line2)!important;
  border-radius:7px!important;color:var(--ink2)!important;font-size:.72rem!important;font-weight:500!important}
div[data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"]{background:var(--buy)!important}
div[data-testid="stExpander"]{border:1px solid var(--line);border-radius:var(--r);background:var(--surf)}
div[data-testid="stExpander"] summary{font-weight:600;font-size:.82rem;color:var(--ink2)}
div[data-testid="stExpander"] summary:hover{color:var(--ink)}

/* ---------- TABS ---------- */
.stTabs [data-baseweb="tab-list"]{gap:2px;border-bottom:1px solid var(--line);background:transparent}
.stTabs [data-baseweb="tab"]{background:transparent;border-radius:0;padding:11px 2px;margin-right:26px;
  font-weight:600;font-size:.86rem;color:var(--mut);border-bottom:2px solid transparent}
.stTabs [aria-selected="true"]{color:var(--ink);border-bottom:2px solid var(--buy)}
.stTabs [data-baseweb="tab-highlight"]{display:none}

/* ---------- HERO ---------- */
.hero{display:grid;grid-template-columns:1.35fr 1fr;gap:0;border-radius:var(--r2);overflow:hidden;
  border:1px solid var(--line);background:var(--surf);margin-top:20px}
.hero.buy{border-color:rgba(0,200,138,.25)}
.hero.sell{border-color:rgba(255,74,99,.22)}
.hero-main{padding:30px 32px;position:relative}
.hero.buy .hero-main{background:linear-gradient(120deg,var(--buy-dim),transparent 70%)}
.hero.sell .hero-main{background:linear-gradient(120deg,var(--sell-dim),transparent 70%)}
.hero-side{padding:24px 26px;border-left:1px solid var(--line);background:rgba(255,255,255,.012);
  display:flex;flex-direction:column;justify-content:center;gap:16px}
.h-tag{font-size:.56rem;letter-spacing:.16em;font-weight:600;color:var(--mut);text-transform:uppercase}
.h-pair{font-size:1.5rem;font-weight:600;letter-spacing:-.01em;margin-top:5px;color:var(--ink)}
.h-dir{font-family:'IBM Plex Mono',monospace;font-size:3.1rem;font-weight:700;line-height:1;
  margin:14px 0 4px;display:flex;align-items:baseline;gap:12px;letter-spacing:-.02em}
.h-dir .ar{font-size:1.7rem}
.buy .h-dir{color:var(--buy)} .sell .h-dir{color:var(--sell)} .wait .h-dir{color:var(--ink2);font-size:2rem}
.h-row{display:flex;flex-direction:column;gap:4px}
.h-k{font-size:.56rem;letter-spacing:.14em;color:var(--mut);font-weight:600;text-transform:uppercase}
.h-v{font-size:.95rem;font-weight:600;color:var(--ink)}
.h-v.mono{font-size:1.05rem}

/* ---------- FORÇA ---------- */
.fb{display:flex;gap:4px;align-items:center}
.fb .b{width:34px;height:4px;border-radius:2px;background:rgba(255,255,255,.09)}
.buy .b.on{background:var(--buy)} .sell .b.on{background:var(--sell)}
.fb .lbl2{margin-left:10px;font-size:.68rem;letter-spacing:.1em;font-weight:600;color:var(--ink2)}

/* ---------- CHIPS ---------- */
.strats{display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.sc{font-size:.66rem;font-weight:500;padding:4px 9px;border-radius:6px;
  background:var(--surf2);color:var(--ink2);border:1px solid var(--line)}
.conf{font-size:.58rem;font-weight:600;letter-spacing:.08em;padding:4px 9px;border-radius:6px;
  background:var(--buy-dim);color:var(--buy);border:1px solid rgba(0,200,138,.28)}

/* ---------- SEÇÃO / GRADE ---------- */
.sect{margin:30px 0 14px;font-size:.6rem;font-weight:600;letter-spacing:.16em;color:var(--mut);
  text-transform:uppercase;display:flex;align-items:center;gap:10px}
.sect:after{content:"";flex:1;height:1px;background:var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:12px}
.card{border-radius:var(--r);background:var(--surf);border:1px solid var(--line);overflow:hidden;
  transition:border-color .16s ease,transform .16s ease}
.card:hover{border-color:var(--line2);transform:translateY(-2px)}
.card .top{height:2px;background:var(--line)}
.card.buy .top{background:var(--buy)} .card.sell .top{background:var(--sell)}
.card .body{padding:15px 16px}
.card .row1{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:9px}
.card .p{font-size:.9rem;font-weight:600;color:var(--ink)}
.card .px{font-family:'IBM Plex Mono',monospace;font-size:.75rem;color:var(--mut);font-variant-numeric:tabular-nums}
.card .d{font-family:'IBM Plex Mono',monospace;font-size:1.15rem;font-weight:700;margin-bottom:10px;
  display:flex;align-items:center;gap:7px}
.card.buy .d{color:var(--buy)} .card.sell .d{color:var(--sell)}
.card .fb{margin-bottom:10px}
.card .fb .b{width:20px;height:3px} .card .fb .lbl2{margin-left:8px;font-size:.6rem}
.card .sc{font-size:.6rem;padding:3px 7px}

/* ---------- TABELA ---------- */
.tbl{width:100%;border-collapse:collapse;font-size:.83rem}
.tbl th{text-align:left;font-size:.56rem;letter-spacing:.14em;color:var(--mut);font-weight:600;
  text-transform:uppercase;padding:0 14px 10px;border-bottom:1px solid var(--line)}
.tbl td{padding:13px 14px;border-bottom:1px solid var(--line)}
.tbl tr:hover td{background:rgba(255,255,255,.014)}
.tbl tr.on td{background:rgba(0,200,138,.035)}
.tbl .nm{font-weight:600;color:var(--ink)}
.wr{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:.98rem;font-variant-numeric:tabular-nums}
.good{color:var(--buy)} .bad{color:var(--sell)} .mid{color:var(--ink2)}
.ci{font-family:'IBM Plex Mono',monospace;font-size:.66rem;color:var(--mut);margin-left:5px}
.n{color:var(--mut);font-size:.7rem}
.verd{font-size:.56rem;font-weight:600;letter-spacing:.06em;padding:3px 7px;border-radius:5px;margin-left:6px}
.v-good{background:var(--buy-dim);color:var(--buy)}
.v-bad{background:var(--sell-dim);color:var(--sell)}
.v-mid{background:rgba(217,164,65,.10);color:var(--warn)}
.tagmini{font-size:.54rem;font-weight:600;letter-spacing:.06em;padding:2px 6px;border-radius:5px;
  background:var(--surf2);color:var(--mut);border:1px solid var(--line);margin-left:7px}
.note{margin-top:18px;font-size:.72rem;color:var(--mut);border-left:2px solid var(--line2);
  padding:2px 0 2px 14px;line-height:1.65}
.note b{color:var(--ink2);font-weight:600}
.foot{margin-top:34px;padding-top:18px;border-top:1px solid var(--line);text-align:center;
  font-size:.64rem;color:#4E5872}
div[data-testid="stMetric"]{background:var(--surf);border:1px solid var(--line);
  border-radius:var(--r);padding:14px 16px}
div[data-testid="stMetricLabel"] p{font-size:.58rem!important;letter-spacing:.14em;
  text-transform:uppercase;color:var(--mut)!important;font-weight:600!important}
div[data-testid="stMetricValue"]{font-family:'IBM Plex Mono',monospace;font-size:1.3rem;
  font-variant-numeric:tabular-nums}
/* ---------- janela de entrada / alertas ---------- */
.win{display:flex;align-items:center;gap:10px;border-radius:10px;padding:11px 16px;
  font-size:.82rem;color:var(--ink2);margin:16px 0 4px;border:1px solid var(--line)}
.win .pt{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.win b{color:var(--ink);font-weight:600}
.win.ok{background:var(--buy-dim);border-color:rgba(0,200,138,.28)}
.win.ok .pt{background:var(--buy);box-shadow:0 0 0 3px rgba(0,200,138,.18);
  animation:bl 1.2s ease-in-out infinite}
.win.wait{background:var(--surf)}
.win.wait .pt{background:var(--mut)}
.win.alert{background:rgba(217,164,65,.09);border-color:rgba(217,164,65,.3);color:#E4C48A}
.win.alert .pt{background:var(--warn)}
@keyframes bl{0%,100%{opacity:.45}50%{opacity:1}}
.hero.stale,.card.stale{opacity:.42;filter:saturate(.55)}
.hero.stale{border-color:var(--line)}
/* remove o vão que o iframe do contador cria */
div[data-testid="element-container"]:has(iframe){margin-top:-6px;margin-bottom:-10px}
div[data-testid="stExpander"]{margin-bottom:4px}
@media(max-width:900px){.hero{grid-template-columns:1fr}.hero-side{border-left:0;border-top:1px solid var(--line)}}
</style>
""", unsafe_allow_html=True)

# ============================== CONTROLES (no corpo da página) ==============================
topbar_slot = st.empty()          # a barra de status é preenchida depois (precisa dos dados)
st.markdown('<div class="ctrlbar">', unsafe_allow_html=True)
cc1, cc2, cc3 = st.columns([1.05, 2.1, 1.05])
with cc1:
    st.markdown('<div class="lbl">Timeframe</div>', unsafe_allow_html=True)
    tf_label = st.radio("tf", ["1 min", "5 min", "15 min"], index=1, horizontal=True,
                        label_visibility="collapsed")
    TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]
with cc2:
    st.markdown('<div class="lbl">Estratégias ativas</div>', unsafe_allow_html=True)
    default_sel = [k for k in ("G · Fade vela extrema", "J · Z-score forte", "K · Reversão dupla")
                   if k in STRATEGIES]
    sel_strats = st.multiselect("est", list(STRATEGIES), default=default_sel,
                                placeholder="Escolha uma ou mais estratégias",
                                label_visibility="collapsed")
    if not sel_strats:
        sel_strats = default_sel or [list(STRATEGIES)[0]]
with cc3:
    st.markdown('<div class="lbl">Força mínima</div>', unsafe_allow_html=True)
    min_force = st.select_slider("fm", options=["FRACA", "MÉDIA", "FORTE"], value="FRACA",
                                 label_visibility="collapsed")
st.markdown('</div>', unsafe_allow_html=True)

with st.expander("Mais opções — filtros, áudio, payout e atualização"):
    o1, o2, o3 = st.columns(3)
    with o1:
        st.markdown("**Filtros**")
        only_conf = st.toggle("Só entradas com 2+ estratégias", value=False)
        show_closed = st.toggle("Incluir pares fora de sessão", value=False)
    with o2:
        st.markdown("**Áudio**")
        audio_on = st.toggle("🔊 Aviso por voz na entrada", value=False)
        st.caption("O navegador exige um clique para liberar som — o botão aparece na aba Sinais.")
    with o3:
        st.markdown("**Análise e atualização**")
        payout_lbl = st.radio("Payout da corretora", ["80%", "90%"], index=0, horizontal=True)
        auto_on = st.toggle("Atualização automática", value=True)
        every = st.slider("Intervalo (s)", 10, 60, 15, step=5, disabled=not auto_on)
    st.markdown("**Sessões do mercado — horário de Brasília**")
    ativas = set(active_sessions(datetime.now(timezone.utc)))
    BADGE = '<span class="verd v-good">ativa</span>'
    linhas = ""
    for n in SESSIONS:
        marca = BADGE if n in ativas else ""
        linhas += (f'<tr><td class="nm">{n} {marca}</td>'
                   f'<td class="mono n">{sess_window_br(n)}</td></tr>')
    st.markdown(f'<table class="tbl" style="max-width:420px">'
                f'<tr><th>Sessão</th><th>Janela (BRT)</th></tr>{linhas}</table>',
                unsafe_allow_html=True)
PAYOUT = 0.80 if payout_lbl == "80%" else 0.90
BE = breakeven(PAYOUT) * 100

now = datetime.now(timezone.utc)
interval, minutes = TF_YF[TF], int(TF)

# ---- janela de entrada: o backtest assume entrada na ABERTURA da vela ----
ENTRY_WINDOW = 20                                   # segundos válidos após a virada
_per = minutes * 60
_age = now.timestamp() % _per                       # segundos decorridos da vela atual
secs_to_next = _per - _age
window_open = _age <= ENTRY_WINDOW

# refresh: acorda exatamente na virada da vela (assim a janela nunca é perdida)
if auto_on:
    st_autorefresh(interval=int(min(every, max(3, secs_to_next + 1)) * 1000), key="auto")

open_assets = [a for a in ASSETS if pair_open(a, now)]
scan_list = ASSETS if show_closed else open_assets
data = get_data(scan_list, interval, minutes)


def data_lag(data_map):
    """Maior atraso (min) entre a última vela recebida e o relógio."""
    ref = pd.Timestamp(now).tz_localize(None)
    pior, quem = None, None
    for nome, df in data_map.items():
        if df is None or len(df) == 0:
            continue
        lag = (ref - df.index[-1]).total_seconds() / 60.0
        if pior is None or lag > pior:
            pior, quem = lag, nome
    return pior, quem


lag_min, lag_asset = data_lag(data)
dados_atrasados = lag_min is not None and lag_min > (2 * minutes + 1)

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
        px = float(d["Close"].iloc[-1]) if len(d) else float("nan")
        e = agg.setdefault(key, {"a": a, "dir": r[0], "force": r[1], "score": abs(last),
                                 "strats": [], "px": px})
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
    stat = '<span class="dotstat"><i></i>Mercado aberto</span>'
    sess = "".join(f'<span class="sess-tag" title="{s}: {sess_window_br(s)} (Brasília)">{s}</span>'
                   for s in active_sessions(now)) or '<span class="sess-tag">—</span>'
else:
    stat = '<span class="dotstat off"><i></i>Forex fechado</span>'
    sess = '<span class="sess-tag">fim de semana · cripto 24/7</span>'

topbar_slot.markdown(f"""
<div class="hdr">
  <div class="hdr-l">
    <div class="brand"><span class="mk">S</span>Sinais IA</div>
    <div class="meta"><span class="k">Status</span><span class="v">{stat}</span></div>
    <div class="meta"><span class="k">Sessões</span><span class="v">{sess}</span></div>
    <div class="meta"><span class="k">Timeframe</span><span class="v">{TF_LABEL[TF]}</span></div>
    <div class="meta"><span class="k">Estratégias</span><span class="v">{len(sel_strats)} ativas</span></div>
    <div class="meta"><span class="k">Varredura</span><span class="v">{len(scan_list)} ativos</span></div>
    <div class="meta"><span class="k">Horário de Brasília</span>
      <span class="v mono">{br(now).strftime('%H:%M:%S')}</span></div>
  </div>
</div>""", unsafe_allow_html=True)

components.html(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@500;600&family=IBM+Plex+Mono:wght@600&display=swap');
*{{box-sizing:border-box}} body{{margin:0}}
.bar{{font-family:'Inter',sans-serif;background:#0D111A;border:1px solid rgba(255,255,255,.06);
 border-radius:12px;padding:11px 20px;display:flex;align-items:center;gap:16px;color:#E9EDF5}}
.k{{font-size:.56rem;letter-spacing:.14em;color:#6F7B93;font-weight:600;text-transform:uppercase}}
.t{{font-family:'IBM Plex Mono',monospace;font-size:1.35rem;font-weight:600;font-variant-numeric:tabular-nums}}
.track{{height:3px;flex:1;border-radius:2px;background:rgba(255,255,255,.07);overflow:hidden}}
.fill{{height:100%;background:#00C88A}}
.badge{{font-size:.6rem;font-weight:600;letter-spacing:.08em;padding:4px 10px;border-radius:6px;
 background:rgba(0,200,138,.12);color:#00C88A;border:1px solid rgba(0,200,138,.28)}}
@keyframes pulse{{0%,100%{{opacity:.5}}50%{{opacity:1}}}}
</style>
<div class="bar">
  <span class="k">Próxima vela</span>
  <span class="t" id="k">--:--</span>
  <span id="e"></span>
  <div class="track"><div class="fill" id="f" style="width:0%"></div></div>
</div>
<script>var TF={int(TF)};function t(){{var n=Date.now()/1000,per=TF*60,pos=n%per,l=per-pos,
m=Math.floor(l/60),s=Math.floor(l%60);
document.getElementById('k').textContent=(m<10?'0':'')+m+':'+(s<10?'0':'')+s;
document.getElementById('f').style.width=((pos/per)*100).toFixed(1)+'%';
document.getElementById('e').innerHTML=pos<{ENTRY_WINDOW}?'<span class="badge" style="animation:pulse 1.1s infinite">ENTRADA VÁLIDA</span>':'';}}
t();setInterval(t,1000);</script>""", height=52)

def _short(nm):
    return nm.split("·")[0].strip()


# ---------- persistência do histórico ----------
HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hist_signals.json")


def hist_load():
    """Carrega do disco na primeira execução da sessão."""
    if "hist" in st.session_state:
        return st.session_state["hist"]
    h = []
    try:
        if os.path.exists(HIST_PATH):
            with open(HIST_PATH, "r", encoding="utf-8") as f:
                for r in json.load(f):
                    r["ts"] = pd.Timestamp(r["ts"])
                    h.append(r)
    except Exception:
        h = []
    st.session_state["hist"] = h
    return h


def hist_save(h):
    try:
        out = [{**r, "ts": pd.Timestamp(r["ts"]).isoformat()} for r in h]
        with open(HIST_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass


def hist_df(h):
    if not h:
        return pd.DataFrame()
    return pd.DataFrame([{
        "data_hora_brasilia": dhm(r["ts"]), "utc": pd.Timestamp(r["ts"]).isoformat(),
        "ativo": r["asset"], "direcao": r["dir"], "forca": FL.get(r["force"], r["force"]),
        "estrategias": "+".join(r["strats"]), "timeframe_min": r.get("tf", ""),
        "resultado": r["res"] or "aguardando",
    } for r in sorted(h, key=lambda x: x["ts"], reverse=True)])


# ---------- registra os sinais emitidos e apura o resultado pela cor da vela ----------
def record_and_resolve(entries, data, minutes):
    hist = hist_load()
    ck = candle_key(minutes)
    start = pd.Timestamp(ck * minutes * 60, unit="s")     # abertura da vela da entrada
    seen = {(h["asset"], h["dir"], h["ck"], h.get("tf")) for h in hist}
    changed = False
    for e in entries:
        k = (e["a"]["name"], e["dir"], ck, minutes)
        if k not in seen:
            hist.append({"ck": ck, "ts": start, "asset": e["a"]["name"], "dir": e["dir"],
                         "force": e["force"], "strats": [_short(s) for s in e["strats"]],
                         "tf": minutes, "res": None})
            seen.add(k)
            changed = True
    for h in hist:                                        # apura o que já fechou
        if h["res"] is not None:
            continue
        if h.get("tf") not in (None, minutes):            # só apura o timeframe atual
            continue
        df = data.get(h["asset"])
        if df is None or len(df) == 0:
            continue
        ts = h["ts"]
        if ts in df.index and df.index[-1] > ts:
            row = df.loc[ts]
            op, cl = float(row["Open"]), float(row["Close"])
            if cl == op:
                h["res"] = "empate"
            else:
                venceu = (cl > op) == (h["dir"] == "COMPRA")
                h["res"] = "ganhou" if venceu else "perdeu"
            changed = True
    if len(hist) > 3000:
        del hist[:len(hist) - 3000]
    if changed:
        hist_save(hist)
    return hist


hist = record_and_resolve(entries, data, minutes)

tab_sig, tab_perf, tab_hist = st.tabs(["Sinais", "Desempenho", "Histórico"])


def bars(f):
    n = FORCE_ORDER.get(f, 0)
    return "".join(f'<span class="b {"on" if i < n else ""}"></span>' for i in range(3))


def chips(e, big=False):
    n = len(e["strats"])
    c = f'<span class="conf">{n} concordam</span>' if n > 1 else ""
    if big:
        c += "".join(f'<span class="sc">{s}</span>' for s in e["strats"])
    else:
        c += "".join(f'<span class="sc">{_short(s)}</span>' for s in e["strats"])
    return f'<div class="strats">{c}</div>'


def hero_html(e, cvela):
    cls = "buy" if e["dir"] == "COMPRA" else "sell"
    ar = "▲" if e["dir"] == "COMPRA" else "▼"
    return f"""<div class="hero {cls}">
      <div class="hero-main">
        <div class="h-tag">Melhor entrada</div>
        <div class="h-pair">{e["a"]["name"]}</div>
        <div class="h-dir"><span class="ar">{ar}</span>{e["dir"]}</div>
        <div class="fb">{bars(e["force"])}<span class="lbl2">Força {FL[e["force"]].lower()}</span></div>
      </div>
      <div class="hero-side">
        <div class="h-row"><span class="h-k">Preço atual</span>
          <span class="h-v mono">{fmt_price(e["a"]["name"], e.get("px"))}</span></div>
        <div class="h-row"><span class="h-k">Entrada na vela</span>
          <span class="h-v mono">{cvela}</span></div>
        <div class="h-row"><span class="h-k">Estratégias</span>{chips(e, big=True)}</div>
      </div></div>"""


def card_html(e):
    cls = "buy" if e["dir"] == "COMPRA" else "sell"
    ar = "▲" if e["dir"] == "COMPRA" else "▼"
    return (f'<div class="card {cls}"><div class="top"></div><div class="body">'
            f'<div class="row1"><span class="p">{e["a"]["name"]}</span>'
            f'<span class="px">{fmt_price(e["a"]["name"], e.get("px"))}</span></div>'
            f'<div class="d">{ar} {e["dir"]}</div>'
            f'<div class="fb">{bars(e["force"])}<span class="lbl2">{FL[e["force"]].lower()}</span></div>'
            f'{chips(e)}</div></div>')


# ============================== ABA SINAIS ==============================
with tab_sig:
    cvela = hm(pd.Timestamp(candle_key(minutes) * minutes * 60, unit="s"))

    # --- estado da janela de entrada ---
    if window_open:
        st.markdown(f'<div class="win ok"><span class="pt"></span>'
                    f'<b>Entrada válida agora</b> — vela das {cvela}. '
                    f'Restam {int(ENTRY_WINDOW - _age)}s desta janela.</div>', unsafe_allow_html=True)
    else:
        mm, ss = divmod(int(secs_to_next), 60)
        st.markdown(f'<div class="win wait"><span class="pt"></span>'
                    f'<b>Vela em andamento</b> — já se passaram {int(_age)}s desta vela. '
                    f'Próxima janela de entrada em {mm:02d}:{ss:02d}.</div>', unsafe_allow_html=True)

    if dados_atrasados:
        st.markdown(f'<div class="win alert"><span class="pt"></span>'
                    f'<b>Dados atrasados</b> — a vela mais recente ({lag_asset}) chegou há '
                    f'{lag_min:.0f} min. O sinal pode estar calculado sobre dados vencidos.</div>',
                    unsafe_allow_html=True)

    if entries:
        dim = "" if window_open else " stale"
        st.markdown(hero_html(entries[0], cvela).replace('class="hero ', f'class="hero{dim} '),
                    unsafe_allow_html=True)
        rest = entries[1:]
        st.markdown(f'<div class="sect">Outras entradas · {len(entries)} no total</div>',
                    unsafe_allow_html=True)
        if rest:
            cards = "".join(card_html(e).replace('class="card ', f'class="card{dim} ') for e in rest)
            st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)
        else:
            st.caption("Esta é a única entrada no momento.")
    else:
        st.markdown(f"""<div class="hero wait">
          <div class="hero-main">
            <div class="h-tag">Scanner</div>
            <div class="h-pair">Mercado</div>
            <div class="h-dir">Nenhuma entrada</div>
            <div class="fb"><span class="lbl2" style="margin-left:0">Aguardando setup</span></div>
          </div>
          <div class="hero-side">
            <div class="h-row"><span class="h-k">Próxima vela</span>
              <span class="h-v mono">{cvela}</span></div>
            <div class="h-row"><span class="h-k">Estratégias ativas</span>
              <span class="h-v">{len(sel_strats)}</span></div>
            <div class="h-row"><span class="h-k">Ativos varridos</span>
              <span class="h-v">{len(scan_list)}</span></div>
          </div></div>""", unsafe_allow_html=True)
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
          if(pos<{ENTRY_WINDOW}&&sessionStorage.getItem('dito')!=String(c)){{
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

    st.markdown(f'<div class="sect">Desempenho · {TF_LABEL[TF]} · payout {payout_lbl} '
                f'· breakeven {BE:.2f}%</div>', unsafe_allow_html=True)
    st.markdown(f'<table class="tbl"><tr><th>Estratégia</th><th>Hoje</th>'
                f'<th>Período ({TF_PERIOD[interval]})</th></tr>{rows}</table>', unsafe_allow_html=True)
    st.markdown('<div class="note"><b>Como ler:</b> a taxa é medida operando toda vez que a estratégia '
                'dispara, entrando na <b>abertura da vela seguinte</b>. Acerto pela cor da vela: COMPRA '
                'vence se fechar <b style="color:#00e5a0">verde</b>, VENDA se fechar '
                '<b style="color:#ff4d6d">vermelha</b>; empate devolve a aposta.<br>'
                '<b>IC95</b> é a faixa onde a taxa real provavelmente está. Só existe vantagem se '
                '<b>toda</b> a faixa ficar acima do breakeven — quando ela cruza, o resultado é '
                '<b>não conclusivo</b> e escolher pela maior taxa é perseguir ruído.</div>',
                unsafe_allow_html=True)

# ============================== ABA HISTÓRICO ==============================
with tab_hist:
    if not hist:
        st.markdown('<div class="sect">Histórico de sinais</div>', unsafe_allow_html=True)
        st.caption("Ainda não há sinais registrados nesta sessão. Cada entrada que aparecer "
                   "será gravada aqui e o resultado apurado quando a vela fechar.")
    else:
        fechados = [h for h in hist if h["res"] in ("ganhou", "perdeu")]
        g = sum(1 for h in fechados if h["res"] == "ganhou")
        emp = sum(1 for h in hist if h["res"] == "empate")
        abertos = sum(1 for h in hist if h["res"] is None)
        taxa = (g / len(fechados) * 100) if fechados else float("nan")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Sinais registrados", len(hist))
        m2.metric("Resolvidos", len(fechados))
        m3.metric("Acertos", g)
        m4.metric("Taxa da sessão", f"{taxa:.1f}%" if fechados else "—")
        if fechados:
            p, lo, hi = wilson_ci(g, len(fechados))
            v = verdict(g, len(fechados), PAYOUT)
            txt = {"acima": "acima do breakeven", "abaixo": "abaixo do breakeven",
                   "inconclusivo": "não conclusivo", "sem dados": "sem dados"}[v]
            st.caption(f"IC95 {lo*100:.0f}–{hi*100:.0f}% · **{txt}** "
                       f"(breakeven {BE:.2f}% com payout {payout_lbl}) · "
                       f"{emp} empate(s) devolvido(s) · {abertos} aguardando fechar")

        rows = ""
        for h in sorted(hist, key=lambda x: x["ts"], reverse=True)[:60]:
            if h["res"] == "ganhou":
                r = '<span class="verd v-good">ganhou</span>'
            elif h["res"] == "perdeu":
                r = '<span class="verd v-bad">perdeu</span>'
            elif h["res"] == "empate":
                r = '<span class="verd v-mid">empate</span>'
            else:
                r = '<span class="verd v-mid">aguardando</span>'
            dcls = "good" if h["dir"] == "COMPRA" else "bad"
            arw = "▲" if h["dir"] == "COMPRA" else "▼"
            chips_h = "".join(f'<span class="sc">{s}</span>' for s in h["strats"])
            rows += (f'<tr><td class="n">{dhm(h["ts"])}</td>'
                     f'<td class="nm">{h["asset"]}</td>'
                     f'<td class="{dcls}" style="font-weight:800">{arw} {h["dir"]}</td>'
                     f'<td class="n">{FL[h["force"]]}</td>'
                     f'<td>{chips_h}</td><td>{r}</td></tr>')
        st.markdown('<div class="sect">Sinais desta sessão</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl"><tr><th>Vela</th><th>Ativo</th><th>Direção</th>'
                    f'<th>Força</th><th>Estratégias</th><th>Resultado</th></tr>{rows}</table>',
                    unsafe_allow_html=True)
        st.markdown('<div class="note"><b>Este é o teste que vale.</b> Aqui não há backtest nem '
                    'seleção de período: são os sinais que o app realmente emitiu, apurados pela '
                    'cor da vela em que a entrada valeria. É a amostra mais honesta que existe — '
                    'e a única livre de garimpo de dados.</div>', unsafe_allow_html=True)

    st.markdown('<div class="sect">Backup e importação</div>', unsafe_allow_html=True)
    b1, b2, b3 = st.columns([1, 1.4, 1])
    with b1:
        df_h = hist_df(hist)
        st.download_button("Baixar histórico (CSV)",
                           data=(df_h.to_csv(index=False).encode("utf-8-sig") if not df_h.empty
                                 else "sem dados".encode()),
                           file_name=f"sinais_historico_{br(now).strftime('%Y%m%d_%H%M')}.csv",
                           mime="text/csv", disabled=df_h.empty, use_container_width=True)
    with b2:
        up = st.file_uploader("Importar CSV salvo antes (mescla sem duplicar)",
                              type=["csv"], label_visibility="collapsed")
        if up is not None:
            try:
                imp = pd.read_csv(up)
                cur = hist_load()
                seen = {(r["asset"], r["dir"], r["ck"], r.get("tf")) for r in cur}
                add = 0
                for _, r in imp.iterrows():
                    ts = pd.Timestamp(r["utc"])
                    tf = int(r.get("timeframe_min") or 0) or None
                    ckk = int(ts.timestamp() // 60 // (tf or 5))
                    key = (r["ativo"], r["direcao"], ckk, tf)
                    if key in seen:
                        continue
                    inv = {v: k for k, v in FL.items()}
                    cur.append({"ck": ckk, "ts": ts, "asset": r["ativo"], "dir": r["direcao"],
                                "force": inv.get(r["forca"], "FRACA"),
                                "strats": str(r["estrategias"]).split("+"), "tf": tf,
                                "res": None if r["resultado"] == "aguardando" else r["resultado"]})
                    seen.add(key); add += 1
                hist_save(cur)
                st.success(f"{add} registro(s) importado(s).")
            except Exception as ex:
                st.error(f"Não consegui ler o CSV: {ex}")
    with b3:
        if st.button("Limpar histórico", use_container_width=True):
            st.session_state["hist"] = []
            hist_save([])
            st.rerun()
    st.markdown('<div class="note">O histórico é gravado em arquivo no servidor, então '
                '<b>sobrevive a recarregar a página</b>. Mas o disco do Streamlit Cloud é '
                '<b>efêmero</b>: quando o app hiberna por inatividade ou recebe uma atualização, '
                'ele é zerado. Para acumular semanas de dados de verdade, <b>baixe o CSV</b> de '
                'vez em quando e reimporte — o backup no seu computador é o que dura.</div>',
                unsafe_allow_html=True)

st.markdown('<div class="foot">Uso próprio · não é recomendação financeira</div>', unsafe_allow_html=True)
