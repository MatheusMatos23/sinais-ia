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

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

from strategies import (STRATEGIES, MIN_SCORE, add_indicators, score_of, classify,
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
  padding:var(--pad-card);margin-bottom:var(--gap-bloco);
  position:relative;overflow:hidden}
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
/* Contador no cabeçalho: o relógio fica onde o olho já procura tempo, e o app
   deixa de gastar uma faixa inteira só para isso. */
.cd-meta{min-width:92px}
.cd-meta .v{font-size:1.02rem;letter-spacing:.01em;transition:color .3s}
/* A barra de progresso atravessa o rodapé do cabeçalho inteiro. Dentro da
   métrica ela desalinhava a linha de base em relação às outras. */
.hdr-prog{position:absolute;left:0;right:0;bottom:0;height:2px;
  background:rgba(255,255,255,.05)}
.hdr-prog i{display:block;height:100%;width:0;background:var(--buy);
  transition:width .9s linear;box-shadow:0 0 10px rgba(0,200,138,.5)}
.cd-badge{font-size:.56rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  padding:5px 10px;border-radius:999px;background:rgba(0,200,138,.12);color:var(--buy);
  border:1px solid rgba(0,200,138,.3);animation:lpulse 1.2s ease-in-out infinite}
/* pastilha Ao vivo / Pausado — é um link, não um widget do Streamlit */
.livebtn{display:inline-flex;align-items:center;gap:7px;flex:none;text-decoration:none!important;
  font-size:.64rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding:7px 13px;border-radius:999px;border:1px solid var(--line2);
  color:var(--mut);background:var(--surf2);transition:filter .15s,border-color .15s}
.livebtn:hover{filter:brightness(1.25)}
.livebtn i{width:7px;height:7px;border-radius:50%;background:var(--mut);font-style:normal}
.livebtn.on{color:var(--buy);border-color:rgba(0,200,138,.34);background:rgba(0,200,138,.10)}
/* O ponto "ao vivo" pulsa o HALO, não a opacidade. Piscar o elemento inteiro
   criava um tique constante no canto da tela, competindo com o sinal. */
.livebtn.on i{background:var(--buy);animation:halo 2.4s ease-in-out infinite}
@keyframes halo{
  0%,100%{box-shadow:0 0 0 3px rgba(0,200,138,.18)}
  50%{box-shadow:0 0 0 5px rgba(0,200,138,.04)}}
/* Usada só pela pastilha de ENTRADA VÁLIDA, que dura 20s e precisa chamar. */
@keyframes lpulse{0%,100%{opacity:1}50%{opacity:.45}}
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
/* ---------- TABELAS ----------
   DEFINIÇÃO ÚNICA. Havia três blocos .tbl espalhados pelo arquivo, com
   border-collapse alternando entre collapse e separate — e o cabeçalho sticky
   dentro de um cartão com overflow:hidden. Essa combinação é justamente a que
   quebra: com border-collapse:collapse o `th` sticky descola das bordas e
   invade as linhas, e o overflow:hidden impede a fixação funcionar.
   Cabeçalho fixo foi removido: as abas já são sticky e dão a referência. */
.tbl{width:100%;font-size:.83rem;border-collapse:separate;border-spacing:0;
  background:var(--surf);border:1px solid var(--line);border-radius:var(--r2);
  overflow:hidden;margin-bottom:var(--gap-bloco)}
.tbl th{background:var(--surf2);border-bottom:1px solid var(--line2);
  padding:11px 16px}
.tbl td{padding:14px 16px;border-bottom:1px solid var(--line)}
.tbl tr:last-child td{border-bottom:0}
.tbl tr:nth-child(even) td{background:rgba(255,255,255,.012)}
.tbl tr:hover td{background:rgba(255,255,255,.035)}
.tbl tr.on td{background:rgba(0,200,138,.05);box-shadow:inset 2px 0 0 var(--buy)}
.tbl tr.on:hover td{background:rgba(0,200,138,.08)}
.tbl th{text-align:left;font-size:.56rem;letter-spacing:.14em;color:var(--mut);font-weight:600;
  text-transform:uppercase;padding:0 14px 10px;border-bottom:1px solid var(--line)}
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
.win{display:flex;align-items:flex-start;gap:10px;border-radius:10px;padding:12px 20px;
  font-size:.82rem;color:var(--ink2);margin:var(--gap-bloco) 0 var(--gap-curto);border:1px solid var(--line)}
.win .pt{width:7px;height:7px;border-radius:50%;flex:0 0 auto;margin-top:6px}
/* Tudo que não for o ponto entra AQUI. Sem este wrapper, cada <b> e <code>
   solto vira um item de flex e o texto sai quebrado em colunas. */
.win .msg{flex:1 1 auto;min-width:0;line-height:1.6}
.win code{font-family:'IBM Plex Mono',monospace;font-size:.75rem;
  background:rgba(255,255,255,.06);border:1px solid var(--line2);border-radius:5px;
  padding:1px 6px;color:var(--ink);white-space:nowrap}
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
/* Caixa colapsável do diagnóstico: uma linha quando está tudo certo. */
.diagbox{margin:var(--gap-curto) 0 var(--gap-bloco)}
.diagbox summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:8px;
  font-size:.7rem;color:var(--mut);font-weight:500;padding:5px 0;user-select:none}
.diagbox summary::-webkit-details-marker{display:none}
.diagbox summary:hover{color:var(--ink2)}
.diagbox summary::after{content:"▾";font-size:.6rem;opacity:.6;transition:transform .15s}
.diagbox[open] summary::after{transform:rotate(180deg)}
.diagbox .dot{width:6px;height:6px;border-radius:50%;flex:none}
.diagbox .dot.ok{background:var(--buy);box-shadow:0 0 0 3px rgba(0,200,138,.14)}
.diagbox .dot.bad{background:var(--warn);box-shadow:0 0 0 3px rgba(217,164,65,.14)}
.diag{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:var(--gap-curto) 0 0}
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
/* O iframe do contador não desenha nada (só roda o script que alimenta o
   cabeçalho). Altura 0 é rejeitada pela API, então fica 1px e some no CSS.
   ATENÇÃO: escopo pelo irmão do marcador .wheel-pass, NÃO por title. Os dois
   iframes do app têm title="st.iframe"; a regra ampla espremia também o do
   áudio, que ficava com barra de rolagem. */
[data-testid="stElementContainer"]:has(.wheel-pass) + [data-testid="stElementContainer"]{
  min-height:0;height:0;overflow:hidden;margin:0}

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
/* ESCOPO NA BARRA DE CONTROLES. Estas regras existem por causa dos chips que
   quebram em várias linhas — situação que só acontece lá. Aplicadas de forma
   global, o `align-items:flex-start` empurrava o texto de placeholder dos
   filtros do Histórico para cima e ele saía cortado no topo da caixa. */
[data-testid="stHorizontalBlock"]:has(.lbl) .stMultiSelect [data-baseweb="select"]>div{
  flex-wrap:wrap;align-items:flex-start;padding-top:5px;padding-bottom:5px}
/* o "limpar" e a seta ficavam boiando no meio vertical quando havia 4+ chips */
[data-testid="stHorizontalBlock"]:has(.lbl) .stMultiSelect
  [data-baseweb="select"]>div>div:last-child{align-self:flex-start;padding-top:3px}
[data-testid="stHorizontalBlock"]:has(.lbl) .stMultiSelect
  [data-baseweb="tag"]{margin:2px 3px 2px 0!important}
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

/* ---------- FIM DO PISCA-PISCA ----------
   Medido no DOM: 62 contêineres carregam `data-stale` e `transition: all`.
   Durante cada rerun o Streamlit marca data-stale="true" e reduz a opacidade
   deles. Com auto-refresh a cada 15s, a tela inteira desbota e volta — é o
   "apagando e acendendo". A informação já é atualizada de qualquer forma, então
   o efeito não comunica nada: só cansa a vista de quem está esperando entrada. */
[data-testid="stElementContainer"],
[data-testid="stVerticalBlock"],
[data-testid="stHorizontalBlock"]{transition:none!important}
[data-stale="true"]{opacity:1!important;transition:none!important;filter:none!important}

/* posição na primeira coluna, como ranking */
.tbl .nm{position:relative}
.rankn{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;
  border-radius:6px;background:var(--surf2);border:1px solid var(--line);
  font-family:'IBM Plex Mono',monospace;font-size:.62rem;font-weight:700;
  color:var(--mut);margin-right:10px;vertical-align:1px}
.rankn.top{background:var(--buy-dim);border-color:rgba(0,200,138,.3);color:var(--buy)}

/* ---------- GRÁFICO DE HORAS ---------- */
.horas{position:relative;display:flex;align-items:flex-end;gap:3px;height:120px;
  background:var(--surf);border:1px solid var(--line);border-radius:var(--r2);
  padding:14px 16px 8px;margin-bottom:var(--gap-curto)}
.horas .hcol{flex:1;display:flex;flex-direction:column;justify-content:flex-end;
  align-items:center;height:100%;gap:6px}
.horas .hcol i{width:100%;border-radius:4px 4px 0 0;display:block;min-height:4px;
  transition:filter .15s}
.horas .hcol:hover i{filter:brightness(1.35)}
.horas .hcol i.bom{background:linear-gradient(180deg,var(--buy),rgba(0,200,138,.35))}
.horas .hcol i.neutro{background:rgba(255,255,255,.14)}
.horas .hcol i.ruim{background:linear-gradient(180deg,rgba(255,74,99,.55),rgba(255,74,99,.2))}
.horas .hcol i.vazio{background:repeating-linear-gradient(45deg,
  rgba(255,255,255,.05) 0 3px,transparent 3px 6px);height:8px}
.horas .hh{font-family:'IBM Plex Mono',monospace;font-size:.56rem;color:var(--mut)}
.horas .h-linha.be{position:absolute;left:16px;right:16px;top:52%;
  border-top:1px dashed var(--warn);opacity:.55}

/* barra do que foi registrado na vela atual */
.regbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin:var(--gap-curto) 0 var(--gap-bloco);font-size:.7rem}
.regbar .k{font-size:.58rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mut);font-weight:600;margin-right:2px}
.regchip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:999px;
  background:var(--surf2);border:1px solid var(--line2);color:var(--ink2);font-weight:600}
.regchip.off{opacity:.45;text-decoration:line-through;border-style:dashed}
.regbar .obs{color:var(--mut);font-size:.66rem}

/* estado pausado do scanner */
.empty.pausado{border-left:3px solid var(--warn)}
.empty.pausado .e-ico{color:var(--warn);border-color:rgba(217,164,65,.3);
  background:rgba(217,164,65,.08)}

/* Separador de dia no histórico, com o subtotal na própria linha. */
.tbl tr.daysep td{background:rgba(255,255,255,.03);border-bottom:1px solid var(--line2);
  padding:9px 14px}
.tbl tr.daysep .dlbl{font-size:.62rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink2);font-weight:700;margin-right:12px}
.tbl tr.daysep:hover td{background:rgba(255,255,255,.03)}

/* Altura mínima do painel: quando o número de entradas cai para zero o conteúdo
   encolhe e a página dá um salto. Reservando o espaço, nada pula. */
[data-testid="stTabs"] [role="tabpanel"]{min-height:520px}

/* Realce de 1s no cartão que mudou. É o oposto do pisca geral: em vez de
   destacar tudo (e portanto nada), marca só o que é novo. */
@keyframes surge{
  0%{box-shadow:0 0 0 0 rgba(0,200,138,.55);transform:translateY(-2px)}
  100%{box-shadow:0 0 0 14px rgba(0,200,138,0);transform:none}}
.hero.novo{animation:surge 1s cubic-bezier(.2,.7,.3,1) 1}

/* ---------- MODO FOCO ----------
   Só o sinal na tela. Para quando você está operando, não configurando. */
body.foco [data-testid="stHorizontalBlock"]:has(.lbl),
body.foco [data-testid="stTabs"] [role="tablist"],
body.foco .diagbox,
body.foco .hdr .meta:not(.cd-meta){display:none!important}
body.foco .block-container{padding-top:.6rem}
body.foco [data-testid="stTabs"] [role="tabpanel"]{min-height:0}
.focobtn{display:inline-flex;align-items:center;gap:7px;text-decoration:none!important;
  font-size:.64rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding:7px 12px;border-radius:999px;border:1px solid var(--line2);
  color:var(--mut);background:var(--surf2);flex:none;margin-left:8px}
.focobtn:hover{filter:brightness(1.25)}
.focobtn.on{color:var(--warn);border-color:rgba(217,164,65,.35);
  background:rgba(217,164,65,.10)}

/* Rodapé discreto. */
.foot{margin:var(--gap-secao) 0 8px;text-align:center;font-size:.66rem;color:var(--mut);
  border-top:1px solid var(--line);padding-top:16px}

/* Barra "vs. breakeven" na tabela de desempenho. */
.barcel{vertical-align:middle}
.bar{position:relative;height:8px;border-radius:4px;background:rgba(255,255,255,.05);
  overflow:hidden;min-width:150px}
.bar .fill{position:absolute;left:0;top:0;height:100%;border-radius:4px;display:block}
.bar .be{position:absolute;top:-3px;width:1px;height:14px;background:var(--warn);
  display:block;opacity:.9}

/* Curva da taxa acumulada. */
.curva{background:var(--surf);border:1px solid var(--line);border-radius:var(--r);
  padding:var(--pad-card);margin:var(--gap-bloco) 0 var(--gap-curto)}
.curva .c-head{display:flex;justify-content:space-between;align-items:center;
  gap:12px;margin-bottom:10px;flex-wrap:wrap}
.curva .c-head .k{font-size:.58rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mut);font-weight:600}
.curva .lg{display:inline-flex;align-items:center;gap:6px;font-size:.64rem;color:var(--mut)}
.curva .lg i.be{width:14px;height:0;border-top:1px dashed var(--warn);display:inline-block}
.curva svg{width:100%;height:140px;display:block}
.curva .c-foot{display:flex;justify-content:space-between;margin-top:8px;
  font-size:.64rem;color:var(--mut)}
.curva .c-foot .mono{font-family:'IBM Plex Mono',monospace;color:var(--ink2);font-weight:600}

/* Telas largas: 1180px deixava margens enormes sobrando e apertava as tabelas. */
@media(min-width:1500px){.block-container{max-width:1360px}}

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
foco = st.query_params.get("foco", "0") == "1"

# --- Persistência remota (opcional) ---------------------------------------
# O disco do Streamlit Cloud é EFÊMERO: todo rebuild do app zera o container e
# leva o hist_signals.json junto. Sem isso, o forward test nunca acumula amostra
# suficiente para dar veredito conclusivo. Com um token nos secrets, o histórico
# é espelhado num Gist privado e sobrevive aos reinícios.
GIST_FILE = "sinais_historico.json"      # histórico
GIST_CFG = "kairo_config.json"           # preferências


def _gh():
    """(token, gist_id) dos secrets, ou (None, None). O token nunca vai pro código."""
    def s(k):
        try:
            v = st.secrets.get(k, "")
        except Exception:
            v = ""
        return v or os.environ.get(k, "")
    return s("GITHUB_TOKEN") or None, s("GIST_ID") or None


def gist_load(arquivo=GIST_FILE):
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
        c = r.json().get("files", {}).get(arquivo, {}).get("content")
        return json.loads(c) if c else []
    except Exception as e:
        st.session_state["gist_erro"] = str(e)[:90]
        return None


_gist_err = [""]          # último erro de gravação (fora do session_state: roda em thread)


def gist_save(out, arquivo=GIST_FILE):
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
                json={"files": {arquivo: {"content": json.dumps(out, ensure_ascii=False)}}})
            _gist_err[0] = "" if r.status_code == 200 else f"HTTP {r.status_code} ao gravar"
        except Exception as e:
            _gist_err[0] = str(e)[:90]

    threading.Thread(target=_w, daemon=True).start()
    return True


HIST_REMOTO = all(_gh())


# ---------- preferências do usuário ----------
# Mesmo caminho do histórico: arquivo no disco (sobrevive a recarregar a página)
# espelhado no Gist quando configurado (sobrevive ao rebuild do container).
# Precisa ser carregado AQUI, antes dos widgets, porque cada um usa o valor
# salvo como padrão.
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kairo_config.json")
CFG_PADRAO = {
    "tf": "5", "estrategias": None, "forca": "FRACA", "mercado": "Tudo",
    "payout": "80%", "intervalo": 15, "so_confluencia": False,
    "fora_sessao": False, "audio": False, "sistema": True,
    "usar_janela": False, "janela": [9, 17],
}


def cfg_load():
    if "cfg" in st.session_state:
        return st.session_state["cfg"]
    cfg = dict(CFG_PADRAO)
    remoto = gist_load(GIST_CFG)
    if isinstance(remoto, dict):
        cfg.update({k: v for k, v in remoto.items() if k in CFG_PADRAO})
    else:
        try:
            if os.path.exists(CFG_PATH):
                with open(CFG_PATH, "r", encoding="utf-8") as f:
                    cfg.update({k: v for k, v in json.load(f).items() if k in CFG_PADRAO})
        except Exception:
            pass
    st.session_state["cfg"] = cfg
    return cfg


def cfg_save(cfg):
    """Só grava quando algo mudou — senão escreveria em disco a cada rerun."""
    if st.session_state.get("cfg_salvo") == cfg:
        return
    st.session_state["cfg_salvo"] = dict(cfg)
    st.session_state["cfg"] = dict(cfg)
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
    except Exception:
        pass
    gist_save(cfg, GIST_CFG)


CFG = cfg_load()

# ---------- persistência do histórico ----------
HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hist_signals.json")




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



def pnl_de(h):
    """
    Resultado financeiro de um sinal, em unidades monetárias.
    Binária: acerto devolve a aposta + payout; erro perde a aposta; empate zera.
    Usa o payout e o valor GRAVADOS no sinal, não os atuais — senão mudar o
    payout hoje reescreveria o resultado de operações passadas.
    """
    v = float(h.get("stake") or 0)
    if not v or h.get("res") not in ("ganhou", "perdeu"):
        return 0.0
    p = float(h.get("payout") or 0.8)
    return v * p if h["res"] == "ganhou" else -v


def pnl_do_dia(hist_, dia_br):
    return sum(pnl_de(h) for h in hist_
               if br(h["ts"]).date() == dia_br and h.get("exec", True))


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
    _tfs = ["1 min", "5 min", "15 min"]
    _tf_ini = _tfs.index(f'{CFG["tf"]} min') if f'{CFG["tf"]} min' in _tfs else 1
    tf_label = st.radio("tf", _tfs, index=_tf_ini, horizontal=True,
                        label_visibility="collapsed")
    TF = {"1 min": "1", "5 min": "5", "15 min": "15"}[tf_label]
with cc2:
    st.markdown('<div class="lbl">Estratégias ativas</div>', unsafe_allow_html=True)
    default_sel = [k for k in (CFG.get("estrategias")
                              or ("G · Fade vela extrema", "J · Z-score forte",
                                  "K · Reversão dupla"))
                   if k in STRATEGIES]
    sel_strats = st.multiselect("est", list(STRATEGIES), default=default_sel,
                                format_func=lambda s: CHIP.get(s, s),
                                placeholder="Escolha uma ou mais estratégias",
                                label_visibility="collapsed")
    if not sel_strats:
        sel_strats = default_sel or [list(STRATEGIES)[0]]

    # Com 4+ estratégias os chips quebravam em duas linhas e esticavam a barra.
    # A partir do 4º ficam recolhidos atrás de um "+N"; passar o mouse ou focar
    # o campo mostra todos de novo. O seletor segue a estrutura real do BaseWeb:
    # select > div > (contêiner das tags) + (limpar/seta).
    _extra = len(sel_strats) - 3
    if _extra > 0:
        # Escopo obrigatório na barra de controles: sem isso a regra atinge TODOS
        # os multiselects da página — os filtros do Histórico ganhavam um "+N"
        # fantasma por cima do texto de placeholder.
        # Escopo obrigatório na barra de controles: sem isso a regra atinge TODOS
        # os multiselects da página — os filtros do Histórico ganhavam um "+N"
        # fantasma por cima do texto de placeholder.
        _BAR = '[data-testid="stHorizontalBlock"]:has(.lbl) .stMultiSelect'
        _TAGS = '[data-baseweb="select"]>div>div:first-child'
        st.markdown(f"""<style>
        {_BAR} {_TAGS}>[data-baseweb="tag"]:nth-child(n+4){{display:none}}
        {_BAR} {_TAGS}::after{{content:"+{_extra}";display:inline-flex;align-items:center;
          height:24px;padding:0 9px;margin:2px 0;border-radius:7px;
          font-family:'Inter',sans-serif;font-size:.7rem;font-weight:600;
          color:var(--mut);background:var(--surf2);border:1px solid var(--line2);
          cursor:default}}
        {_BAR}:hover {_TAGS}>[data-baseweb="tag"]:nth-child(n+4),
        {_BAR}:focus-within {_TAGS}>[data-baseweb="tag"]:nth-child(n+4){{display:inline-flex}}
        {_BAR}:hover {_TAGS}::after,
        {_BAR}:focus-within {_TAGS}::after{{display:none}}
        </style>""", unsafe_allow_html=True)
with cc3:
    st.markdown('<div class="lbl">Força mínima</div>', unsafe_allow_html=True)
    min_force = st.select_slider("fm", options=["FRACA", "MÉDIA", "FORTE"],
                                 value=CFG.get("forca", "FRACA"),
                                 label_visibility="collapsed")
st.markdown('</div>', unsafe_allow_html=True)

# As abas nascem aqui, logo abaixo da barra de controles. Antes o painel de
# ajustes era um expander no meio da tela de operação; agora é uma aba própria e
# a tela principal fica só com o que importa na hora de entrar.
tab_sig, tab_perf, tab_hist, tab_cfg = st.tabs(
    ["Sinais", "Desempenho", "Histórico", "Ajustes"])

with tab_cfg:
    o1, o2, o3 = st.columns(3)
    with o1:
        st.markdown("**Operação**")
        # Chave mestra: desligado, o scanner não emite nem registra nada. Serve
        # para horários em que você já sabe que não vale operar — e evita sujar
        # o forward test com sinais que você nunca executaria.
        sistema_on = st.toggle("Sistema ativo", value=CFG.get("sistema", True),
                               help="Desligado, nenhuma entrada é gerada ou gravada "
                                    "no histórico.")
        stake = st.number_input("Valor por entrada", min_value=0.0, step=5.0,
                                value=float(CFG.get("stake", 10.0)),
                                help="Usado só para calcular resultado financeiro e "
                                     "drawdown. Fica gravado em cada sinal.")
        lim_on = st.toggle("Parar após perder X no dia",
                           value=CFG.get("limite_on", False),
                           help="A única proteção que funciona contra decisão "
                                "emocional é a que você toma antes.")
        lim_val = st.number_input("Limite de perda no dia", min_value=0.0, step=10.0,
                                  value=float(CFG.get("limite", 50.0)),
                                  disabled=not lim_on)
        usar_janela = st.toggle("Operar só em uma faixa de horário",
                                value=CFG.get("usar_janela", False))
        jan_ini, jan_fim = st.slider("Faixa (horário de Brasília)", 0, 23,
                                     tuple(CFG.get("janela", [9, 17])),
                                     disabled=not usar_janela,
                                     format="%dh")
        st.markdown("**Mercados**")
        _mkts = ["Tudo", "Só forex", "Só cripto"]
        mercado = st.radio("Onde operar", _mkts,
                           index=_mkts.index(CFG.get("mercado", "Tudo"))
                           if CFG.get("mercado") in _mkts else 0,
                           horizontal=False, label_visibility="collapsed",
                           help="Cripto opera 24/7; forex fecha no fim de semana e "
                                "fora das sessões.")
        st.markdown("**Filtros**")
        only_conf = st.toggle("Só entradas com 2+ estratégias",
                              value=CFG.get("so_confluencia", False))
        show_closed = st.toggle("Incluir pares fora de sessão",
                                value=CFG.get("fora_sessao", False))
    with o2:
        st.markdown("**Áudio**")
        audio_on = st.toggle("🔊 Aviso por voz na entrada", value=CFG.get("audio", False))
        st.caption("O navegador só toca som depois de um clique seu. Libere aqui:")
        html_box("""
        <div style="font-family:Inter,sans-serif">
          <button id="u" style="background:rgba(0,200,138,.10);color:#00C88A;
            border:1px solid rgba(0,200,138,.32);border-radius:10px;padding:9px 15px;
            font-weight:600;cursor:pointer;font-size:.78rem;font-family:inherit">
            Liberar áudio neste navegador</button>
          <div id="s" style="color:#6F7B93;font-size:.68rem;margin-top:7px"></div>
        </div>
        <script>
        function say(t){try{var u=new SpeechSynthesisUtterance(t);u.lang='pt-BR';
          u.rate=1.05;window.speechSynthesis.cancel();window.speechSynthesis.speak(u);}catch(e){}}
        var el=document.getElementById('s');
        function estado(){el.textContent = window.parent.sessionStorage.getItem('voz')==='1'
          ? 'Áudio liberado nesta aba.' : 'Áudio ainda não liberado.';}
        document.getElementById('u').onclick=function(){
          window.parent.sessionStorage.setItem('voz','1');say('Voz ativada.');estado();};
        estado();
        </script>""", height=88)
    with o3:
        st.markdown("**Análise e atualização**")
        payout_lbl = st.radio("Payout padrão da corretora", ["80%", "90%"],
                              index=0 if CFG.get("payout", "80%") == "80%" else 1,
                              horizontal=True)
        st.caption("A pastilha *Ao vivo / Pausado* fica no cabeçalho, à direita.")
        every = st.slider("Intervalo (s)", 10, 60, int(CFG.get("intervalo", 15)),
                          step=5, disabled=not auto_on)
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
# Salva as preferências depois que todos os widgets existem. cfg_save só grava
# quando algo mudou de fato, então isso não escreve em disco a cada rerun.
cfg_save({
    "tf": TF, "estrategias": list(sel_strats), "forca": min_force,
    "mercado": mercado, "payout": payout_lbl, "intervalo": int(every),
    "so_confluencia": bool(only_conf), "fora_sessao": bool(show_closed),
    "audio": bool(audio_on), "sistema": bool(sistema_on),
    "usar_janela": bool(usar_janela), "janela": [int(jan_ini), int(jan_fim)],
    "stake": float(stake), "limite_on": bool(lim_on), "limite": float(lim_val),
})

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

# Classe de ativo escolhida nos Ajustes. Filtra ANTES de qualquer busca, então
# escolher "só cripto" também economiza créditos da Twelve Data.
_tipo = {"Só forex": "fx", "Só cripto": "crypto"}.get(mercado)
_universo = [a for a in ASSETS if _tipo is None or a["type"] == _tipo]
open_assets = [a for a in _universo if pair_open(a, now)]
scan_list = _universo if show_closed else open_assets
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

# Chave mestra e janela de horário. Fora do horário permitido o app continua
# mostrando dados e diagnóstico, mas NÃO emite entrada — e, como record_and_resolve
# só grava o que está em `entries`, o histórico também não é contaminado.
_h_br = br(now).hour
if usar_janela:
    dentro_janela = (jan_ini <= _h_br <= jan_fim) if jan_ini <= jan_fim \
        else (_h_br >= jan_ini or _h_br <= jan_fim)
else:
    dentro_janela = True
# Limite de perda diária: consulta o histórico já gravado. Fica ANTES do gate
# porque, uma vez atingido, nenhuma entrada nova deve ser gerada nem registrada.
_perda_hoje = 0.0
_bloqueio_perda = False
if lim_on and lim_val > 0:
    _perda_hoje = pnl_do_dia(hist_load(), br(now).date())
    _bloqueio_perda = _perda_hoje <= -abs(lim_val)

operando = sistema_on and dentro_janela and not _bloqueio_perda

entries = list(agg.values()) if operando else []
minf = {"FRACA": 1, "MÉDIA": 2, "FORTE": 3}[min_force]
entries = [e for e in entries if FORCE_ORDER[e["force"]] >= minf]
if only_conf:
    entries = [e for e in entries if len(e["strats"]) > 1]
entries.sort(key=lambda e: (len(e["strats"]), FORCE_ORDER[e["force"]], e["score"]), reverse=True)


# ============================== DESEMPENHO ==============================
def horas_backtest(d, score, hb, acc):
    """
    Mesma regra do backtest() — entrada na abertura da vela seguinte, acerto pela
    cor, empate devolvido —, mas agregando por hora de uma vez.

    Fazia isso com 24 chamadas a backtest() sobre fatias mascaradas, e o painel
    passou a custar o dobro (medido: 0,93s -> 1,97s). Dois groupby resolvem.
    """
    o_next = d["Open"].shift(-1)
    c_next = d["Close"].shift(-1)
    sig = score.where(score.abs() >= MIN_SCORE, 0.0)
    valid = (sig != 0) & o_next.notna() & c_next.notna()
    tie = c_next == o_next
    win = pd.Series(np.where(sig > 0, c_next > o_next, c_next < o_next), index=d.index)
    ok = valid & ~tie                       # empate sai do denominador
    if not bool(ok.any()):
        return
    for h_, v in ok.groupby(hb).sum().items():
        acc[int(h_)][0] += int(v)
    for h_, v in (ok & win).groupby(hb).sum().items():
        acc[int(h_)][1] += int(v)


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
    horas = {h: [0, 0] for h in range(24)}          # hora BRT -> [ops, acertos]
    for a in scan_list:
        df = dhist.get(a["name"])
        if df is None or len(df) < 80:
            continue
        d = add_indicators(df)                      # uma vez por ativo
        m = d.index.date == today                   # máscara do dia, idem
        tem_hoje = m.any()
        d_hoje = d[m] if tem_hoje else None
        # Hora de Brasília de cada vela: o índice está em UTC.
        hb = (d.index.hour - 3) % 24
        for name in STRATEGIES:
            sc = score_of(name, d, interval)
            r = backtest(d, sc)
            acc = out[name]
            acc["per"][0] += r["trades"]; acc["per"][1] += r["wins"]
            if tem_hoje:
                rd = backtest(d_hoje, sc[m])
                acc["hoje"][0] += rd["trades"]; acc["hoje"][1] += rd["wins"]
            # Recorte por hora só das estratégias em uso: perguntar "que horas
            # operar" sobre estratégias que você não usa não responde nada.
            if name in sel_strats:
                horas_backtest(d, sc, hb, horas)
    return {"est": out, "horas": horas}


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
            "chave": (interval, len(scan_list), tuple(sorted(sel_strats))),
            "dados": run_perf(),
            "quando": datetime.now(timezone.utc), "levou": time.perf_counter() - t0}
    return st.session_state.get("perf_cache")      # None = ainda não calculado

# ============================== TOPBAR ==============================
# Cripto opera 24/7: com a varredura só em cripto, "Forex fechado" não descreve
# nada do que está acontecendo e só confunde.
if mercado == "Só cripto":
    stat = '<span class="dotstat"><i></i>Cripto · 24/7</span>'
    sess = '<span class="sess-tag">sem janela de sessão</span>'
elif market_open(now):
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
    <div class="meta cd-meta"><span class="k">Próxima vela</span>
      <span class="v mono" id="kairo-cd">{int(secs_to_next // 60):02d}:{int(secs_to_next % 60):02d}</span></div>
    <span id="kairo-cdbadge"></span>
  </div>
  <a class="focobtn {'on' if foco else ''}" target="_self"
     href="?live={'1' if auto_on else '0'}&foco={'0' if foco else '1'}"
     title="{'Sair do modo foco' if foco else 'Modo foco: esconde controles e abas'}"
     >{'Sair do foco' if foco else 'Foco'}</a>
  <span class="hdr-prog"><i id="kairo-cdfill"
       style="width:{(_age / _per) * 100:.1f}%"></i></span>
  <a class="livebtn {'on' if auto_on else ''}" target="_self"
     href="?live={'0' if auto_on else '1'}&foco={'1' if foco else '0'}"
     title="{'Pausar a atualização automática para ler as tabelas paradas'
             if auto_on else 'Retomar a atualização automática'}"><i></i>{
     'Ao vivo' if auto_on else 'Pausado'}</a>
</div>""", unsafe_allow_html=True)

# marcador invisível: o CSS usa o irmão seguinte para liberar a roda do mouse
st.markdown('<div class="wheel-pass"></div>', unsafe_allow_html=True)
html_box(f"""
<style>*{{box-sizing:border-box}} body{{margin:0;background:transparent}}</style>
<script>
/* Este iframe não desenha nada: ele é o motor do contador que vive no CABEÇALHO.
   Como usa srcdoc, é mesma origem do app e escreve direto no DOM do pai. Isso
   eliminou um bloco inteiro de largura total que só mostrava um relógio. */
var TF={int(TF)}, JAN={ENTRY_WINDOW};
var PD=window.parent.document;
/* O modo foco é uma classe no <body> do app. Só o iframe consegue mexer lá,
   porque st.markdown não executa script. */
PD.body.classList.toggle('foco', {str(foco).lower()});
function t(){{
  var n=Date.now()/1000, per=TF*60, pos=n%per, l=per-pos;
  var m=Math.floor(l/60), s=Math.floor(l%60);
  var el=PD.getElementById('kairo-cd'); if(!el) return;
  el.textContent=(m<10?'0':'')+m+':'+(s<10?'0':'')+s;
  var f=PD.getElementById('kairo-cdfill');
  if(f) f.style.width=((pos/per)*100).toFixed(1)+'%';
  var b=PD.getElementById('kairo-cdbadge');
  if(b) b.innerHTML = pos<JAN ? '<span class="cd-badge">Entrada válida</span>' : '';
  el.style.color = pos<JAN ? '#00C88A' : '';
}}
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
</script>""", height=1)

def _short(nm):
    return nm.split("·")[0].strip()


def hist_df(h):
    if not h:
        return pd.DataFrame()
    return pd.DataFrame([{
        "data_hora_brasilia": dhm(r["ts"]), "utc": pd.Timestamp(r["ts"]).isoformat(),
        "ativo": r["asset"], "direcao": r["dir"], "forca": FL.get(r["force"], r["force"]),
        "estrategias": "+".join(r["strats"]), "timeframe_min": r.get("tf", ""),
        "resultado": r["res"] or "aguardando",
        "executei": bool(r.get("exec", False)),
        "atraso_min": r.get("lag", ""), "fonte": r.get("src", ""),
        "payout": r.get("payout", ""),
    } for r in sorted(h, key=lambda x: x["ts"], reverse=True)])


# ---------- registra os sinais emitidos e apura o resultado pela cor da vela ----------
def record_and_resolve(entries, data, minutes, na_janela):
    """
    BUG CORRIGIDO AQUI. Antes, qualquer sinal presente em QUALQUER momento da
    vela era gravado com `ts` = abertura daquela vela. Como o app varre a cada
    ~15s e a vela anterior às vezes chega atrasada, um sinal podia surgir no
    segundo 200 e ser registrado como se você tivesse entrado na abertura — uma
    entrada que não existia quando a janela estava aberta e que você não teria
    como executar. Isso inflava o histórico com operações impossíveis.

    Agora só grava dentro da janela de entrada. Se o app não estiver rodando na
    virada, não há registro — o que é a leitura correta: não houve entrada.
    """
    hist = hist_load()
    ck = candle_key(minutes)
    start = pd.Timestamp(ck * minutes * 60, unit="s")     # abertura da vela da entrada
    seen = {(h["asset"], h["dir"], h["ck"], h.get("tf")) for h in hist}
    changed = False
    for e in (entries if na_janela else []):
        k = (e["a"]["name"], e["dir"], ck, minutes)
        if k not in seen:
            nome = e["a"]["name"]
            hist.append({"ck": ck, "ts": start, "asset": nome, "dir": e["dir"],
                         "force": e["force"], "strats": [_short(s) for s in e["strats"]],
                         "tf": minutes, "res": None, "janela": True,
                         # instrumentação: permite medir depois se atraso derruba o acerto
                         "lag": (round(float(lag_ativo[nome]), 2)
                                 if nome in lag_ativo else None),
                         "src": st.session_state.get("fontes", {}).get(nome, "?"),
                         "payout": payout_de(nome), "stake": float(stake)})
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


hist = record_and_resolve(entries, data, minutes, window_open)



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
        st.markdown(f'<div class="win ok"><span class="pt"></span><div class="msg">'
                    f'<b>Entrada válida agora</b> — vela das {cvela}. '
                    f'Restam {int(ENTRY_WINDOW - _age)}s desta janela.</div></div>', unsafe_allow_html=True)
    else:
        mm, ss = divmod(int(secs_to_next), 60)
        st.markdown(f'<div class="win wait"><span class="pt"></span><div class="msg">'
                    f'<b>Vela em andamento</b> — já se passaram {int(_age)}s desta vela. '
                    f'Próxima janela de entrada em {mm:02d}:{ss:02d}.</div></div>', unsafe_allow_html=True)

    if dados_atrasados:
        if sem_vela:
            quais = ", ".join(sem_vela[:4]) + ("…" if len(sem_vela) > 4 else "")
            det = (f'a vela das {hm(vela_esperada)} ainda não chegou para {len(sem_vela)} ativo(s) '
                   f'({quais}) — <b>bloqueados nesta vela</b>, não geram entrada')
        else:
            det = f'a vela mais recente ({lag_asset}) chegou há {lag_min:.0f} min'
        st.markdown(f'<div class="win alert"><span class="pt"></span><div class="msg">'
                    f'<b>Atraso na fonte de dados</b> — {det}.</div></div>',
                    unsafe_allow_html=True)

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
        # 640 = 80% da cota diária. O aviso tem de vir com folga para dar tempo
        # de reduzir ativos ou trocar de timeframe antes de cair no yfinance.
        cs.append(chip("Créditos TD", f"{usados}/{lim} min · {dia}/800 dia",
                       alerta=(dia >= 640)))
        if dia >= 640:
            st.warning(f"Créditos da Twelve Data em {dia}/800 hoje. Perto do limite, "
                       f"o forex volta para o yfinance, que chega ~3 min mais atrasado. "
                       f"Para esticar: use 5 ou 15 min, ou reduza os ativos varridos.")
    if bloqueados:
        cs.append(chip("Bloqueados", f"{len(bloqueados)}", alerta=True))
    err = st.session_state.get("td_erro")
    if err:
        cs.append(chip("Twelve Data", "indisponível", alerta=True))
    # Resumo colapsado: o detalhe das fontes só importa quando algo está errado.
    # Aberto por padrão apenas se houver alerta.
    problema = bool(err) or bool(bloqueados) or any(
        v == "yfinance" for k, v in _f.items() if k in varridos)
    lag_pior = max(lag_fonte.values()) if lag_fonte else 0.0
    resumo = ("Fontes com problema" if problema
              else f"Fontes OK · atraso {lag_pior:.1f}min"
                   + (f" · sinal +{tl:.1f}s" if tl is not None else ""))
    st.markdown(
        f'<details class="diagbox"{" open" if problema else ""}>'
        f'<summary><span class="dot {"bad" if problema else "ok"}"></span>{resumo}</summary>'
        f'<div class="diag">{"".join(cs)}</div></details>', unsafe_allow_html=True)
    if err:
        st.caption(f"Twelve Data: {err}")

    if not operando:
        if _bloqueio_perda:
            _motivo = (f"Limite de perda do dia atingido: {_perda_hoje:.2f} de "
                       f"−{abs(lim_val):.2f}. O sistema para até amanhã.")
        elif not sistema_on:
            _motivo = "Sistema desligado."
        else:
            _motivo = (f"Fora da faixa de operação ({jan_ini}h–{jan_fim}h, "
                       f"horário de Brasília). Agora são {_h_br}h.")
        st.markdown(
            f'<div class="empty pausado"><div class="e-ico">'
            f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"'
            f' stroke-linecap="round"><path d="M9 8v8M15 8v8"/></svg></div>'
            f'<div class="e-txt"><b>Scanner pausado</b>'
            f'<span>{_motivo} Nenhuma entrada é gerada nem gravada no histórico '
            f'enquanto estiver assim — o forward test continua limpo.</span></div>'
            f'<div class="e-side"><span class="k">Reativar em</span>'
            f'<span class="v">Ajustes</span></div></div>', unsafe_allow_html=True)
    elif entries:
        dim = "" if window_open else " stale"
        # Realce só quando a entrada em destaque REALMENTE mudou (ativo, direção
        # ou vela). Sem isso o cartão brilharia a cada rerun, que é o problema
        # que acabamos de tirar da tela.
        _chave = (entries[0]["a"]["name"], entries[0]["dir"], candle_key(minutes))
        novo_sinal = st.session_state.get("ultimo_sinal") != _chave
        st.session_state["ultimo_sinal"] = _chave
        _cls = f'hero{dim}{" novo" if novo_sinal else ""} '
        st.markdown(hero_html(entries[0], cvela).replace('class="hero ', f'class="{_cls}'),
                    unsafe_allow_html=True)
        rest = entries[1:]
        st.markdown(f'<div class="sect">Outras entradas · {len(entries)} no total</div>',
                    unsafe_allow_html=True)
        if rest:
            cards = "".join(card_html(e).replace('class="card ', f'class="card{dim} ') for e in rest)
            st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)
        else:
            st.caption("Esta é a única entrada no momento.")

    # ---- exposição correlacionada ----
    # Três pares com a mesma moeda não são três apostas independentes. COMPRA
    # EUR/USD e VENDA USD/JPY são, as duas, apostas contra o dólar: se o dólar
    # subir, as duas perdem juntas. Quem opera isso como diversificação está com
    # o triplo do risco achando que dividiu.
    if len(entries) > 1:
        _expo = {}
        for e in entries:
            a = next((x for x in ASSETS if x["name"] == e["a"]["name"]), None)
            if not a or a["type"] != "fx" or len(a["cur"]) != 2:
                continue
            base, cotada = a["cur"]
            sinal = 1 if e["dir"] == "COMPRA" else -1
            _expo[base] = _expo.get(base, 0) + sinal      # compra o par = compra a base
            _expo[cotada] = _expo.get(cotada, 0) - sinal  # e vende a cotada
        _conc = sorted(((m, v) for m, v in _expo.items() if abs(v) >= 2),
                       key=lambda kv: -abs(kv[1]))
        if _conc:
            _txt = " · ".join(
                f'<b>{"comprado" if v > 0 else "vendido"} em {m}</b> ×{abs(v)}'
                for m, v in _conc[:3])
            st.markdown(
                f'<div class="win alert"><span class="pt"></span><div class="msg">'
                f'<b>Exposição concentrada.</b> {_txt}. Essas entradas não são '
                f'independentes: elas ganham e perdem juntas conforme essa moeda se '
                f'mexe. Operar as {len(entries)} como se fossem apostas separadas '
                f'multiplica o risco em vez de diluí-lo.</div></div>',
                unsafe_allow_html=True)

    # ---- o que já foi REGISTRADO nesta vela ----
    # A aba Sinais mostra o resultado da varredura DE AGORA; o Histórico mostra o
    # que foi gravado na virada. Os dois podem divergir dentro da mesma vela: um
    # ativo pode ser bloqueado por dado vencido, uma fonte pode falhar, ou você
    # pode ter trocado de estratégia/mercado depois da entrada. Antes o sinal
    # simplesmente sumia da tela e continuava no histórico, sem explicação.
    _ck_atual = candle_key(minutes)
    _reg = [h for h in hist if h.get("ck") == _ck_atual and h.get("tf") == minutes]
    _mostrando = {(e["a"]["name"], e["dir"]) for e in entries}
    _sumidos = [h for h in _reg if (h["asset"], h["dir"]) not in _mostrando]
    if _reg:
        _linhas = "".join(
            f'<span class="regchip{" off" if (h["asset"], h["dir"]) not in _mostrando else ""}">'
            f'{h["asset"]} {"▲" if h["dir"] == "COMPRA" else "▼"}</span>'
            for h in _reg)
        _obs = (" · os apagados saíram da varredura depois de gravados "
                "(dado vencido, fonte trocada ou filtro alterado)" if _sumidos else "")
        st.markdown(
            f'<div class="regbar"><span class="k">Registrado na vela {cvela}</span>'
            f'{_linhas}<span class="obs">{_obs}</span></div>', unsafe_allow_html=True)
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
          </div>""", unsafe_allow_html=True)

    if audio_on:
        if entries:
            top = entries[0]
            ests = ", ".join(_short(s) for s in top["strats"])
            pl = "estratégias" if len(top["strats"]) > 1 else "estratégia"
            fala = (f"Entrada agora. {top['a']['voz']}. {top['dir']}. "
                    f"{pl} {ests}. Força {FL[top['force']].lower()}.")
        else:
            fala = ""
        # Só o MOTOR de fala fica aqui, invisível. O botão de ativar mudou para a
        # aba Ajustes: liberar o áudio é configuração, não parte da operação.
        # (O navegador exige um clique do usuário antes de permitir voz.)
        st.markdown('<div class="wheel-pass"></div>', unsafe_allow_html=True)
        html_box(f"""
        <script>
        var TF={int(TF)}, FALA={fala!r};
        function say(t){{try{{var u=new SpeechSynthesisUtterance(t);u.lang='pt-BR';u.rate=1.05;
          window.speechSynthesis.cancel();window.speechSynthesis.speak(u);}}catch(e){{}}}}
        (function(){{if(!FALA)return;if(window.parent.sessionStorage.getItem('voz')!=='1')return;
          var per=TF*60,n=Date.now()/1000,pos=n%per,c=Math.floor(n/per);
          if(pos<{ENTRY_WINDOW}&&window.parent.sessionStorage.getItem('dito')!=String(c)){{
            window.parent.sessionStorage.setItem('dito',String(c));say(FALA);}}}})();
        </script>""", height=1)

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
            desatual = cp["chave"] != (interval, len(scan_list),
                                       tuple(sorted(sel_strats)))
            q = f'calculado às {hm(cp["quando"])} em {cp["levou"]:.1f}s'
            st.caption(("⚠️ " + q + " — com outro timeframe/lista de ativos. Recalcule."
                        ) if desatual else q)
    if pedir:
        with st.spinner("Rodando o backtest…"):
            cp = get_perf(calcular=True)

    if cp is not None:
        perf = cp["dados"]["est"]
        horas = cp["dados"]["horas"]
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
        ver_hoje = st.toggle("Mostrar coluna “Hoje”", value=False, key="tg_hoje",
                             help="O recorte do dia costuma ter poucas dezenas de "
                                  "operações — quase sempre ruído. Fica oculto por padrão.")

        def linhas(nomes, com_rank=True):
            out = ""
            for i, name in enumerate(nomes, 1):
                p = perf[name]
                tag = ""
                if name == top:
                    tag = ('<span class="tagmini">VANTAGEM COMPROVADA</span>' if proven
                           else '<span class="tagmini">MAIOR TAXA · não comprovada</span>')
                td_hoje = f'<td>{cell(*p["hoje"])}</td>' if ver_hoje else ""
                rk = (f'<span class="rankn{" top" if i == 1 else ""}">{i}</span>'
                      if com_rank else "")
                out += (f'<tr class="{"on" if name in sel_strats else ""}">'
                        f'<td class="nm">{rk}{name}{tag}</td>{td_hoje}'
                        f'<td>{cell(*p["per"])}</td>{barra(*p["per"])}</tr>')
            return out

        def barra(n, w):
            """Barra horizontal com o breakeven marcado. Lê-se de relance de que
            lado da linha a estratégia está, sem precisar comparar números."""
            if n == 0:
                return '<td class="barcel"></td>'
            p = w / n * 100
            lo, hi = 35.0, 75.0                      # faixa útil do eixo
            pos = max(0.0, min(100.0, (p - lo) / (hi - lo) * 100))
            be_pos = max(0.0, min(100.0, (BE - lo) / (hi - lo) * 100))
            fraca = n < N_MIN
            cor = ("var(--mut)" if fraca
                   else ("var(--buy)" if p >= BE else "var(--sell)"))
            return (f'<td class="barcel"><div class="bar">'
                    f'<i class="fill" style="width:{pos:.1f}%;background:{cor};'
                    f'opacity:{".45" if fraca else "1"}"></i>'
                    f'<i class="be" style="left:{be_pos:.1f}%"></i></div></td>')

        cab = ('<tr><th>Estratégia</th>'
               + (f'<th>Hoje</th>' if ver_hoje else "")
               + f'<th>Período ({TF_PERIOD[interval]})</th>'
               + f'<th style="width:190px">Vs. breakeven</th></tr>')

        # Em uso primeiro, separadas do resto: é a informação que você consulta
        # antes de operar. O resto é catálogo.
        em_uso = [n for n in ranked if n in sel_strats]
        outras = [n for n in ranked if n not in sel_strats]

        st.markdown(f'<div class="sect">Em uso agora · {TF_LABEL[TF]} · payout {payout_lbl} '
                    f'· breakeven {BE:.2f}%</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl">{cab}{linhas(em_uso)}</table>', unsafe_allow_html=True)

        st.markdown('<div class="sect">Todas as estratégias · ordenado pela taxa do período'
                    '</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl">{cab}{linhas(outras)}</table>', unsafe_allow_html=True)

        # ---------- MELHORES HORÁRIOS ----------
        tot_h = sum(v[0] for v in horas.values())
        if tot_h >= 500:
            st.markdown(f'<div class="sect">Horários · {mercado.lower()} · estratégias '
                        f'em uso · horário de Brasília</div>', unsafe_allow_html=True)
            N_H = 150                      # mínimo por hora para a barra valer algo
            col = ""
            for h_ in range(24):
                n_, w_ = horas[h_]
                if n_ < N_H:
                    col += (f'<div class="hcol"><i class="vazio"></i>'
                            f'<span class="hh">{h_:02d}</span></div>')
                    continue
                p_ = w_ / n_ * 100
                _, lo_, hi_ = wilson_ci(w_, n_)
                acima = lo_ * 100 > BE      # IC inteiro acima: o critério de sempre
                cls_ = "bom" if acima else ("ruim" if p_ < BE else "neutro")
                alt = max(6, min(100, (p_ - 40) / 20 * 100))
                col += (f'<div class="hcol" title="{h_:02d}h · {p_:.1f}% em {n_} ops '
                        f'(IC95 {lo_*100:.0f}–{hi_*100:.0f}%)">'
                        f'<i class="{cls_}" style="height:{alt:.0f}%"></i>'
                        f'<span class="hh">{h_:02d}</span></div>')
            melhores = [(h_, horas[h_]) for h_ in range(24)
                        if horas[h_][0] >= N_H
                        and wilson_ci(horas[h_][1], horas[h_][0])[1] * 100 > BE]
            melhores.sort(key=lambda kv: -kv[1][1] / kv[1][0])
            if melhores:
                txt = " · ".join(f"<b>{h_:02d}h</b> {v[1]/v[0]*100:.1f}%"
                                 for h_, v in melhores[:4])
                cab_h = f'Horas cujo IC95 inteiro ficou acima do breakeven: {txt}'
            else:
                cab_h = ('<b>Nenhuma hora</b> teve o intervalo de confiança inteiro '
                         'acima do breakeven neste período.')
            st.markdown(f'<div class="horas"><div class="h-linha be"></div>{col}</div>'
                        f'<div class="note">{cab_h}<br><b>Cuidado com esta tabela.</b> '
                        f'São 24 horas testadas ao mesmo tempo: mesmo que nenhuma tenha '
                        f'vantagem real, é provável que uma ou duas pareçam boas por '
                        f'sorte. Use como hipótese a testar no forward test, não como '
                        f'regra — e desconfie se a hora "boa" mudar toda vez que você '
                        f'recalcular. A linha tracejada é o breakeven '
                        f'({BE:.1f}%); barras cinza têm menos de {N_H} operações.</div>',
                        unsafe_allow_html=True)

        # Exportação do backtest: o Histórico já exportava, o Desempenho não.
        _rows = []
        for nm_ in ranked:
            p_ = perf[nm_]
            n_, w_ = p_["per"]
            nh_, wh_ = p_["hoje"]
            _p, _lo, _hi = wilson_ci(w_, n_) if n_ else (float("nan"),) * 3
            _rows.append({
                "estrategia": nm_, "em_uso": nm_ in sel_strats,
                "timeframe_min": TF, "payout": PAYOUT, "breakeven_pct": round(BE, 2),
                "ops_periodo": n_, "acertos_periodo": w_,
                "taxa_periodo_pct": round(w_ / n_ * 100, 2) if n_ else None,
                "ic95_lo_pct": round(_lo * 100, 2) if n_ else None,
                "ic95_hi_pct": round(_hi * 100, 2) if n_ else None,
                "veredito": verdict(w_, n_, PAYOUT) if n_ else "sem dados",
                "ops_hoje": nh_, "acertos_hoje": wh_,
            })
        _df = pd.DataFrame(_rows)
        st.download_button(
            "Baixar backtest (CSV)",
            data=_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"kairo_backtest_{TF}min_{br(now).strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv", key="dl_perf")

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
        # ---- filtros PRIMEIRO ----
        # Eles definem `vis`, e TODO o resto da aba — cartões, curva, tabelas —
        # é calculado sobre `vis`. Antes só a tabela final respeitava o filtro,
        # então os números do topo diziam uma coisa e a lista embaixo outra.
        TIPO_ATIVO = {a["name"]: a["type"] for a in ASSETS}

        def payout_do(itens):
            """
            BUG CORRIGIDO: o payout por ativo era gravado em cada sinal mas as
            estatísticas usavam sempre o payout global. Com 80% num par e 90%
            noutro, o breakeven do recorte não é nenhum dos dois — e o veredito
            saía comparando a taxa contra a linha errada.
            """
            ps = [h.get("payout") for h in itens
                  if isinstance(h.get("payout"), (int, float))]
            return (sum(ps) / len(ps)) if ps else PAYOUT
        f1, f2, f3, f4 = st.columns([2, 1.4, 1, 1])
        with f1:
            todas_est = sorted({s for h in hist for s in h["strats"]})
            f_est = st.multiselect("Estratégia", todas_est, default=[],
                                   placeholder="Todas as estratégias")
        with f2:
            f_res = st.multiselect("Resultado",
                                   ["ganhou", "perdeu", "empate", "aguardando"], default=[],
                                   placeholder="Todos os resultados")
        with f3:
            # Misturar 1min e 5min numa taxa só é comparar coisas diferentes.
            tfs = sorted({h.get("tf") for h in hist if h.get("tf")})
            f_tf = st.multiselect("Timeframe", tfs, default=[],
                                  format_func=lambda v: f"{v} min",
                                  placeholder="Todos")
        with f4:
            f_mkt = st.multiselect("Mercado", ["fx", "crypto"], default=[],
                                   format_func=lambda v: "Forex" if v == "fx" else "Cripto",
                                   placeholder="Tudo")
        vis = [h for h in hist
               if (not f_est or any(x in f_est for x in h["strats"]))
               and (not f_res or (h["res"] or "aguardando") in f_res)
               and (not f_tf or h.get("tf") in f_tf)
               and (not f_mkt or TIPO_ATIVO.get(h["asset"]) in f_mkt)]
        filtrado = len(vis) != len(hist)
        if filtrado:
            st.caption(f"Filtro ativo: {len(vis)} de {len(hist)} sinais. "
                       f"Todos os números abaixo consideram apenas esse recorte.")
        if not vis:
            # Nada de st.stop() aqui: ele encerraria o script inteiro e levaria
            # junto o rodapé. As seções abaixo já degradam bem com lista vazia.
            st.info("Nenhum sinal atende aos filtros selecionados.")

        fechados = [h for h in vis if h["res"] in ("ganhou", "perdeu")]
        g = sum(1 for h in fechados if h["res"] == "ganhou")
        emp = sum(1 for h in vis if h["res"] == "empate")
        abertos = sum(1 for h in vis if h["res"] is None)
        taxa = (g / len(fechados) * 100) if fechados else float("nan")
        # Cartões próprios em vez de st.metric: o componente padrão do Streamlit
        # tem outra tipografia e destoa do resto da interface.
        if fechados:
            p, lo, hi = wilson_ci(g, len(fechados))
            _pay_amostra = payout_do(fechados)
            v = verdict(g, len(fechados), _pay_amostra)
            txt = {"acima": "acima do breakeven", "abaixo": "abaixo do breakeven",
                   "inconclusivo": "não conclusivo", "sem dados": "sem dados"}[v]
            tcls = {"acima": "good", "abaixo": "bad"}.get(v, "mid")
            _be_amostra = breakeven(_pay_amostra) * 100
            taxa_txt = f'{taxa:.1f}%'
            sub = (f'IC95 {lo*100:.0f}–{hi*100:.0f}% · {txt} '
                   f'(breakeven {_be_amostra:.1f}%)')
        else:
            tcls, taxa_txt, sub = "mid", "—", "nenhuma operação resolvida ainda"

        def stat(rot, val, extra="", cls=""):
            return (f'<div class="stat"><span class="k">{rot}</span>'
                    f'<span class="v {cls}">{val}</span>'
                    f'<span class="x">{extra}</span></div>')

        st.markdown(
            '<div class="statrow">'
            + stat("Sinais registrados", len(vis), f"{abertos} aguardando fechar")
            + stat("Resolvidos", len(fechados), f"{emp} empate(s) devolvido(s)")
            + stat("Acertos", g, f"de {len(fechados)} operações")
            + stat("Taxa do forward test", taxa_txt, sub, tcls)
            + '</div>', unsafe_allow_html=True)
        st.caption(f"Breakeven {BE:.2f}% com payout {payout_lbl}.")

        # ---- pendentes que o app não consegue apurar agora ----
        # A apuração lê a vela de entrada nos dados carregados. Se o sinal é de
        # outro timeframe ou de um ativo fora da varredura atual, esses dados não
        # estão em memória e ele fica "aguardando" para sempre — sem nenhum aviso.
        _varridos = {a["name"] for a in scan_list}
        _presos = [h for h in hist if h["res"] is None
                   and (h.get("tf") != minutes or h["asset"] not in _varridos)]
        if _presos:
            _tfs_p = sorted({h.get("tf") for h in _presos if h.get("tf")})
            _ats_p = sorted({h["asset"] for h in _presos})
            st.markdown(
                f'<div class="win alert"><span class="pt"></span><div class="msg">'
                f'<b>{len(_presos)} sinal(is) sem apuração.</b> São de timeframe '
                f'({", ".join(f"{t} min" for t in _tfs_p)}) ou de ativos '
                f'({", ".join(_ats_p[:4])}{"…" if len(_ats_p) > 4 else ""}) que não '
                f'estão na varredura atual — e o app só consegue apurar o resultado '
                f'com os dados carregados. Para resolvê-los, volte ao timeframe e ao '
                f'mercado correspondentes por alguns instantes.</div></div>',
                unsafe_allow_html=True)

        # ---- curva de capital, drawdown e referência aleatória ----
        # A taxa de acerto esconde o que quebra conta: duas estratégias com 54%
        # podem ter sequências de perda muito diferentes, e é a sequência que
        # determina o tamanho da banca necessária.
        _cap = [h for h in sorted(vis, key=lambda x: x["ts"])
                if h["res"] in ("ganhou", "perdeu", "empate") and h.get("stake")]
        if len(_cap) >= 8:
            eq, pico, ddmax, ddmax_pct = [], 0.0, 0.0, 0.0
            acc = 0.0
            for h in _cap:
                acc += pnl_de(h)
                eq.append(acc)
                pico = max(pico, acc)
                dd = pico - acc
                if dd > ddmax:
                    ddmax = dd
                    ddmax_pct = (dd / pico * 100) if pico > 0 else 0.0
            # referência: mesma sequência de apostas com 50% de acerto (moeda)
            _pm = sum(float(h.get("payout") or .8) for h in _cap) / len(_cap)
            _sm = sum(float(h.get("stake") or 0) for h in _cap) / len(_cap)
            _passo = _sm * (0.5 * _pm - 0.5)          # esperança por operação
            ref = [_passo * (i + 1) for i in range(len(_cap))]

            _todos = eq + ref
            _lo, _hi = min(_todos + [0.0]), max(_todos + [0.0])
            _rng = max(_hi - _lo, 1e-9)
            H2 = 40.0

            def _y2(v):
                return H2 - (v - _lo) / _rng * H2

            _p1 = " ".join(f"{i/(len(eq)-1)*100:.2f},{_y2(v):.2f}"
                           for i, v in enumerate(eq))
            _p2 = " ".join(f"{i/(len(ref)-1)*100:.2f},{_y2(v):.2f}"
                           for i, v in enumerate(ref))
            _cor = "var(--buy)" if eq[-1] >= 0 else "var(--sell)"
            st.markdown(
                f'<div class="curva"><div class="c-head">'
                f'<span class="k">Resultado acumulado · valor por entrada gravado</span>'
                f'<span class="lg"><i class="be"></i>moeda (50%)</span></div>'
                f'<svg viewBox="0 0 100 {H2}" preserveAspectRatio="none">'
                f'<line x1="0" y1="{_y2(0):.2f}" x2="100" y2="{_y2(0):.2f}" '
                f'stroke="var(--line2)" stroke-width=".3"/>'
                f'<polyline points="{_p2}" fill="none" stroke="var(--warn)" '
                f'stroke-width=".7" stroke-dasharray="2 2" vector-effect="non-scaling-stroke" '
                f'opacity=".8"/>'
                f'<polyline points="{_p1}" fill="none" stroke="{_cor}" stroke-width="1" '
                f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>'
                f'<div class="c-foot"><span>{len(_cap)} operações</span>'
                f'<span class="mono">resultado {eq[-1]:+.2f} · '
                f'pior queda {ddmax:.2f}'
                f'{f" ({ddmax_pct:.0f}% do pico)" if ddmax_pct else ""}</span></div></div>'
                f'<div class="note">A linha tracejada é o que uma <b>moeda</b> renderia '
                f'nas mesmas apostas: com payout médio de {_pm*100:.0f}%, acertar metade '
                f'das vezes dá prejuízo constante. Estar acima dela não significa ter '
                f'vantagem — significa apenas não estar perdendo no ritmo do acaso. '
                f'<b>Pior queda</b> é a maior distância entre um pico e o vale seguinte: '
                f'é ela, não a taxa de acerto, que define o tamanho de banca necessário.'
                f'</div>', unsafe_allow_html=True)

        # ---- aviso de sequência ruim ----
        # Não prova nada sobre a estratégia: sequências ruins acontecem mesmo com
        # 55% de acerto. O aviso existe porque este é o momento em que se costuma
        # dobrar a aposta para recuperar — e é aí que uma perda administrável
        # vira uma perda grande.
        ult = [h for h in sorted(vis, key=lambda x: x["ts"])
               if h["res"] in ("ganhou", "perdeu")][-12:]
        if len(ult) >= 8:
            perdas = sum(1 for h in ult if h["res"] == "perdeu")
            seguidas = 0
            for h in reversed(ult):
                if h["res"] == "perdeu":
                    seguidas += 1
                else:
                    break
            if seguidas >= 4 or perdas / len(ult) >= 0.7:
                det = (f"{seguidas} perdas seguidas" if seguidas >= 4
                       else f"{perdas} perdas nas últimas {len(ult)}")
                st.markdown(
                    f'<div class="win alert"><span class="pt"></span><div class="msg">'
                    f'<b>Sequência ruim — {det}.</b> Isso acontece mesmo em estratégias '
                    f'com vantagem: com 55% de acerto, 4 perdas seguidas ocorrem a cada '
                    f'~25 sequências. O risco aqui não é estatístico, é de decisão — '
                    f'aumentar a aposta para recuperar é o que transforma uma perda '
                    f'administrável em uma perda grande. Se for parar, pare por ter '
                    f'decidido antes, não por estar perdendo agora.</div></div>',
                    unsafe_allow_html=True)

        # ---- curva da taxa acumulada ----
        # Mostra se o número é estável ou fruto de uma sequência. Uma taxa que
        # sobe e desce muito com a amostra crescendo é sinal de que ainda não há
        # informação suficiente, por mais bonito que esteja o valor final.
        seq = [h for h in sorted(vis, key=lambda x: x["ts"])
               if h["res"] in ("ganhou", "perdeu")]
        if len(seq) >= 8:
            acum, w = [], 0
            for i, h in enumerate(seq, 1):
                w += h["res"] == "ganhou"
                acum.append(w / i * 100)
            W, H, PADL = 100.0, 34.0, 0.0
            lo_y = min(min(acum), BE) - 4
            hi_y = max(max(acum), BE) + 4
            rng = max(hi_y - lo_y, 1e-6)

            def _y(v):
                return H - (v - lo_y) / rng * H

            pts = " ".join(f"{PADL + i / (len(acum) - 1) * W:.2f},{_y(v):.2f}"
                           for i, v in enumerate(acum))
            y_be = _y(BE)
            cor = "var(--buy)" if acum[-1] >= BE else "var(--sell)"
            st.markdown(
                f'<div class="curva"><div class="c-head">'
                f'<span class="k">Taxa acumulada ao longo das operações</span>'
                f'<span class="lg"><i class="be"></i>breakeven {BE:.1f}%</span></div>'
                f'<svg viewBox="0 0 100 {H}" preserveAspectRatio="none">'
                f'<line x1="0" y1="{y_be:.2f}" x2="100" y2="{y_be:.2f}" '
                f'stroke="var(--warn)" stroke-width=".4" stroke-dasharray="2 2" opacity=".8"/>'
                f'<polyline points="{pts}" fill="none" stroke="{cor}" stroke-width=".9" '
                f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>'
                f'<div class="c-foot"><span>1ª op</span>'
                f'<span class="mono">{acum[-1]:.1f}% em {len(acum)} ops</span></div></div>',
                unsafe_allow_html=True)


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
        for h in vis:
            for s in h["strats"]:
                por_est.setdefault(s, []).append(h)
        if por_est:
            def _taxa(itens):
                f = [h for h in itens if h["res"] in ("ganhou", "perdeu")]
                if not f:
                    return (-1.0, 0)          # sem dados vai para o fim
                w = sum(1 for h in f if h["res"] == "ganhou")
                return (w / len(f), len(f))   # desempate pelo tamanho da amostra

            linhas = "".join(
                linha_ic(s, "estratégia", v, payout_do(v))
                for s, v in sorted(por_est.items(), key=lambda kv: _taxa(kv[1]),
                                   reverse=True))
            st.markdown('<div class="sect">Forward test por estratégia</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Tipo</th><th>Ops</th>'
                        f'<th>Acerto</th><th>IC95</th><th>Veredito</th></tr>{linhas}</table>',
                        unsafe_allow_html=True)

        # ---- dado fresco x dado atrasado: a pergunta que a instrumentação responde ----
        com_lag = [h for h in vis if isinstance(h.get("lag"), (int, float))
                   and math.isfinite(h["lag"])]
        if com_lag:
            corte = max(1.0, float(minutes))
            fresco = [h for h in com_lag if h["lag"] <= corte]
            velho = [h for h in com_lag if h["lag"] > corte]
            linhas = (linha_ic(f"Atraso ≤ {corte:.0f} min", "dado fresco", fresco,
                               payout_do(fresco))
                      + linha_ic(f"Atraso > {corte:.0f} min", "dado atrasado", velho,
                                 payout_do(velho)))
            st.markdown('<div class="sect">Efeito do atraso dos dados</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Tipo</th><th>Ops</th>'
                        f'<th>Acerto</th><th>IC95</th><th>Veredito</th></tr>{linhas}</table>',
                        unsafe_allow_html=True)
            st.caption("Só ganha sentido com algumas centenas de operações. Até lá os "
                       "intervalos vão ficar largos e o veredito, não conclusivo — isso é "
                       "o esperado, não um defeito.")

        # Agrupado por dia, com subtotal em cada cabeçalho. A taxa agregada
        # esconde dias sistematicamente ruins; separando, isso fica visível.
        VERD = {"ganhou": ("v-good", "ganhou"), "perdeu": ("v-bad", "perdeu"),
                "empate": ("v-mid", "empate"), None: ("v-mid", "aguardando")}
        por_dia = {}
        for h in sorted(vis, key=lambda x: x["ts"], reverse=True)[:200]:
            por_dia.setdefault(br(h["ts"]).strftime("%d/%m/%Y"), []).append(h)

        rows = ""
        for dia_lbl, itens in por_dia.items():
            f_ = [x for x in itens if x["res"] in ("ganhou", "perdeu")]
            w_ = sum(1 for x in f_ if x["res"] == "ganhou")
            if f_:
                tx = w_ / len(f_) * 100
                cls_ = "good" if tx >= BE else "bad"
                sub = (f'<span class="{cls_} mono" style="font-weight:700">{tx:.1f}%</span>'
                       f'<span class="n"> · {w_}/{len(f_)} resolvidos</span>')
            else:
                sub = '<span class="n">nenhuma resolvida</span>'
            rows += (f'<tr class="daysep"><td colspan="7">'
                     f'<span class="dlbl">{dia_lbl}</span>{sub}</td></tr>')
            for h in itens:
                vc, vt = VERD.get(h["res"], ("v-mid", "aguardando"))
                r = f'<span class="verd {vc}">{vt}</span>'
                dcls = "good" if h["dir"] == "COMPRA" else "bad"
                arw = "▲" if h["dir"] == "COMPRA" else "▼"
                chips_h = "".join(f'<span class="sc">{x}</span>' for x in h["strats"])
                _lg = h.get("lag")
                lag_txt = (f'{_lg:.1f}min' if isinstance(_lg, (int, float))
                           and math.isfinite(_lg) else "—")
                rows += (f'<tr><td class="n">{hm(h["ts"])}</td>'
                         f'<td class="nm">{h["asset"]}</td>'
                         f'<td class="n mono">{h.get("tf", "—")}m</td>'
                         f'<td class="{dcls}" style="font-weight:800">{arw} {h["dir"]}</td>'
                         f'<td>{chips_h}</td>'
                         f'<td class="n mono">{lag_txt}</td><td>{r}</td></tr>')
        st.markdown(f'<div class="sect">Sinais registrados · {len(vis)} de {len(hist)}</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl"><tr><th>Hora</th><th>Ativo</th><th>TF</th>'
                    f'<th>Direção</th><th>Estratégias</th><th>Atraso</th>'
                    f'<th>Resultado</th></tr>{rows}</table>',
                    unsafe_allow_html=True)
        st.markdown('<div class="note"><b>Este é o teste que vale.</b> Aqui não há backtest nem '
                    'seleção de período: são os sinais que o app realmente emitiu, apurados pela '
                    'cor da vela em que a entrada valeria. É a amostra mais honesta que existe — '
                    'e a única livre de garimpo de dados.</div>', unsafe_allow_html=True)

    if HIST_REMOTO:
        err = _gist_err[0]
        if err:
            st.markdown(f'<div class="win alert"><span class="pt"></span><div class="msg">'
                        f'<b>Falha ao sincronizar</b> — {err}. O histórico está só no disco '
                        f'do container e se perde no próximo rebuild.</div></div>',
                        unsafe_allow_html=True)
        else:
            st.markdown('<div class="note"><b>Histórico sincronizado.</b> Está espelhado no '
                        'seu Gist privado, então sobrevive aos reinícios do Streamlit Cloud.'
                        '</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="win alert"><span class="pt"></span><div class="msg">'
            '<b>Este histórico é temporário.</b> O disco do Streamlit Cloud é apagado a cada '
            'rebuild do app, então tudo abaixo some junto — e sem amostra acumulada o forward '
            'test nunca sai de "não conclusivo".<br>'
            'Para preservar, adicione nos Secrets do app: <code>GITHUB_TOKEN</code> '
            '(token do GitHub com escopo <code>gist</code>) e <code>GIST_ID</code> '
            '(de um Gist privado contendo o arquivo <code>sinais_historico.json</code>).<br>'
            'Enquanto isso, baixe o CSV abaixo com frequência.</div></div>',
            unsafe_allow_html=True)

    if hist:
        # ---- marcação do que foi realmente executado ----
        # O app registra tudo que sinalizou, mas ninguém opera todos os sinais.
        # Marcando o que você executou de fato, o forward test passa a medir a
        # SUA operação, não a sugestão do app — que é a única medida que importa
        # para decidir se vale continuar.
        st.markdown('<div class="sect">O que você executou de fato</div>',
                    unsafe_allow_html=True)
        _recentes = sorted(vis, key=lambda x: x["ts"], reverse=True)[:25]
        _tab = pd.DataFrame([{
            "id": f'{h["asset"]}|{h["dir"]}|{h.get("ck")}|{h.get("tf")}',
            "Executei": bool(h.get("exec", False)),
            "Quando": dhm(h["ts"]), "Ativo": h["asset"], "Direção": h["dir"],
            "TF": f'{h.get("tf", "—")}m',
            "Estratégias": "+".join(h["strats"]),
            "Resultado": h["res"] or "aguardando",
        } for h in _recentes])
        _ed = st.data_editor(
            _tab, hide_index=True, width="stretch", key="ed_exec",
            column_config={
                "id": None,
                "Executei": st.column_config.CheckboxColumn(
                    "Executei", help="Marque as entradas que você realmente operou.",
                    width="small"),
            },
            disabled=["Quando", "Ativo", "Direção", "TF", "Estratégias", "Resultado"])
        _mudou = False
        _mapa = dict(zip(_ed["id"], _ed["Executei"]))
        for h in hist:
            k = f'{h["asset"]}|{h["dir"]}|{h.get("ck")}|{h.get("tf")}'
            if k in _mapa and bool(h.get("exec", False)) != bool(_mapa[k]):
                h["exec"] = bool(_mapa[k]); _mudou = True
        if _mudou:
            hist_save(hist)
            st.rerun()

        _exec = [h for h in vis if h.get("exec")
                 and h["res"] in ("ganhou", "perdeu")]
        if _exec:
            _we = sum(1 for h in _exec if h["res"] == "ganhou")
            _pe, _loe, _hie = wilson_ci(_we, len(_exec))
            _pe_pay = payout_do(_exec)
            _ve = verdict(_we, len(_exec), _pe_pay)
            _tv = {"acima": "acima do breakeven", "abaixo": "abaixo do breakeven",
                   "inconclusivo": "não conclusivo", "sem dados": "sem dados"}[_ve]
            st.markdown(
                f'<div class="note"><b>Só o que você executou:</b> '
                f'{_we}/{len(_exec)} = <b>{_pe*100:.1f}%</b> · IC95 '
                f'{_loe*100:.0f}–{_hie*100:.0f}% · {_tv}. Esta é a medida da sua '
                f'operação; a taxa geral acima inclui sinais que você não pegou.</div>',
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
