"""
app.py — Sinais IA (ferramenta educativa / conta demo).

Design premium dark. Mostra apenas: ATIVO, SINAL (COMPRA/VENDA) e FORCA
(FRACO/MEDIO/FORTE). Nao expoe o motor (nada de RSI/MACD na tela).

Fonte do sinal: rating oficial de Analise Tecnica do TradingView via a lib
`tradingview_ta` (roda no servidor, sem CORS). Mapeamento:
  STRONG_BUY  -> COMPRA / FORTE
  BUY         -> COMPRA / MEDIO
  NEUTRAL     -> direcao pelo saldo compra-venda / FRACO
  SELL        -> VENDA  / MEDIO
  STRONG_SELL -> VENDA  / FORTE
Confluencia: se o timeframe imediatamente maior concordar, reforca a forca.

Entradas indicadas no COMECO de cada vela: contador regressivo (JS client-side)
ate a proxima vela do timeframe escolhido, com destaque "NOVA ENTRADA" na virada.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone

import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Sinais IA — Educativo", page_icon="⚡", layout="wide")

# --------------------------------------------------------------------------
# Ativos (mercado aberto) + cripto 24/7
# --------------------------------------------------------------------------
ASSETS = [
    {"name": "EUR/USD", "tv": "FX_IDC:EURUSD", "ex": "FX_IDC", "scr": "forex", "cur": ["EUR", "USD"], "type": "fx"},
    {"name": "GBP/USD", "tv": "FX_IDC:GBPUSD", "ex": "FX_IDC", "scr": "forex", "cur": ["GBP", "USD"], "type": "fx"},
    {"name": "USD/JPY", "tv": "FX_IDC:USDJPY", "ex": "FX_IDC", "scr": "forex", "cur": ["USD", "JPY"], "type": "fx"},
    {"name": "AUD/USD", "tv": "FX_IDC:AUDUSD", "ex": "FX_IDC", "scr": "forex", "cur": ["AUD", "USD"], "type": "fx"},
    {"name": "USD/CAD", "tv": "FX_IDC:USDCAD", "ex": "FX_IDC", "scr": "forex", "cur": ["USD", "CAD"], "type": "fx"},
    {"name": "USD/CHF", "tv": "FX_IDC:USDCHF", "ex": "FX_IDC", "scr": "forex", "cur": ["USD", "CHF"], "type": "fx"},
    {"name": "NZD/USD", "tv": "FX_IDC:NZDUSD", "ex": "FX_IDC", "scr": "forex", "cur": ["NZD", "USD"], "type": "fx"},
    {"name": "EUR/JPY", "tv": "FX_IDC:EURJPY", "ex": "FX_IDC", "scr": "forex", "cur": ["EUR", "JPY"], "type": "fx"},
    {"name": "BTC/USD", "tv": "BINANCE:BTCUSDT", "ex": "BINANCE", "scr": "crypto", "cur": [], "type": "crypto"},
    {"name": "ETH/USD", "tv": "BINANCE:ETHUSDT", "ex": "BINANCE", "scr": "crypto", "cur": [], "type": "crypto"},
]
BY_TV = {a["tv"]: a for a in ASSETS}

SESSIONS = {"Sydney": (21, 6), "Toquio": (23, 8), "Londres": (7, 16), "Nova York": (12, 21)}
CUR_SESS = {"AUD": "Sydney", "NZD": "Sydney", "JPY": "Toquio", "EUR": "Londres",
            "GBP": "Londres", "CHF": "Londres", "USD": "Nova York", "CAD": "Nova York"}
INTERVAL_NAME = {"1": "INTERVAL_1_MINUTE", "5": "INTERVAL_5_MINUTES", "15": "INTERVAL_15_MINUTES"}
HIGHER = {"1": "5", "5": "15", "15": "15"}
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
    act = set(active_sessions(d))
    return any(CUR_SESS.get(c) in act for c in a["cur"])


# --------------------------------------------------------------------------
# Dados: rating do TradingView (batch por screener/intervalo), cache por vela
# --------------------------------------------------------------------------
@st.cache_data(ttl=55, show_spinner=False)
def fetch_batch(screener, interval_name, symbols, candle_key):
    """Uma chamada para varios simbolos. Retorna {tv_symbol: summary|None}."""
    from tradingview_ta import get_multiple_analysis, Interval
    iv = getattr(Interval, interval_name)
    out = {}
    try:
        res = get_multiple_analysis(screener=screener, interval=iv, symbols=list(symbols))
        for k, v in res.items():
            out[k] = v.summary if v else None
    except Exception:
        for s in symbols:
            out[s] = None
    return out


def classify(summary):
    """summary -> (direcao, forca) sem confluencia."""
    if not summary:
        return None, None
    rec = summary.get("RECOMMENDATION", "NEUTRAL")
    buy, sell = summary.get("BUY", 0), summary.get("SELL", 0)
    if rec in ("STRONG_BUY", "BUY"):
        direc = "COMPRA"
    elif rec in ("STRONG_SELL", "SELL"):
        direc = "VENDA"
    else:
        direc = "COMPRA" if buy > sell else ("VENDA" if sell > buy else "NEUTRO")
    forca = "FORTE" if rec in ("STRONG_BUY", "STRONG_SELL") else ("MEDIO" if rec in ("BUY", "SELL") else "FRACO")
    return direc, forca


def apply_confluence(direc, forca, higher_summary):
    if not higher_summary or direc not in ("COMPRA", "VENDA"):
        return forca
    hd, _ = classify(higher_summary)
    if hd == direc and forca == "MEDIO":
        return "FORTE"
    if hd and hd != direc and forca == "FORTE":
        return "MEDIO"
    return forca


def candle_key(tf_min):
    now = datetime.now(timezone.utc).timestamp() / 60.0
    return int(math.floor(now / tf_min))


# --------------------------------------------------------------------------
# CSS premium (dark / glassmorphism / neon)
# --------------------------------------------------------------------------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap');
:root{
  --buy:#00f5b0; --buy2:#00d4ff; --sell:#ff3b6b; --sell2:#ff008c; --neu:#8ea0c0;
  --glass:rgba(255,255,255,.045); --glass-bd:rgba(255,255,255,.10);
}
.stApp{background:
   radial-gradient(1200px 600px at 15% -10%, #16224a 0%, rgba(11,15,26,0) 55%),
   radial-gradient(1000px 500px at 100% 0%, #0c2f3a 0%, rgba(11,15,26,0) 50%),
   #070b16;
   color:#e9eefb; font-family:'Inter',sans-serif;}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:1.4rem;max-width:1220px}
.mono{font-family:'JetBrains Mono',monospace}
.hero{position:relative;border-radius:20px;padding:22px 26px;overflow:hidden;
   background:linear-gradient(120deg,rgba(0,212,255,.10),rgba(0,245,176,.06) 45%,rgba(255,0,140,.08));
   border:1px solid var(--glass-bd);backdrop-filter:blur(14px)}
.hero h1{margin:0;font-size:1.7rem;font-weight:800;letter-spacing:.3px;
   background:linear-gradient(90deg,#7ef7d6,#4fd2ff 60%,#c48cff);-webkit-background-clip:text;
   -webkit-text-fill-color:transparent}
.hero p{margin:.4rem 0 0;color:#aebbdb;font-size:.92rem}
.chip{display:inline-block;font-size:.68rem;font-weight:700;letter-spacing:1.5px;
   padding:3px 9px;border-radius:999px;background:rgba(0,245,176,.14);color:#68f5c8;
   border:1px solid rgba(0,245,176,.3);vertical-align:middle;margin-left:8px}
.sessbar{margin:14px 0 2px;font-size:.84rem;color:#9fb0d4}
.spill{display:inline-block;padding:3px 11px;border-radius:999px;font-size:.75rem;font-weight:700;
   margin:3px 5px 3px 0;background:var(--glass);border:1px solid var(--glass-bd)}
.spill.on{color:#7ef7d6;border-color:rgba(0,245,176,.35)}
.spill.ses{color:#8fd0ff;border-color:rgba(0,212,255,.3)}
.spill.off{color:#8ea0c0;opacity:.6}
/* cards */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;margin-top:6px}
.card{position:relative;border-radius:18px;padding:16px 18px;background:var(--glass);
   border:1px solid var(--glass-bd);backdrop-filter:blur(12px);transition:transform .18s ease}
.card:hover{transform:translateY(-3px)}
.card .pair{font-size:1.02rem;font-weight:700;color:#dfe8ff;letter-spacing:.3px}
.card .dir{font-family:'JetBrains Mono',monospace;font-size:1.7rem;font-weight:700;margin:.28rem 0 .1rem}
.card .arrow{font-size:1.25rem}
.buy .dir{color:#00f5b0;text-shadow:0 0 18px rgba(0,245,176,.55)}
.sell .dir{color:#ff3b6b;text-shadow:0 0 18px rgba(255,59,107,.5)}
.neu .dir{color:#9fb0d4}
.card.buy{box-shadow:inset 0 0 0 1px rgba(0,245,176,.25),0 8px 30px rgba(0,245,176,.06)}
.card.sell{box-shadow:inset 0 0 0 1px rgba(255,59,107,.22),0 8px 30px rgba(255,59,107,.06)}
.forca{display:flex;align-items:center;gap:8px;margin-top:6px;font-size:.74rem;
   letter-spacing:1.4px;color:#aab8d8;font-weight:700}
.bars{display:flex;gap:3px}
.bar{width:16px;height:6px;border-radius:3px;background:rgba(255,255,255,.12)}
.buy .bar.f{background:linear-gradient(90deg,#00f5b0,#00d4ff)}
.sell .bar.f{background:linear-gradient(90deg,#ff3b6b,#ff008c)}
.neu .bar.f{background:#9fb0d4}
.na .dir{color:#6b7ba0;font-size:1.05rem}
.big{border-radius:20px;padding:22px 24px}
.big .dir{font-size:2.5rem}
.big .pair{font-size:1.25rem}
.sec-title{margin:22px 0 8px;font-size:1rem;font-weight:700;color:#cdd9f7;letter-spacing:.3px}
.sec-title small{color:#8395bd;font-weight:500}
.foot{margin-top:26px;font-size:.78rem;color:#7d8cb0;line-height:1.5;
   background:var(--glass);border:1px solid var(--glass-bd);border-radius:14px;padding:12px 16px}
/* streamlit widgets */
div[role="radiogroup"]{gap:8px}
.stRadio label{background:var(--glass);border:1px solid var(--glass-bd);padding:6px 14px;border-radius:10px}
</style>
""", unsafe_allow_html=True)

# --------------------------------------------------------------------------
# Sidebar (aviso honesto, discreto)
# --------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### ⚡ Sinais IA")
    st.caption("Ferramenta **educativa** para conta demo. Sem dinheiro real, "
               "sem conexão com corretora e sem envio de ordens.")
    st.divider()
    show_closed = st.toggle("Mostrar pares fora de sessão", value=False)
    st.caption("Os sinais são a **agregação de indicadores do TradingView** "
               "(Análise Técnica). Não são previsão garantida. Em 1–15 min o preço "
               "é praticamente ruído e binárias têm payout < 100%.")

# --------------------------------------------------------------------------
# Header + controles
# --------------------------------------------------------------------------
st.markdown("""
<div class="hero">
  <h1>Sinais IA <span class="chip">TRADINGVIEW · TEMPO REAL</span></h1>
  <p>Direção e força por ativo, no timeframe escolhido — entradas na virada da vela.</p>
</div>
""", unsafe_allow_html=True)

c1, c2, c3 = st.columns([1.2, 1.4, 1])
with c1:
    tf_label = st.radio("Timeframe", ["1 min", "5 min", "15 min"], index=1, horizontal=True)
    TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]
with c2:
    names = [a["name"] for a in ASSETS]
    sel_name = st.selectbox("Ativo em destaque", names, index=0)
with c3:
    st.caption("Atualiza a cada vela e a cada ~10s.")

st_autorefresh(interval=10000, key="auto")

now = datetime.now(timezone.utc)
ck = candle_key(int(TF))
hck = candle_key(int(HIGHER[TF]))

# pares abertos agora
open_assets = [a for a in ASSETS if pair_open(a, now)]
open_fx = [a for a in open_assets if a["type"] == "fx"]

# --------------------------------------------------------------------------
# Busca de ratings (batch por screener) — timeframe atual + maior (confluência)
# --------------------------------------------------------------------------
def gather(interval_name, ckey):
    fx_syms = tuple(a["tv"] for a in ASSETS if a["type"] == "fx")
    cr_syms = tuple(a["tv"] for a in ASSETS if a["type"] == "crypto")
    data = {}
    data.update(fetch_batch("forex", interval_name, fx_syms, ckey))
    data.update(fetch_batch("crypto", interval_name, cr_syms, ckey))
    return data

cur = gather(INTERVAL_NAME[TF], ck)
hi = gather(INTERVAL_NAME[HIGHER[TF]], hck)

def signal_for(a):
    d, f = classify(cur.get(a["tv"]))
    if d is None:
        return None
    f = apply_confluence(d, f, hi.get(a["tv"]))
    return d, f

# --------------------------------------------------------------------------
# Sessões (barra)
# --------------------------------------------------------------------------
sess = active_sessions(now)
if market_open(now):
    spills = "".join(f'<span class="spill ses">🟢 {s}</span>' for s in sess)
    openp = "".join(f'<span class="spill on">{a["name"]}</span>' for a in open_fx)
    st.markdown(f'<div class="sessbar"><b>Sessões ativas (UTC):</b> {spills} &nbsp; '
                f'<b>abertos:</b> {openp or "—"}</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="sessbar">🔴 <b>Mercado forex fechado</b> (fim de semana). '
                'Apenas cripto (24/7) ativo.</div>', unsafe_allow_html=True)

# --------------------------------------------------------------------------
# Contador de vela + NOVA ENTRADA (JS client-side, suave)
# --------------------------------------------------------------------------
components.html(f"""
<div id="cd" style="font-family:'JetBrains Mono',monospace;color:#e9eefb;
     background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.10);
     border-radius:16px;padding:14px 18px;display:flex;align-items:center;gap:18px;backdrop-filter:blur(10px)">
  <div style="font-size:.72rem;letter-spacing:1.5px;color:#9fb0d4">PRÓXIMA VELA ({TF_LABEL[TF]})</div>
  <div id="clock" style="font-size:2rem;font-weight:700;letter-spacing:2px">--:--</div>
  <div id="entry"></div>
  <div style="flex:1"></div>
  <div id="progress" style="height:8px;flex:0 0 220px;border-radius:6px;background:rgba(255,255,255,.10);overflow:hidden">
     <div id="pbar" style="height:100%;width:0%;background:linear-gradient(90deg,#00f5b0,#00d4ff)"></div>
  </div>
</div>
<style>@keyframes pulse{{0%{{opacity:.35;transform:scale(.98)}}50%{{opacity:1;transform:scale(1.03)}}100%{{opacity:.35;transform:scale(.98)}}}}
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@700&display=swap');</style>
<script>
var TF={int(TF)};
function tick(){{
  var now=Date.now()/1000;
  var per=TF*60;
  var pos=now % per;
  var left=per-pos;
  var mm=Math.floor(left/60), ss=Math.floor(left%60);
  document.getElementById('clock').textContent=(mm<10?'0':'')+mm+':'+(ss<10?'0':'')+ss;
  document.getElementById('pbar').style.width=((pos/per)*100).toFixed(1)+'%';
  var e=document.getElementById('entry');
  if(pos<15){{
    e.innerHTML='<span style="font-family:Inter,sans-serif;font-weight:800;letter-spacing:1px;'
      +'padding:6px 14px;border-radius:999px;color:#04120d;background:linear-gradient(90deg,#00f5b0,#00d4ff);'
      +'box-shadow:0 0 22px rgba(0,245,176,.6);animation:pulse 1s infinite">● NOVA ENTRADA</span>';
  }} else {{ e.innerHTML=''; }}
}}
tick(); setInterval(tick,1000);
</script>
""", height=74)

# --------------------------------------------------------------------------
# Card grande do ativo em destaque + gráfico
# --------------------------------------------------------------------------
sel = next(a for a in ASSETS if a["name"] == sel_name)

def card_html(a, sig, big=False):
    cls = "big " if big else ""
    if sig is None:
        return (f'<div class="card na {cls}"><div class="pair">{a["name"]}</div>'
                f'<div class="dir">— sinal indisponível</div>'
                f'<div class="forca">sem conexão com o TradingView agora</div></div>')
    d, f = sig
    dircls = "buy" if d == "COMPRA" else ("sell" if d == "VENDA" else "neu")
    arrow = "▲" if d == "COMPRA" else ("▼" if d == "VENDA" else "◆")
    nfill = {"FRACO": 1, "MEDIO": 2, "FORTE": 3}.get(f, 0)
    bars = "".join(f'<span class="bar {"f" if i < nfill else ""}"></span>' for i in range(3))
    flabel = {"FRACO": "FRACO", "MEDIO": "MÉDIO", "FORTE": "FORTE"}.get(f, f)
    return (f'<div class="card {dircls} {cls}"><div class="pair">{a["name"]}</div>'
            f'<div class="dir"><span class="arrow">{arrow}</span> {d}</div>'
            f'<div class="forca"><div class="bars">{bars}</div> FORÇA {flabel}</div></div>')

colA, colB = st.columns([1, 1.35])
with colA:
    st.markdown(card_html(sel, signal_for(sel), big=True), unsafe_allow_html=True)
    st.caption("Sinal do timeframe selecionado, reforçado pela confluência com o timeframe maior.")
with colB:
    components.html(f"""
    <div class="tradingview-widget-container" style="height:340px">
      <div class="tradingview-widget-container__widget" style="height:100%"></div>
      <script type="text/javascript" async
        src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js">
      {{"autosize":true,"symbol":"{sel['tv']}","interval":"{TF}","timezone":"Etc/UTC",
       "theme":"dark","style":"1","locale":"br","hide_side_toolbar":true,
       "allow_symbol_change":false,"backgroundColor":"#0b1020"}}
      </script>
    </div>""", height=350)

# --------------------------------------------------------------------------
# Visão geral — todos os pares abertos
# --------------------------------------------------------------------------
st.markdown(f'<div class="sec-title">Visão geral — {TF_LABEL[TF]} '
            f'<small>(pares abertos agora)</small></div>', unsafe_allow_html=True)
show_list = open_assets if not show_closed else ASSETS
cards = "".join(card_html(a, signal_for(a)) for a in show_list)
st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)

# --------------------------------------------------------------------------
# Rodapé honesto (discreto)
# --------------------------------------------------------------------------
st.markdown("""
<div class="foot">
  <b>Aviso:</b> ferramenta educativa para conta demo. Os sinais são a agregação de
  indicadores de Análise Técnica do TradingView — <b>não são previsão garantida</b> nem
  recomendação financeira. Movimento de 1–15 min é dominado por ruído; opções binárias têm
  payout &lt; 100% (é preciso acertar ~53–56% só para empatar). Dados © TradingView.
</div>
""", unsafe_allow_html=True)
