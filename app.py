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
import threading
import time
from collections import deque
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

# st.components.v1.html está depreciado e já passou da data de remoção
# (01/06/2026). Usa st.iframe onde existir, mantendo o fallback para rodar
# em instalações locais com Streamlit antigo.
def html_box(code, height=0, **kw):
    # height="content" mede alto demais e abre um vão; altura fixa fica exata.
    fn = getattr(st, "iframe", None)
    if fn is not None:
        return fn(code, height=height, **kw)
    return components.html(code, height=height, **kw)

# ============================== MARCA ==============================
# Trocar o nome é uma linha só. O símbolo é SVG inline: escala sem perder
# nitidez e acompanha a paleta do app.
BRAND = "Kairo"
BRAND_SUB = "signal scanner"
# Monograma: a haste do "K" é o corpo de uma vela (com pavio em cima e embaixo);
# os braços abrem como uma seta de entrada e terminam no ponto do sinal. Lê como
# K e como marcação de entrada ao mesmo tempo, e continua nítido em 20px.
LOGO_SVG = """<svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="kg" x1="8" y1="6" x2="33" y2="34" gradientUnits="userSpaceOnUse">
      <stop stop-color="#3BF0B4"/><stop offset="1" stop-color="#0C8FD4"/>
    </linearGradient>
  </defs>
  <rect x="1" y="1" width="38" height="38" rx="11.5" fill="#0A1018"
        stroke="url(#kg)" stroke-width="1.7"/>
  <path d="M13.4 7.5 V32.5" stroke="url(#kg)" stroke-width="1.5"
        stroke-linecap="round" opacity=".5"/>
  <rect x="10.9" y="11.5" width="5" height="17" rx="2.5" fill="url(#kg)"/>
  <path d="M18.6 20 L26.4 12.6" stroke="url(#kg)" stroke-width="3.4" stroke-linecap="round"/>
  <path d="M18.6 20 L26.4 27.4" stroke="url(#kg)" stroke-width="3.4" stroke-linecap="round"/>
  <circle cx="27.4" cy="11.8" r="3.1" fill="#0A1018"/>
  <circle cx="27.4" cy="11.8" r="2.5" fill="#3BF0B4"/>
</svg>"""

socket.setdefaulttimeout(8)
st.set_page_config(page_title=BRAND, page_icon="⚡", layout="wide",
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
TF_PERIOD = {"1m": "7d", "5m": "1mo", "15m": "1mo"}      # janela grande: backtest
TF_PERIOD_LIVE = {"1m": "1d", "5m": "2d", "15m": "5d"}   # janela curta: sinal ao vivo
# Rótulo curto para os chips do seletor. Encurtar por regra automática deixava
# E, G e I todos como "Fade" — aqui cada uma mantém o que a distingue.
CHIP = {
    "A · Tendência": "A · Tendência", "B · Reversão": "B · Reversão",
    "C · Rompimento": "C · Rompimento", "D · Confluência multi-TF": "D · Multi-TF",
    "E · Fade de rompimento": "E · Fade romp.", "F · Exaustão": "F · Exaustão",
    "G · Fade vela extrema": "G · Vela extrema", "H · Z-score reversão": "H · Z-score",
    "I · Fade extremo lateral": "I · Lateral", "J · Z-score forte": "J · Z-score forte",
    "K · Reversão dupla": "K · Rev. dupla",
}
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


# ====================== FONTE DE DADOS: TWELVE DATA ======================
# Chave lida dos secrets do Streamlit (nunca fica no código).
def _td_key():
    try:
        k = st.secrets.get("TWELVE_DATA_KEY", "")
    except Exception:
        k = ""
    return k or os.environ.get("TWELVE_DATA_KEY", "")


TD_KEY = _td_key()
TD_INTERVAL = {"1m": "1min", "5m": "5min", "15m": "15min"}


def _td_to_df(values):
    """Lista de velas da Twelve Data -> DataFrame OHLC (UTC, crescente)."""
    rows = []
    for v in values:
        try:
            rows.append((pd.Timestamp(v["datetime"]), float(v["open"]), float(v["high"]),
                         float(v["low"]), float(v["close"])))
        except Exception:
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "Open", "High", "Low", "Close"]).set_index("dt")
    df = df.sort_index()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    return df


# --- Orçamento de créditos (plano grátis: 8 por minuto, 800 por dia) ---
# Módulo-global: compartilhado por TODAS as abas/sessões do mesmo servidor.
# A janela da Twelve Data não começa junto com a nossa: se contarmos 60 s exatos,
# um scan no fim de um minuto + outro no início do seguinte ainda somam >8 para eles.
# Por isso a janela local é de 75 s e o teto é 7 (margem de 1 crédito).
TD_LIMIT_MIN = 7
TD_WINDOW_S = 75
_td_lock = threading.Lock()
_td_spent = deque()          # timestamps dos créditos gastos (janela de 60 s)
_td_day = [0, None]          # [créditos no dia, data UTC]


def td_budget(n):
    """Reserva n créditos se couber na janela de 60 s. True = pode buscar."""
    now = time.time()
    today = datetime.now(timezone.utc).date()
    with _td_lock:
        while _td_spent and now - _td_spent[0] > TD_WINDOW_S:
            _td_spent.popleft()
        if _td_day[1] != today:
            _td_day[0], _td_day[1] = 0, today
        if len(_td_spent) + n > TD_LIMIT_MIN:
            return False
        for _ in range(n):
            _td_spent.append(now)
        _td_day[0] += n
        return True


def td_status():
    now = time.time()
    with _td_lock:
        while _td_spent and now - _td_spent[0] > TD_WINDOW_S:
            _td_spent.popleft()
        return len(_td_spent), TD_LIMIT_MIN, _td_day[0]


@st.cache_data(ttl=900, show_spinner=False)
def td_fetch_cached(symbols_key, interval, candle, outputsize=250):
    """
    Cache CROSS-SESSION: várias abas abertas compartilham a mesma busca,
    então N abas continuam gastando os mesmos créditos de 1 busca por vela.
    `candle` entra na chave só para invalidar a cada vela nova.
    """
    return td_fetch(list(symbols_key), interval, outputsize)


def td_fetch(symbols, interval, outputsize=250):
    """
    Busca vários símbolos em UMA requisição (1 crédito por símbolo).
    Retorna {símbolo: DataFrame} — só os que vieram OK.
    """
    if not TD_KEY or not symbols:
        return {}
    if not td_budget(len(symbols)):
        st.session_state["td_erro"] = (
            f"orçamento de {TD_LIMIT_MIN} créditos/min já usado neste minuto — "
            "usando yfinance nesta vela")
        return {}
    import requests
    try:
        r = requests.get("https://api.twelvedata.com/time_series",
                         params={"symbol": ",".join(symbols),
                                 "interval": TD_INTERVAL[interval],
                                 "outputsize": outputsize, "timezone": "UTC",
                                 "apikey": TD_KEY, "format": "JSON"}, timeout=9)
        j = r.json()
    except Exception:
        return {}
    out = {}
    if isinstance(j, dict) and "values" in j and len(symbols) == 1:      # resposta simples
        d = _td_to_df(j["values"])
        if d is not None:
            out[symbols[0]] = d
        return out
    if isinstance(j, dict) and j.get("code") in (429, 401, 403):         # limite/credencial
        st.session_state["td_erro"] = j.get("message", "limite atingido")
        return {}
    if isinstance(j, dict):                                             # resposta múltipla
        for sym, blk in j.items():
            if isinstance(blk, dict) and blk.get("status") == "ok" and "values" in blk:
                d = _td_to_df(blk["values"])
                if d is not None:
                    out[sym] = d
    return out


# ====================== FONTE DE DADOS: CRIPTO (EXCHANGES) ======================
# APIs públicas, sem chave e sem limite prático. O yfinance entrega cripto com
# vários minutos de atraso; as exchanges entregam a vela fechada em segundos.
# Cadeia de tentativa: Binance -> Coinbase -> yfinance.
CRYPTO_SYMS = {
    "BTC/USD": {"binance": "BTCUSDT", "coinbase": "BTC-USD"},
    "ETH/USD": {"binance": "ETHUSDT", "coinbase": "ETH-USD"},
}
BINANCE_IV = {"1m": "1m", "5m": "5m", "15m": "15m"}
COINBASE_GRAN = {"1m": 60, "5m": 300, "15m": 900}


def _ohlc_df(rows):
    """rows = [(ts_utc_naive, o, h, l, c)] -> DataFrame ordenado."""
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "Open", "High", "Low", "Close"]).set_index("dt")
    return df.sort_index()


def _binance(sym, interval, limit=250):
    import requests
    r = requests.get("https://api.binance.com/api/v3/klines",
                     params={"symbol": sym, "interval": BINANCE_IV[interval], "limit": limit},
                     timeout=6)
    j = r.json()
    if not isinstance(j, list):                      # ex.: 451 em região bloqueada
        raise RuntimeError(str(j)[:120])
    return _ohlc_df([(pd.Timestamp(k[0], unit="ms"), float(k[1]), float(k[2]),
                      float(k[3]), float(k[4])) for k in j])


def _coinbase(sym, interval, limit=250):
    import requests
    g = COINBASE_GRAN[interval]
    fim = datetime.now(timezone.utc)
    ini = fim - timedelta(seconds=g * limit)
    r = requests.get(f"https://api.exchange.coinbase.com/products/{sym}/candles",
                     params={"granularity": g, "start": ini.isoformat(),
                             "end": fim.isoformat()},
                     headers={"User-Agent": "sinais-ia"}, timeout=6)
    j = r.json()
    if not isinstance(j, list):
        raise RuntimeError(str(j)[:120])
    # Coinbase: [time, low, high, open, close, volume]
    return _ohlc_df([(pd.Timestamp(k[0], unit="s"), float(k[3]), float(k[2]),
                      float(k[1]), float(k[4])) for k in j])


@st.cache_data(ttl=900, show_spinner=False)
def crypto_fetch(nome, interval, candle):
    """Retorna (DataFrame, fonte) ou (None, None). `candle` só invalida o cache."""
    m = CRYPTO_SYMS.get(nome)
    if not m:
        return None, None
    for fonte, fn, sym in (("binance", _binance, m["binance"]),
                           ("coinbase", _coinbase, m["coinbase"])):
        try:
            d = fn(sym, interval)
            if d is not None and len(d) >= 60:
                return d, fonte
        except Exception:
            continue
    return None, None


def _dl(yf_symbol, interval, period):
    try:
        import yfinance as yf
        df = yf.download(yf_symbol, interval=interval, period=period,
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


def get_data_live(assets, interval, minutes):
    """
    Janela CURTA — caminho crítico do sinal. Refeita a cada vela.
    Forex vem da Twelve Data (1 requisição para todos os pares) quando há chave;
    o que falhar cai para o yfinance. Cripto continua no yfinance.
    """
    cache = st.session_state.setdefault("ohlc_live", {})
    ck = candle_key(minutes)
    todo = [a for a in assets if (a["yf"], interval, ck) not in cache]
    took, fontes = 0.0, st.session_state.setdefault("fontes", {})
    if todo:
        t0 = time.perf_counter()
        pend = list(todo)
        # 1) Twelve Data para os pares de forex
        if TD_KEY:
            fx = [a for a in pend if a["type"] == "fx"]
            if fx:
                st.session_state.pop("td_erro", None)      # erro é por vela, não permanente
                syms = tuple(sorted(a["name"] for a in fx))
                got = td_fetch_cached(syms, interval, ck)
                for a in fx:
                    d = got.get(a["name"])
                    if d is not None and len(d):
                        cache[(a["yf"], interval, ck)] = d
                        fontes[a["name"]] = "twelvedata"
                        pend.remove(a)
        # 2) Cripto direto da exchange (rápido, sem chave)
        for a in [x for x in pend if x["type"] == "crypto"]:
            d, fonte = crypto_fetch(a["name"], interval, ck)
            if d is not None and len(d):
                cache[(a["yf"], interval, ck)] = d
                fontes[a["name"]] = fonte
                pend.remove(a)
        # 3) yfinance para o resto (falhas das fontes acima)
        if pend:
            with ThreadPoolExecutor(max_workers=min(10, len(pend))) as ex:
                futs = {ex.submit(_dl, a["yf"], interval, TF_PERIOD_LIVE[interval]): a for a in pend}
                for f in as_completed(futs):
                    a = futs[f]
                    cache[(a["yf"], interval, ck)] = f.result()
                    fontes[a["name"]] = "yfinance"
        took = time.perf_counter() - t0
        st.session_state["last_fetch_s"] = took
        for k in [k for k in cache if k[2] < ck - 3]:
            cache.pop(k, None)
    return {a["name"]: cache.get((a["yf"], interval, ck)) for a in assets}, took


def get_data_hist(assets, interval):
    """Janela GRANDE — só para o painel de desempenho. Cache de 10 minutos."""
    cache = st.session_state.setdefault("ohlc_hist", {})
    bucket = int(datetime.now(timezone.utc).timestamp() // 600)
    todo = [a for a in assets if (a["yf"], interval, bucket) not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=min(10, len(todo))) as ex:
            futs = {ex.submit(_dl, a["yf"], interval, TF_PERIOD[interval]): a for a in todo}
            for f in as_completed(futs):
                cache[(futs[f]["yf"], interval, bucket)] = f.result()
        for k in [k for k in cache if k[2] < bucket - 1]:
            cache.pop(k, None)
    return {a["name"]: cache.get((a["yf"], interval, bucket)) for a in assets}


# ============================== DESIGN SYSTEM ==============================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600;700&display=swap');
:root{
  --bg:#07090E; --surf:#0D111A; --surf2:#121724; --line:rgba(255,255,255,.06);
  --line2:rgba(255,255,255,.11); --ink:#E9EDF5; --ink2:#B6C0D4; --mut:#6F7B93;
  --buy:#00C88A; --buy-dim:rgba(0,200,138,.10); --sell:#FF4A63; --sell-dim:rgba(255,74,99,.09);
  --warn:#D9A441; --r:12px; --r2:16px;
  /* ---- ESCALA DE ESPAÇAMENTO ----
     Antes cada cartão tinha o seu: paddings de 11/13/14/16/18/22px e margens de
     8/16/18/30/34/38. Agora tudo sai daqui, então nada mais destoa por acidente. */
  --pad-card:16px 20px;   /* preenchimento interno de TODO cartão */
  --gap-bloco:14px;       /* respiro entre um bloco e o seguinte */
  --gap-curto:6px;        /* entre um bloco e sua legenda/complemento */
  --gap-secao:26px;       /* antes de um título de seção */
}
.stApp{background:var(--bg);color:var(--ink);
  font-family:'Inter',-apple-system,sans-serif;-webkit-font-smoothing:antialiased;}
#MainMenu,footer,header{visibility:hidden}
.block-container{padding-top:1.5rem;padding-bottom:4rem;max-width:1180px}
.mono{font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums}
hr{border-color:var(--line)}

/* ---------- HEADER ---------- */
.hdr{display:flex;align-items:center;justify-content:space-between;gap:22px;
  background:linear-gradient(180deg,rgba(255,255,255,.028),transparent 60%),var(--surf);
  border:1px solid var(--line);border-radius:var(--r2);
  padding:var(--pad-card);margin-bottom:var(--gap-bloco)}
.hdr-l{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
/* ---- marca ---- */
.brand{display:flex;align-items:center;gap:11px}
.brand svg{width:32px;height:32px;flex:none;border-radius:10px;
  box-shadow:0 3px 14px -4px rgba(0,200,138,.55)}
.wm{display:flex;flex-direction:column;line-height:1.05}
.wm b{font-size:1.06rem;font-weight:600;letter-spacing:-.025em;color:#fff}
.wm span{font-size:.56rem;letter-spacing:.2em;text-transform:uppercase;
  color:var(--mut);font-weight:600;margin-top:3px}
.vbar{width:1px;height:30px;background:var(--line2);flex:none}
/* pastilha Ao vivo / Pausado — é um link, não um widget do Streamlit */
.livebtn{display:inline-flex;align-items:center;gap:7px;flex:none;text-decoration:none!important;
  font-size:.64rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding:7px 13px;border-radius:999px;border:1px solid var(--line2);
  color:var(--mut);background:var(--surf2);transition:filter .15s,border-color .15s}
.livebtn:hover{filter:brightness(1.25)}
.livebtn i{width:7px;height:7px;border-radius:50%;background:var(--mut);font-style:normal}
.livebtn.on{color:var(--buy);border-color:rgba(0,200,138,.34);background:rgba(0,200,138,.10)}
.livebtn.on i{background:var(--buy);box-shadow:0 0 0 3px rgba(0,200,138,.16);
  animation:lpulse 1.8s ease-in-out infinite}
@keyframes lpulse{0%,100%{opacity:1}50%{opacity:.4}}
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
  padding:6px 9px;border-radius:7px;font-weight:600;font-size:.78rem;
  transition:background .15s;cursor:pointer;white-space:nowrap;flex:0 0 auto}
/* NÃO esconder a bolinha do rádio com `label div:first-child{display:none}`:
   já tentei e o seletor pega também o container do texto, deixando o rádio
   visualmente vazio. A folga veio de alargar a coluna, não de cortar elemento. */
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
.sect{margin:var(--gap-secao) 0 10px;font-size:.6rem;font-weight:600;letter-spacing:.16em;color:var(--mut);
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
/* amostra pequena: número apagado de propósito, para não competir com os que
   têm base estatística. 70% em 30 operações não é melhor que 54% em 4000. */
.wr.faint{color:var(--mut);font-weight:500}
.v-faint{background:rgba(255,255,255,.045);color:var(--mut)}
.tbl tr:has(.v-faint) .nm{color:var(--ink2);font-weight:500}

/* ---------- CARTÕES DE NÚMERO (topo do Histórico) ---------- */
.statrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;
  margin:var(--gap-bloco) 0 var(--gap-curto)}
.stat{background:var(--surf);border:1px solid var(--line);border-radius:var(--r);
  padding:var(--pad-card);display:flex;flex-direction:column;gap:5px;min-width:0}
.stat .k{font-size:.58rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mut);font-weight:600}
.stat .v{font-family:'IBM Plex Mono',monospace;font-size:1.6rem;font-weight:600;
  color:var(--ink);line-height:1.1;font-variant-numeric:tabular-nums}
.stat .x{font-size:.66rem;color:var(--mut);line-height:1.35}

/* ---------- BARRA DE RESUMO (topo do Desempenho) ---------- */
.sumbar{display:flex;align-items:center;justify-content:space-between;gap:24px;flex-wrap:wrap;
  background:var(--surf);border:1px solid var(--line);border-left:3px solid var(--line2);
  border-radius:var(--r);padding:var(--pad-card);margin:var(--gap-bloco) 0 var(--gap-curto)}
.sumbar.good{border-left-color:var(--buy)}
.sumbar.mid{border-left-color:var(--warn)}
.sumbar .s-main{font-size:.92rem;color:var(--ink2);line-height:1.5}
.sumbar .big{font-family:'IBM Plex Mono',monospace;font-size:1.5rem;font-weight:700;
  margin-right:7px;vertical-align:-2px}
.sumbar .s-side{display:flex;gap:26px;flex-wrap:wrap}
.sumbar .s-side span{display:flex;flex-direction:column;gap:3px;font-size:.62rem;
  letter-spacing:.1em;text-transform:uppercase;color:var(--mut);font-weight:600}
.sumbar .s-side i{font-family:'IBM Plex Mono',monospace;font-style:normal;font-size:1.02rem;
  color:var(--ink);font-weight:600;letter-spacing:0;text-transform:none}
.tagmini{font-size:.54rem;font-weight:600;letter-spacing:.06em;padding:2px 6px;border-radius:5px;
  background:var(--surf2);color:var(--mut);border:1px solid var(--line);margin-left:7px}
.note{margin-top:var(--gap-bloco);font-size:.72rem;color:var(--mut);border-left:2px solid var(--line2);
  padding:2px 0 2px 14px;line-height:1.65}
.note b{color:var(--ink2);font-weight:600}
.foot{margin-top:var(--gap-secao);padding-top:18px;border-top:1px solid var(--line);text-align:center;
  font-size:.64rem;color:#4E5872}
div[data-testid="stMetric"]{background:var(--surf);border:1px solid var(--line);
  border-radius:var(--r);padding:var(--pad-card)}
div[data-testid="stMetricLabel"] p{font-size:.58rem!important;letter-spacing:.14em;
  text-transform:uppercase;color:var(--mut)!important;font-weight:600!important}
div[data-testid="stMetricValue"]{font-family:'IBM Plex Mono',monospace;font-size:1.3rem;
  font-variant-numeric:tabular-nums}
/* ---------- janela de entrada / alertas ---------- */
.win{display:flex;align-items:center;gap:10px;border-radius:10px;padding:12px 20px;
  font-size:.82rem;color:var(--ink2);margin:var(--gap-bloco) 0 var(--gap-curto);border:1px solid var(--line)}
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
/* ---------- DIAGNÓSTICO DE DADOS (chips, não log de terminal) ---------- */
.diag{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:var(--gap-bloco) 0 var(--gap-curto)}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:.68rem;font-weight:500;
  color:var(--ink2);background:var(--surf2);border:1px solid var(--line);
  border-radius:999px;padding:4px 11px;line-height:1.3}
.chip .ck{color:var(--mut);font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;
  font-weight:600}
.chip .cv{font-family:'IBM Plex Mono',monospace;font-weight:600;color:var(--ink);
  font-variant-numeric:tabular-nums}
.chip.warn{border-color:rgba(217,164,65,.35);background:rgba(217,164,65,.08)}
.chip.warn .cv{color:var(--warn)}
/* remove o vão que o iframe do contador cria */
/* ---------- SCROLL E IFRAMES ----------
   O Streamlit renderiza cada widget num [data-testid="stElementContainer"].
   (O testid antigo era "element-container"; a regra que usava esse nome estava
   morta há tempos — por isso os ajustes de margem não faziam efeito.)

   Dois problemas reais que quebravam a rolagem:
   1. O componente de auto-refresh é um iframe de altura 0, mas o container dele
      ocupa 26px de largura total e captura a roda do mouse.
   2. O iframe do contador (58px, largura total) também engole a roda: passando
      o cursor sobre ele, a página simplesmente não rola. */
[data-testid="stElementContainer"]:has(> iframe[title^="streamlit_autorefresh"]){
  display:none !important;
}
[data-testid="stElementContainer"]:has(.wheel-pass){display:none !important}
[data-testid="stElementContainer"]:has(.wheel-pass) + [data-testid="stElementContainer"] iframe{
  pointer-events:none;              /* contador é só visual: deixa a roda passar */
}
[data-testid="stElementContainer"]:has(> iframe[title="st.iframe"]){margin:-2px 0 -6px}
[data-testid="stElementContainer"] iframe{display:block}

/* Abas fixas: em tabelas longas as abas saíam da tela e não dava para voltar
   sem rolar tudo de novo. */
/* A lista de abas vem com `overflow:auto hidden` do Streamlit (rolagem
   horizontal). Isso CORTA a pílula na vertical e a linha do sticky passava
   rente à base dela — daí a impressão de sobreposição. Com 3 abas não há o que
   rolar, então libero o overflow e dou respiro entre a pílula e a linha. */
[data-testid="stTabs"] [role="tablist"]{
  position:sticky;top:0;z-index:30;background:var(--bg);
  overflow:visible!important;
  gap:4px;padding:8px 0 12px;
  box-shadow:0 1px 0 var(--line), 0 10px 14px -12px rgba(0,0,0,.9)}
[data-testid="stTabs"] [role="tabpanel"]{padding-top:10px}

/* NOTA: já tentei `overflow-anchor:none` aqui e foi um erro. A ancoragem é
   justamente o mecanismo do navegador que SEGURA a posição quando algo acima
   muda de altura — desligá-la expõe cada deslocamento em vez de escondê-lo.
   O caminho certo é não deixar a altura mudar. Por isso o min-height abaixo:
   a cada rerun o Streamlit recria o iframe do contador, e enquanto ele não
   carrega o container pode ficar com altura 0, empurrando tudo que vem depois. */
[data-testid="stElementContainer"]:has(> iframe[title="st.iframe"]){min-height:58px}

/* ---------- BARRA DE CONTROLES ----------
   O <div class="ctrlbar"> não envolve as colunas (o Streamlit as renderiza como
   irmãs), então o cartão é aplicado no próprio bloco horizontal. Ele é
   identificado por conter os rótulos .lbl, que só existem aqui. */
.ctrlbar{display:none}
[data-testid="stHorizontalBlock"]:has(.lbl){
  background:linear-gradient(180deg,rgba(255,255,255,.022),transparent 70%),var(--surf);
  border:1px solid var(--line);border-radius:var(--r2);
  padding:14px 20px 14px;margin-bottom:14px;align-items:flex-start;gap:22px}
[data-testid="stHorizontalBlock"]:has(.lbl) [data-testid="stElementContainer"]{margin-bottom:0}
/* com 4+ estratégias a caixa do meio cresce; ancorando tudo no topo, as colunas
   vizinhas não são empurradas para o meio da altura */
[data-testid="stHorizontalBlock"]:has(.lbl) [data-testid="stSelectSlider"]{padding-top:10px}
.stMultiSelect [data-baseweb="select"]>div{flex-wrap:wrap}
@media(max-width:900px){.hero{grid-template-columns:1fr}.hero-side{border-left:0;border-top:1px solid var(--line)}}

/* ---------- ACABAMENTO ---------- */
/* Abas: pílulas em vez de sublinhado solto; a ativa ganha corpo.
   (O gap e o padding da lista ficam SÓ na regra do sticky, mais acima. Havia
   uma segunda declaração aqui que sobrescrevia o padding e colava a pílula na
   linha de baixo — era a "sobreposição" que aparecia na tela.) */
[data-testid="stTabs"] [role="tab"]{border-radius:9px;padding:7px 15px!important;
  color:var(--mut);font-weight:600;font-size:.83rem;transition:color .15s,background .15s}
[data-testid="stTabs"] [role="tab"]:hover{color:var(--ink2);background:rgba(255,255,255,.035)}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{color:var(--ink);background:var(--surf2)}
[data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"]{display:none!important}

/* Tabelas: cabeçalho fixo ao rolar, zebra sutil e linha destacada no hover. */
.tbl{border-collapse:separate;border-spacing:0}
.tbl th{position:sticky;top:60px;z-index:9;background:var(--bg);
  backdrop-filter:blur(6px);box-shadow:0 1px 0 var(--line)}
.tbl tbody tr:hover td,.tbl tr:hover td{background:rgba(255,255,255,.022)}
.tbl tr.on:hover td{background:rgba(0,200,138,.06)}

/* Botões: acabamento consistente com os cartões. */
.stButton>button{background:var(--surf2);border:1px solid var(--line2);border-radius:10px;
  color:var(--ink);font-weight:600;font-size:.8rem;padding:9px 16px;transition:.15s}
.stButton>button:hover{border-color:var(--buy);color:var(--buy);
  background:rgba(0,200,138,.08)}
.stDownloadButton>button{background:var(--surf2);border:1px solid var(--line2);
  border-radius:10px;color:var(--ink2);font-weight:600;font-size:.78rem}

/* Expander com a mesma pele dos cartões. */
div[data-testid="stExpander"] details{background:var(--surf);border:1px solid var(--line)!important;
  border-radius:var(--r2)!important}
div[data-testid="stExpander"] summary{font-size:.82rem;font-weight:600;color:var(--ink2)}
div[data-testid="stExpander"] summary:hover{color:var(--ink)}

/* Rodapé discreto. */
.foot{margin:var(--gap-secao) 0 8px;text-align:center;font-size:.66rem;color:var(--mut);
  border-top:1px solid var(--line);padding-top:16px}

/* Estado vazio do scanner: compacto, é o estado mais comum do app. */
.empty{display:flex;align-items:center;gap:18px;background:var(--surf);
  border:1px solid var(--line);border-radius:var(--r2);padding:var(--pad-card)}
.empty .e-ico{width:42px;height:42px;flex:none;border-radius:12px;display:flex;
  align-items:center;justify-content:center;background:var(--surf2);
  border:1px solid var(--line);color:var(--mut)}
.empty .e-ico svg{width:20px;height:20px}
.empty .e-txt{display:flex;flex-direction:column;gap:4px;min-width:0}
.empty .e-txt b{font-size:.95rem;font-weight:600;color:var(--ink2)}
.empty .e-txt span{font-size:.72rem;color:var(--mut);line-height:1.55}
.empty .e-side{margin-left:auto;text-align:right;display:flex;flex-direction:column;gap:3px;flex:none}
.empty .e-side .k{font-size:.56rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mut);font-weight:600}
.empty .e-side .v{font-size:1.15rem;font-weight:600;color:var(--ink)}

/* ---------- RITMO VERTICAL ----------
   Havia vãos de 40-60px entre blocos que não separavam nada: o cartão de
   controles, o expander e o contador são um grupo só. */
div[data-testid="stExpander"]{margin:2px 0 var(--gap-curto)}
[data-testid="stTabs"]{margin-top:10px}
.block-container{padding-top:1.1rem}
/* Legendas do Streamlit (st.caption) apareciam ora coladas, ora com 20px de
   distância do bloco acima. Passam a usar o mesmo respiro curto de todo o app. */
[data-testid="stCaptionContainer"]{margin-top:var(--gap-curto)!important;
  margin-bottom:var(--gap-bloco)!important}
[data-testid="stCaptionContainer"] p{font-size:.72rem!important;color:var(--mut)!important;
  line-height:1.5!important;margin:0!important}
/* Tabela é um bloco como qualquer outro: mesma distância do que vem antes/depois */
.tbl{margin-bottom:var(--gap-bloco)}
/* Dois títulos de seção seguidos não precisam do respiro cheio duas vezes */
.sect:first-child{margin-top:var(--gap-curto)}
@media(max-width:820px){
  .empty{flex-wrap:wrap}.empty .e-side{margin-left:0}
  .sumbar .s-side{gap:16px}
  .statrow{grid-template-columns:repeat(2,1fr)}
}

/* Resultado no Histórico: bolinha colorida antes do texto, para varrer a coluna
   de relance sem ler palavra por palavra. */
.verd::before{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;
  margin-right:5px;vertical-align:1px;background:currentColor}
.tbl td .sc{background:var(--surf2);border:1px solid var(--line)}
/* colunas numéricas alinhadas pela direita deixam a leitura vertical mais limpa */
.tbl td.mono,.tbl td.n.mono{text-align:right;font-variant-numeric:tabular-nums}
</style>
""", unsafe_allow_html=True)

# ============================== CONTROLES (no corpo da página) ==============================
# ---- estado "Ao vivo" pela URL, não por widget ----
# O widget teria de morar na grade de colunas do Streamlit, que não alinha bem
# num cabeçalho denso. Como link com query param, o controle vira HTML meu: fica
# no cabeçalho, alinhado à direita, e o estado sobrevive a recarregar a página.
auto_on = st.query_params.get("live", "1") != "0"

topbar_slot = st.empty()          # a barra de status é preenchida depois (precisa dos dados)
st.markdown('<div class="ctrlbar">', unsafe_allow_html=True)
# Três colunas. A chave "Ao vivo" saiu daqui: ela é um link no cabeçalho, HTML
# meu, onde o alinhamento é controlável. Tentar encaixá-la como quarta coluna
# quebrou a linha três vezes seguidas.
# Alinhamento pelo TOPO, não pela base. Com 4+ estratégias a caixa do meio
# cresce para baixo; com alinhamento pela base ela empurrava os rótulos das
# colunas vizinhas para o meio da altura, desalinhando a barra inteira.
cc1, cc2, cc3 = st.columns([1.35, 2.35, 1.15], vertical_alignment="top")
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
                                format_func=lambda s: CHIP.get(s, s),
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
        payout_lbl = st.radio("Payout padrão da corretora", ["80%", "90%"], index=0,
                              horizontal=True)
        st.caption("A pastilha *Ao vivo / Pausado* fica no cabeçalho, à direita.")
        every = st.slider("Intervalo (s)", 10, 60, 15, step=5, disabled=not auto_on)
    st.markdown("**Payout por ativo** — o breakeven muda com o payout, então vale "
                "conferir o de cada par na sua corretora. Em branco = usa o padrão.")
    pc1, pc2 = st.columns(2)
    _pay_ovr = {}
    for i, a in enumerate(ASSETS):
        with (pc1 if i % 2 == 0 else pc2):
            v = st.number_input(a["name"], min_value=0, max_value=100, step=1, value=0,
                                key=f"pay_{a['name']}",
                                help="0 = usar o payout padrão")
            if v:
                _pay_ovr[a["name"]] = v / 100.0
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
PAY_OVR = _pay_ovr


def payout_de(nome):
    """Payout do ativo (o específico, se informado; senão o padrão)."""
    return PAY_OVR.get(nome, PAYOUT)

now = datetime.now(timezone.utc)
interval, minutes = TF_YF[TF], int(TF)

# ---- janela de entrada: o backtest assume entrada na ABERTURA da vela ----
ENTRY_WINDOW = 20                                   # segundos válidos após a virada
_per = minutes * 60
_age = now.timestamp() % _per                       # segundos decorridos da vela atual
secs_to_next = _per - _age
window_open = _age <= ENTRY_WINDOW

# Refresh mirando a virada. O alvo é secs_to_next + 0,4s: acorda logo depois de a
# vela fechar. O piso de 1,5s evita um laço de reruns colados; o teto `every` é o
# ritmo normal no meio da vela.
if auto_on:
    alvo = max(1.5, min(every, secs_to_next + 0.4))
    st_autorefresh(interval=int(alvo * 1000), key="auto")

# Este rerun é o PRIMEIRO desta vela? É a única definição correta de "virada":
# um carregamento manual dentro dos primeiros 20s não mede latência nenhuma.
_ck_now = candle_key(minutes)
_ck_ant = st.session_state.get("ultimo_ck")
primeiro_da_vela = (_ck_ant is not None) and (_ck_ant != _ck_now)
st.session_state["ultimo_ck"] = _ck_now

open_assets = [a for a in ASSETS if pair_open(a, now)]
scan_list = ASSETS if show_closed else open_assets
t_scan0 = time.perf_counter()
data, fetch_s = get_data_live(scan_list, interval, minutes)
if not fetch_s:
    fetch_s = st.session_state.get("last_fetch_s", 0.0)


def data_diag(data_map):
    """
    Diagnóstico real de atraso.
    esperada = última vela que JÁ deveria ter fechado.
    faltando = ativos cuja vela esperada ainda não chegou (sinal seria calculado
    sobre dados vencidos).
    """
    ref = pd.Timestamp(now).tz_localize(None)
    esperada = pd.Timestamp((candle_key(minutes) - 1) * minutes * 60, unit="s")
    fontes = st.session_state.get("fontes", {})
    pior, quem, faltando = None, None, []
    por_fonte = {}                                   # fonte -> pior atraso (min)
    por_ativo = {}                                   # ativo -> atraso (min)
    for nome, df in data_map.items():
        if df is None or len(df) == 0:
            continue
        ult = df.index[-1]
        lag = (ref - ult).total_seconds() / 60.0
        por_ativo[nome] = lag
        src = fontes.get(nome, "yfinance")
        if src not in por_fonte or lag > por_fonte[src]:
            por_fonte[src] = lag
        if pior is None or lag > pior:
            pior, quem = lag, nome
        if ult < esperada:
            faltando.append(nome)
    return pior, quem, faltando, esperada, por_fonte, por_ativo


lag_min, lag_asset, sem_vela, vela_esperada, lag_fonte, lag_ativo = data_diag(data)
dados_atrasados = bool(sem_vela) or (lag_min is not None and lag_min > (2 * minutes + 1))

# Corte duro de frescor. A estratégia lê a ÚLTIMA VELA FECHADA. Se a vela que já
# deveria ter fechado ainda não chegou, o que o motor chama de "vela anterior" não
# é a que você vê no gráfico da corretora — o sinal descreve outro momento do
# mercado. Antes isso era só um aviso amarelo; agora o ativo sai da varredura.
# A condição é a mesma de `sem_vela`: última vela recebida < vela esperada.
bloqueados = set(sem_vela)

# ============================== SCANNER ==============================
agg = {}
for a in scan_list:
    if a["name"] in bloqueados:        # dado vencido: não vira entrada
        continue
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
    """
    Backtest de todas as estratégias.

    Os laços estão com o ATIVO por fora e a estratégia por dentro de propósito:
    add_indicators (EMA, RSI, MACD, Bollinger, ADX sobre ~6 mil velas) é a conta
    mais cara daqui e depende só do ativo, não da estratégia. Com o laço na ordem
    inversa ela rodava 11× por ativo — 99 vezes no total em vez de 9.
    """
    today = now.date()
    dhist = get_data_hist(scan_list, interval)      # janela grande, cache de 10 min
    out = {n: {"hoje": [0, 0], "per": [0, 0]} for n in STRATEGIES}
    for a in scan_list:
        df = dhist.get(a["name"])
        if df is None or len(df) < 80:
            continue
        d = add_indicators(df)                      # uma vez por ativo
        m = d.index.date == today                   # máscara do dia, idem
        tem_hoje = m.any()
        d_hoje = d[m] if tem_hoje else None
        for name in STRATEGIES:
            sc = score_of(name, d, interval)
            r = backtest(d, sc)
            acc = out[name]
            acc["per"][0] += r["trades"]; acc["per"][1] += r["wins"]
            if tem_hoje:
                rd = backtest(d_hoje, sc[m])
                acc["hoje"][0] += rd["trades"]; acc["hoje"][1] += rd["wins"]
    return out


def get_perf(calcular=False):
    """
    Backtest do painel Desempenho — a parte mais pesada do app: 1 mês de velas de
    todos os ativos + 11 estratégias em cima.

    Nunca roda sozinha. O Streamlit executa o corpo de TODAS as abas em cada
    rerun (a troca de aba é só CSS no navegador), então um recálculo automático
    aqui congela a aba Sinais junto — era o que acontecia a cada 10 minutos.
    Agora só recalcula quando você pede, e enquanto isso mostra o último
    resultado com a hora em que foi feito.
    """
    if calcular:
        t0 = time.perf_counter()
        st.session_state["perf_cache"] = {
            "chave": (interval, len(scan_list)), "dados": run_perf(),
            "quando": datetime.now(timezone.utc), "levou": time.perf_counter() - t0}
    return st.session_state.get("perf_cache")      # None = ainda não calculado

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
    <div class="brand">{LOGO_SVG}
      <div class="wm"><b>{BRAND}</b><span>{BRAND_SUB}</span></div></div>
    <div class="vbar"></div>
    <div class="meta"><span class="k">Status</span><span class="v">{stat}</span></div>
    <div class="meta"><span class="k">Sessões</span><span class="v">{sess}</span></div>
    <div class="meta"><span class="k">Varredura</span><span class="v">{len(scan_list)} ativos</span></div>
    <div class="meta"><span class="k">Horário de Brasília</span>
      <span class="v mono">{br(now).strftime('%H:%M:%S')}</span></div>
  </div>
  <a class="livebtn {'on' if auto_on else ''}" target="_self"
     href="?live={'0' if auto_on else '1'}"
     title="{'Pausar a atualização automática para ler as tabelas paradas'
             if auto_on else 'Retomar a atualização automática'}"><i></i>{
     'Ao vivo' if auto_on else 'Pausado'}</a>
</div>""", unsafe_allow_html=True)

# marcador invisível: o CSS usa o irmão seguinte para liberar a roda do mouse
st.markdown('<div class="wheel-pass"></div>', unsafe_allow_html=True)
html_box(f"""
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
t();setInterval(t,1000);
/* --- Preservação da rolagem entre reruns ---------------------------------
   O Streamlit rola num container interno (stMain), não na janela. A cada
   auto-refresh o conteúdo é reconstruído e a posição pode voltar ao topo ou
   pular — o que só incomoda nas abas longas (Desempenho e Histórico), porque
   na aba Sinais tudo cabe na tela. Este iframe usa srcdoc, então é MESMA
   ORIGEM do app e consegue falar com window.parent.
   Guarda a posição a cada rolagem e devolve depois do rerun, mas só quando o
   container voltou de fato para o topo — assim nunca briga com você. */
try{{
  var P=window.parent, PD=P.document, K='sinaisScrollTop';
  var mainEl=PD.querySelector('[data-testid="stMain"]');
  if(mainEl){{
    var salvo=parseInt(P.sessionStorage.getItem(K)||'0',10);
    if(salvo>0&&mainEl.scrollTop===0){{mainEl.scrollTop=salvo;}}
    if(!mainEl.__hookScroll){{
      mainEl.__hookScroll=1;
      mainEl.addEventListener('scroll',function(){{
        P.sessionStorage.setItem(K,String(Math.round(mainEl.scrollTop)));
      }},{{passive:true}});
    }}
  }}
}}catch(e){{}}
</script>""", height=58)

def _short(nm):
    return nm.split("·")[0].strip()


# ---------- persistência do histórico ----------
HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hist_signals.json")


# --- Persistência remota (opcional) ---------------------------------------
# O disco do Streamlit Cloud é EFÊMERO: todo rebuild do app zera o container e
# leva o hist_signals.json junto. Sem isso, o forward test nunca acumula amostra
# suficiente para dar veredito conclusivo. Com um token nos secrets, o histórico
# é espelhado num Gist privado e sobrevive aos reinícios.
GIST_FILE = "sinais_historico.json"


def _gh():
    """(token, gist_id) dos secrets, ou (None, None). O token nunca vai pro código."""
    def s(k):
        try:
            v = st.secrets.get(k, "")
        except Exception:
            v = ""
        return v or os.environ.get(k, "")
    return s("GITHUB_TOKEN") or None, s("GIST_ID") or None


def gist_load():
    tok, gid = _gh()
    if not (tok and gid):
        return None
    import requests
    try:
        r = requests.get(f"https://api.github.com/gists/{gid}", timeout=4,
                         headers={"Authorization": f"Bearer {tok}",
                                  "Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            st.session_state["gist_erro"] = f"HTTP {r.status_code} ao ler o Gist"
            return None
        c = r.json().get("files", {}).get(GIST_FILE, {}).get("content")
        return json.loads(c) if c else []
    except Exception as e:
        st.session_state["gist_erro"] = str(e)[:90]
        return None


_gist_err = [""]          # último erro de gravação (fora do session_state: roda em thread)


def gist_save(out):
    """Grava em background — a virada da vela não pode esperar rede."""
    tok, gid = _gh()
    if not (tok and gid):
        return False

    def _w():
        import requests
        try:
            r = requests.patch(
                f"https://api.github.com/gists/{gid}", timeout=8,
                headers={"Authorization": f"Bearer {tok}",
                         "Accept": "application/vnd.github+json"},
                json={"files": {GIST_FILE: {"content": json.dumps(out, ensure_ascii=False)}}})
            _gist_err[0] = "" if r.status_code == 200 else f"HTTP {r.status_code} ao gravar"
        except Exception as e:
            _gist_err[0] = str(e)[:90]

    threading.Thread(target=_w, daemon=True).start()
    return True


HIST_REMOTO = all(_gh())


def hist_load():
    """Carrega o histórico: Gist (se configurado) e disco local, unindo os dois."""
    if "hist" in st.session_state:
        return st.session_state["hist"]
    bruto = []
    remoto = gist_load()
    if remoto:
        bruto.extend(remoto)
    try:
        if os.path.exists(HIST_PATH):
            with open(HIST_PATH, "r", encoding="utf-8") as f:
                bruto.extend(json.load(f))
    except Exception:
        pass
    vistos, h = set(), []
    for r in bruto:                                   # de-duplica local x remoto
        try:
            r["ts"] = pd.Timestamp(r["ts"])
        except Exception:
            continue
        k = (r.get("asset"), r.get("dir"), r.get("ck"), r.get("tf"))
        if k in vistos:
            continue
        vistos.add(k)
        h.append(r)
    h.sort(key=lambda x: x["ts"])
    st.session_state["hist"] = h
    return h


def hist_save(h):
    out = [{**r, "ts": pd.Timestamp(r["ts"]).isoformat()} for r in h]
    try:
        with open(HIST_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    gist_save(out)


def hist_df(h):
    if not h:
        return pd.DataFrame()
    return pd.DataFrame([{
        "data_hora_brasilia": dhm(r["ts"]), "utc": pd.Timestamp(r["ts"]).isoformat(),
        "ativo": r["asset"], "direcao": r["dir"], "forca": FL.get(r["force"], r["force"]),
        "estrategias": "+".join(r["strats"]), "timeframe_min": r.get("tf", ""),
        "resultado": r["res"] or "aguardando",
        "atraso_min": r.get("lag", ""), "fonte": r.get("src", ""),
        "payout": r.get("payout", ""),
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
            nome = e["a"]["name"]
            hist.append({"ck": ck, "ts": start, "asset": nome, "dir": e["dir"],
                         "force": e["force"], "strats": [_short(s) for s in e["strats"]],
                         "tf": minutes, "res": None,
                         # instrumentação: permite medir depois se atraso derruba o acerto
                         "lag": (round(float(lag_ativo[nome]), 2)
                                 if nome in lag_ativo else None),
                         "src": st.session_state.get("fontes", {}).get(nome, "?"),
                         "payout": payout_de(nome)})
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
        if sem_vela:
            quais = ", ".join(sem_vela[:4]) + ("…" if len(sem_vela) > 4 else "")
            det = (f'a vela das {hm(vela_esperada)} ainda não chegou para {len(sem_vela)} ativo(s) '
                   f'({quais}) — <b>bloqueados nesta vela</b>, não geram entrada')
        else:
            det = f'a vela mais recente ({lag_asset}) chegou há {lag_min:.0f} min'
        st.markdown(f'<div class="win alert"><span class="pt"></span>'
                    f'<b>Atraso na fonte de dados</b> — {det}.</div>', unsafe_allow_html=True)

    # medição real da latência (transparência)
    render_s = time.perf_counter() - t_scan0
    _f = st.session_state.get("fontes", {})
    SRC = {"twelvedata": "Twelve Data", "binance": "Binance",
           "coinbase": "Coinbase", "yfinance": "yfinance"}
    varridos = {a["name"] for a in scan_list}

    def chip(rot, val, alerta=False):
        return (f'<span class="chip{" warn" if alerta else ""}">'
                f'<span class="ck">{rot}</span><span class="cv">{val}</span></span>')

    cs = []
    # uma pastilha por fonte: quantos ativos ela serviu e qual o atraso dela
    for src in sorted(set(_f.get(n, "?") for n in varridos)):
        qtd = sum(1 for n in varridos if _f.get(n) == src)
        lag = lag_fonte.get(src)
        val = f'{qtd} ativo{"s" if qtd != 1 else ""}'
        if lag is not None:
            val += f' · {lag:.1f}min'
        cs.append(chip(SRC.get(src, src), val, alerta=(src == "yfinance")))
    cs.append(chip("Busca", f"{fetch_s:.1f}s"))
    # Latência da VIRADA: só faz sentido medir no rerun disparado pela troca de
    # vela. Num carregamento manual no meio da vela, _age é a idade da vela e não
    # mede nada — por isso só grava dentro da janela de entrada, e nas demais
    # execuções mostra a última medida válida.
    if primeiro_da_vela:
        st.session_state["turn_lat"] = _age + (time.perf_counter() - t_scan0)
    tl = st.session_state.get("turn_lat")
    cs.append(chip("Sinal pronto em",
                   f"+{tl:.1f}s da virada" if tl is not None else "aguardando virada",
                   alerta=(tl is not None and tl > 5)))
    if TD_KEY:
        usados, lim, dia = td_status()
        cs.append(chip("Créditos TD", f"{usados}/{lim} min · {dia}/800 dia",
                       alerta=(dia > 700)))
    if bloqueados:
        cs.append(chip("Bloqueados", f"{len(bloqueados)}", alerta=True))
    err = st.session_state.get("td_erro")
    if err:
        cs.append(chip("Twelve Data", "indisponível", alerta=True))
    st.markdown(f'<div class="diag">{"".join(cs)}</div>', unsafe_allow_html=True)
    if err:
        st.caption(f"Twelve Data: {err}")

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
        # Estado vazio compacto. Antes era um cartão da mesma altura do sinal
        # real, com muito espaço morto — e "nenhuma entrada" é o estado mais
        # frequente do app, então não faz sentido ele ocupar a tela inteira.
        st.markdown(f"""<div class="empty">
          <div class="e-ico">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
                 stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4.2-4.2"/></svg>
          </div>
          <div class="e-txt">
            <b>Nenhuma entrada agora</b>
            <span>Varrendo {len(scan_list)} ativos com {len(sel_strats)} estratégia(s).
            Ficar sem entrada na maior parte das velas é o comportamento esperado —
            filtro que dispara sempre não está filtrando nada.</span>
          </div>
          <div class="e-side">
            <span class="k">Próxima vela</span><span class="v mono">{cvela}</span>
          </div></div>""", unsafe_allow_html=True)

    if audio_on:
        if entries:
            top = entries[0]
            ests = ", ".join(_short(s) for s in top["strats"])
            pl = "estratégias" if len(top["strats"]) > 1 else "estratégia"
            fala = (f"Entrada agora. {top['a']['voz']}. {top['dir']}. "
                    f"{pl} {ests}. Força {FL[top['force']].lower()}.")
        else:
            fala = ""
        html_box(f"""
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

    N_MIN = 200          # abaixo disso o IC é largo demais para significar algo

    def cell(n, w):
        """
        Célula de taxa. Amostra pequena é DESTACADA PARA BAIXO de propósito:
        com 30 operações, 70% e 45% são estatisticamente a mesma coisa, e um
        número grande em verde faz o olho acreditar no que o dado não sustenta.
        """
        if n == 0:
            return '<span class="wr mid">—</span><br><span class="n">0 ops</span>'
        p, lo, hi = wilson_ci(w, n)
        v = verdict(w, n, PAYOUT)
        vc, vt = VS[v]
        if n < N_MIN:
            return (f'<span class="wr faint">{p*100:.1f}%</span>'
                    f'<span class="ci">IC95 {lo*100:.0f}–{hi*100:.0f}%</span><br>'
                    f'<span class="n">{n} ops</span>'
                    f'<span class="verd v-faint">amostra pequena</span>')
        cls = "good" if v == "acima" else ("bad" if v == "abaixo" else "mid")
        return (f'<span class="wr {cls}">{p*100:.1f}%</span>'
                f'<span class="ci">IC95 {lo*100:.0f}–{hi*100:.0f}%</span><br>'
                f'<span class="n">{n} ops</span><span class="verd {vc}">{vt}</span>')

    # Recálculo sob demanda: nada aqui roda sozinho, senão trava a aba Sinais.
    cp = get_perf()
    bc1, bc2 = st.columns([0.62, 4], vertical_alignment="center")
    with bc1:
        pedir = st.button("Calcular" if cp is None else "Recalcular",
                          use_container_width=True, key="btn_perf")
    with bc2:
        if cp is None:
            st.caption("O backtest baixa 1 mês de velas de todos os ativos — "
                       "leva alguns segundos e por isso só roda quando você pede.")
        else:
            desatual = cp["chave"] != (interval, len(scan_list))
            q = f'calculado às {hm(cp["quando"])} em {cp["levou"]:.1f}s'
            st.caption(("⚠️ " + q + " — com outro timeframe/lista de ativos. Recalcule."
                        ) if desatual else q)
    if pedir:
        with st.spinner("Rodando o backtest…"):
            cp = get_perf(calcular=True)

    if cp is not None:
        perf = cp["dados"]
        ranked = sorted(STRATEGIES, key=lambda k: (perf[k]["per"][1] / perf[k]["per"][0]) if perf[k]["per"][0] else 0,
                        reverse=True)
        top = ranked[0] if perf[ranked[0]]["per"][0] else None
        proven = bool(top) and verdict(perf[top]["per"][1], perf[top]["per"][0], PAYOUT) == "acima"

        # ---- resumo: a resposta antes da tabela ----
        acima, abaixo, incon, total_ops = 0, 0, 0, 0
        for nm_, p_ in perf.items():
            n_, w_ = p_["per"]
            total_ops += n_
            if n_ < N_MIN:
                incon += 1
                continue
            v_ = verdict(w_, n_, PAYOUT)
            acima += v_ == "acima"
            abaixo += v_ == "abaixo"
            incon += v_ == "inconclusivo"
        if acima:
            veredito = (f'<b class="big good">{acima}</b> estratégia(s) com vantagem '
                        f'estatística sobre o breakeven')
            vcls = "good"
        else:
            veredito = ('<b class="big mid">Nenhuma</b> estratégia comprovou vantagem '
                        'sobre o breakeven neste período')
            vcls = "mid"
        st.markdown(
            f'<div class="sumbar {vcls}"><div class="s-main">{veredito}</div>'
            f'<div class="s-side"><span><i>{abaixo}</i> abaixo do breakeven</span>'
            f'<span><i>{incon}</i> sem conclusão</span>'
            f'<span><i>{total_ops:,}</i> operações analisadas</span></div></div>'
            .replace(",", "."), unsafe_allow_html=True)

        # Ordenado pela taxa do período, não em ordem alfabética: a pergunta é
        # "qual funciona melhor", então a resposta tem de estar na primeira linha.
        rows = ""
        for name in ranked:
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
                    f'· breakeven {BE:.2f}% · ordenado pela taxa do período</div>',
                    unsafe_allow_html=True)
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
        # Cartões próprios em vez de st.metric: o componente padrão do Streamlit
        # tem outra tipografia e destoa do resto da interface.
        if fechados:
            p, lo, hi = wilson_ci(g, len(fechados))
            v = verdict(g, len(fechados), PAYOUT)
            txt = {"acima": "acima do breakeven", "abaixo": "abaixo do breakeven",
                   "inconclusivo": "não conclusivo", "sem dados": "sem dados"}[v]
            tcls = {"acima": "good", "abaixo": "bad"}.get(v, "mid")
            taxa_txt = f'{taxa:.1f}%'
            sub = f'IC95 {lo*100:.0f}–{hi*100:.0f}% · {txt}'
        else:
            tcls, taxa_txt, sub = "mid", "—", "nenhuma operação resolvida ainda"

        def stat(rot, val, extra="", cls=""):
            return (f'<div class="stat"><span class="k">{rot}</span>'
                    f'<span class="v {cls}">{val}</span>'
                    f'<span class="x">{extra}</span></div>')

        st.markdown(
            '<div class="statrow">'
            + stat("Sinais registrados", len(hist), f"{abertos} aguardando fechar")
            + stat("Resolvidos", len(fechados), f"{emp} empate(s) devolvido(s)")
            + stat("Acertos", g, f"de {len(fechados)} operações")
            + stat("Taxa do forward test", taxa_txt, sub, tcls)
            + '</div>', unsafe_allow_html=True)
        st.caption(f"Breakeven {BE:.2f}% com payout {payout_lbl}.")

        VTXT = {"acima": ('v-good', 'acima do breakeven'),
                "abaixo": ('v-bad', 'abaixo do breakeven'),
                "inconclusivo": ('v-mid', 'não conclusivo'),
                "sem dados": ('v-mid', 'sem dados')}

        def linha_ic(rot, sub, sinais, payout):
            """Linha de tabela com n, taxa, IC95 e veredito para um recorte."""
            f = [h for h in sinais if h["res"] in ("ganhou", "perdeu")]
            if not f:
                return (f'<tr><td class="nm">{rot}</td><td class="n">{sub}</td>'
                        f'<td class="n">0</td><td class="n">—</td><td class="n">—</td>'
                        f'<td><span class="verd v-mid">sem dados</span></td></tr>')
            w = sum(1 for h in f if h["res"] == "ganhou")
            _p, _lo, _hi = wilson_ci(w, len(f))
            cls, t = VTXT[verdict(w, len(f), payout)]
            return (f'<tr><td class="nm">{rot}</td><td class="n">{sub}</td>'
                    f'<td class="n mono">{len(f)}</td>'
                    f'<td class="n mono">{w/len(f)*100:.1f}%</td>'
                    f'<td class="n mono">{_lo*100:.0f}–{_hi*100:.0f}%</td>'
                    f'<td><span class="verd {cls}">{t}</span></td></tr>')

        # ---- por estratégia (um sinal com 2 estratégias conta nas duas) ----
        por_est = {}
        for h in hist:
            for s in h["strats"]:
                por_est.setdefault(s, []).append(h)
        if por_est:
            linhas = "".join(
                linha_ic(s, "estratégia", v, PAYOUT)
                for s, v in sorted(por_est.items(),
                                   key=lambda kv: -sum(1 for h in kv[1]
                                                       if h["res"] in ("ganhou", "perdeu"))))
            st.markdown('<div class="sect">Forward test por estratégia</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Tipo</th><th>Ops</th>'
                        f'<th>Acerto</th><th>IC95</th><th>Veredito</th></tr>{linhas}</table>',
                        unsafe_allow_html=True)

        # ---- dado fresco x dado atrasado: a pergunta que a instrumentação responde ----
        com_lag = [h for h in hist if isinstance(h.get("lag"), (int, float))
                   and math.isfinite(h["lag"])]
        if com_lag:
            corte = max(1.0, float(minutes))
            fresco = [h for h in com_lag if h["lag"] <= corte]
            velho = [h for h in com_lag if h["lag"] > corte]
            linhas = (linha_ic(f"Atraso ≤ {corte:.0f} min", "dado fresco", fresco, PAYOUT)
                      + linha_ic(f"Atraso > {corte:.0f} min", "dado atrasado", velho, PAYOUT))
            st.markdown('<div class="sect">Efeito do atraso dos dados</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Tipo</th><th>Ops</th>'
                        f'<th>Acerto</th><th>IC95</th><th>Veredito</th></tr>{linhas}</table>',
                        unsafe_allow_html=True)
            st.caption("Só ganha sentido com algumas centenas de operações. Até lá os "
                       "intervalos vão ficar largos e o veredito, não conclusivo — isso é "
                       "o esperado, não um defeito.")

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
            _lg = h.get("lag")
            lag_txt = f'{_lg:.1f}min' if isinstance(_lg, (int, float)) and math.isfinite(_lg) else "—"
            rows += (f'<tr><td class="n">{dhm(h["ts"])}</td>'
                     f'<td class="nm">{h["asset"]}</td>'
                     f'<td class="{dcls}" style="font-weight:800">{arw} {h["dir"]}</td>'
                     f'<td class="n">{FL[h["force"]]}</td>'
                     f'<td>{chips_h}</td>'
                     f'<td class="n mono">{lag_txt}</td><td>{r}</td></tr>')
        st.markdown('<div class="sect">Sinais desta sessão</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl"><tr><th>Vela</th><th>Ativo</th><th>Direção</th>'
                    f'<th>Força</th><th>Estratégias</th><th>Atraso</th>'
                    f'<th>Resultado</th></tr>{rows}</table>',
                    unsafe_allow_html=True)
        st.markdown('<div class="note"><b>Este é o teste que vale.</b> Aqui não há backtest nem '
                    'seleção de período: são os sinais que o app realmente emitiu, apurados pela '
                    'cor da vela em que a entrada valeria. É a amostra mais honesta que existe — '
                    'e a única livre de garimpo de dados.</div>', unsafe_allow_html=True)

    if HIST_REMOTO:
        err = _gist_err[0]
        if err:
            st.markdown(f'<div class="win alert"><span class="pt"></span>'
                        f'<b>Falha ao sincronizar</b> — {err}. O histórico está só no disco '
                        f'do container e se perde no próximo rebuild.</div>',
                        unsafe_allow_html=True)
        else:
            st.markdown('<div class="note"><b>Histórico sincronizado.</b> Está espelhado no '
                        'seu Gist privado, então sobrevive aos reinícios do Streamlit Cloud.'
                        '</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="win alert"><span class="pt"></span><b>Este histórico é temporário.</b> '
            'O disco do Streamlit Cloud é apagado a cada rebuild do app, então tudo abaixo '
            'some junto — e sem amostra acumulada o forward test nunca sai de "não conclusivo". '
            'Para preservar: crie um Gist privado com um arquivo <code>sinais_historico.json</code> '
            'e um token do GitHub com escopo <code>gist</code>, e adicione '
            '<code>GITHUB_TOKEN</code> e <code>GIST_ID</code> nos Secrets do app. '
            'Enquanto isso, baixe o CSV abaixo com frequência.</div>',
            unsafe_allow_html=True)

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
