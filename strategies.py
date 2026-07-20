"""
strategies.py — Estratégias e backtest (código PURO, sem Streamlit).

O MESMO código roda ao vivo (scanner) e no backtest, então não existe diferença
entre o que é testado e o que é operado.

Cada estratégia devolve um SCORE por barra, em [-1, 1]:
    score > 0 -> viés de COMPRA · score < 0 -> viés de VENDA · 0 -> sem entrada
A magnitude vira a força: >=0.80 FORTE · >=0.60 MÉDIA · >=MIN_SCORE FRACA.

SEM LOOK-AHEAD: o score da barra i usa apenas dados até o fechamento da barra i.
A entrada acontece na ABERTURA da barra i+1 e o acerto é pela COR dessa vela:
    COMPRA vence se close(i+1) > open(i+1)  (vela verde)
    VENDA  vence se close(i+1) < open(i+1)  (vela vermelha)
Empate (close == open) conta como derrota e é reportado à parte.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_SCORE = 0.50          # abaixo disso não é entrada
HIGHER_RULE = {"1m": "5min", "5m": "15min", "15m": "60min"}


# ----------------------------------------------------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    c, o, h, l = d["Close"], d["Open"], d["High"], d["Low"]
    d["ema9"] = c.ewm(span=9, adjust=False).mean()
    d["ema21"] = c.ewm(span=21, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d["rsi"] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    d["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    d["bb_mid"], d["bb_up"], d["bb_low"] = mid, mid + 2 * sd, mid - 2 * sd
    d["body"] = (c - o).abs()
    d["rng"] = (h - l)
    d["avg_body"] = d["body"].rolling(20).mean()
    d["upper_wick"] = h - np.maximum(c, o)
    d["lower_wick"] = np.minimum(c, o) - l
    d["bull"] = c > o
    d["bear"] = c < o
    return d


def _norm(series: pd.Series, ref: pd.Series) -> pd.Series:
    """Razão normalizada em [0,1] (quanto a série se destaca da sua média)."""
    r = (series.abs() / ref.replace(0, np.nan)).clip(0, 2) / 2
    return r.fillna(0)


# ----------------------------------------------------------------------
# A · TENDÊNCIA — alinhamento de EMAs + MACD do mesmo lado
def strat_tendencia(d: pd.DataFrame) -> pd.Series:
    up = (d["ema9"] > d["ema21"]) & (d["ema21"] > d["ema50"]) & (d["macd_hist"] > 0) & (d["Close"] > d["ema9"])
    dn = (d["ema9"] < d["ema21"]) & (d["ema21"] < d["ema50"]) & (d["macd_hist"] < 0) & (d["Close"] < d["ema9"])
    conf = _norm(d["macd_hist"], d["macd_hist"].abs().rolling(20).mean())
    score = pd.Series(0.0, index=d.index)
    score[up] = 0.55 + 0.45 * conf[up]
    score[dn] = -(0.55 + 0.45 * conf[dn])
    return score.fillna(0.0)


# B · REVERSÃO — RSI em extremo + fora da Bollinger + candle de rejeição
def strat_reversao(d: pd.DataFrame) -> pd.Series:
    buy = (d["rsi"] < 30) & (d["Close"] <= d["bb_low"]) & (d["bull"] | (d["lower_wick"] >= d["body"]))
    sell = (d["rsi"] > 70) & (d["Close"] >= d["bb_up"]) & (d["bear"] | (d["upper_wick"] >= d["body"]))
    ext_b = ((30 - d["rsi"]).clip(0, 20) / 20)
    ext_s = ((d["rsi"] - 70).clip(0, 20) / 20)
    score = pd.Series(0.0, index=d.index)
    score[buy] = 0.55 + 0.45 * ext_b[buy]
    score[sell] = -(0.55 + 0.45 * ext_s[sell])
    return score.fillna(0.0)


# C · ROMPIMENTO — fecha além da máx/mín de 20 barras com corpo expandido
def strat_rompimento(d: pd.DataFrame) -> pd.Series:
    hh = d["High"].rolling(20).max().shift(1)      # shift(1): exclui a barra atual
    ll = d["Low"].rolling(20).min().shift(1)
    ratio = (d["body"] / d["avg_body"].replace(0, np.nan)).fillna(0)
    up = (d["Close"] > hh) & (ratio >= 1.2)
    dn = (d["Close"] < ll) & (ratio >= 1.2)
    mag = (ratio / 2.5).clip(0, 1)
    score = pd.Series(0.0, index=d.index)
    score[up] = 0.55 + 0.45 * mag[up]
    score[dn] = -(0.55 + 0.45 * mag[dn])
    return score.fillna(0.0)


# D · CONFLUÊNCIA MULTI-TF — direção do TF base concorda com o TF maior
def _trend_dir(d: pd.DataFrame) -> pd.Series:
    up = (d["ema9"] > d["ema21"]) & (d["macd_hist"] > 0)
    dn = (d["ema9"] < d["ema21"]) & (d["macd_hist"] < 0)
    s = pd.Series(0.0, index=d.index)
    s[up] = 1.0
    s[dn] = -1.0
    return s


def strat_confluencia(d: pd.DataFrame, tf: str) -> pd.Series:
    base = _trend_dir(d)
    rule = HIGHER_RULE.get(tf)
    if rule is None or not isinstance(d.index, pd.DatetimeIndex):
        return pd.Series(0.0, index=d.index)
    hi = d[["Open", "High", "Low", "Close"]].resample(rule).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    if len(hi) < 30:
        return pd.Series(0.0, index=d.index)
    hi = add_indicators(hi)
    hdir = _trend_dir(hi).shift(1)               # shift(1): só barra maior JÁ FECHADA
    hdir = hdir.reindex(d.index, method="ffill").fillna(0.0)
    agree_up = (base > 0) & (hdir > 0)
    agree_dn = (base < 0) & (hdir < 0)
    conf = _norm(d["macd_hist"], d["macd_hist"].abs().rolling(20).mean())
    score = pd.Series(0.0, index=d.index)
    score[agree_up] = 0.60 + 0.40 * conf[agree_up]
    score[agree_dn] = -(0.60 + 0.40 * conf[agree_dn])
    return score.fillna(0.0)


STRATEGIES = {
    "A · Tendência": strat_tendencia,
    "B · Reversão": strat_reversao,
    "C · Rompimento": strat_rompimento,
    "D · Confluência multi-TF": strat_confluencia,
}
NEEDS_TF = {"D · Confluência multi-TF"}


def score_of(name: str, d: pd.DataFrame, tf: str) -> pd.Series:
    fn = STRATEGIES[name]
    return fn(d, tf) if name in NEEDS_TF else fn(d)


def classify(score: float):
    """score -> (direção, força) ou None se não houver entrada."""
    if score is None or not np.isfinite(score) or abs(score) < MIN_SCORE:
        return None
    d = "COMPRA" if score > 0 else "VENDA"
    m = abs(score)
    f = "FORTE" if m >= 0.80 else ("MEDIA" if m >= 0.60 else "FRACA")
    return (d, f)


# ----------------------------------------------------------------------
def backtest(d: pd.DataFrame, score: pd.Series) -> dict:
    """
    Entrada na ABERTURA da barra seguinte; acerto pela COR dessa vela.
    Retorna trades, wins, ties, win_rate.
    """
    o_next = d["Open"].shift(-1)
    c_next = d["Close"].shift(-1)
    sig = score.where(score.abs() >= MIN_SCORE, 0.0)
    valid = (sig != 0) & o_next.notna() & c_next.notna()
    if not valid.any():
        return {"trades": 0, "wins": 0, "ties": 0, "win_rate": float("nan")}
    up = c_next > o_next
    dn = c_next < o_next
    tie = c_next == o_next
    win = np.where(sig > 0, up, dn)
    n = int(valid.sum())
    w = int((pd.Series(win, index=d.index) & valid).sum())
    t = int((tie & valid).sum())
    return {"trades": n, "wins": w, "ties": t, "win_rate": w / n if n else float("nan")}


def backtest_all(d: pd.DataFrame, tf: str) -> dict:
    """Roda todas as estratégias no mesmo DataFrame."""
    d = add_indicators(d)
    return {name: backtest(d, score_of(name, d, tf)) for name in STRATEGIES}
