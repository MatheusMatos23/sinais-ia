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

import math

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
    # ADX(14) — força de tendência. Baixo = mercado lateral (favorável a reversão).
    up_m, dn_m = h.diff(), -l.diff()
    plus = pd.Series(np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0), index=d.index)
    minus = pd.Series(np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0), index=d.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1/14, adjust=False).mean()
    pdi = 100 * plus.ewm(alpha=1/14, adjust=False).mean() / atr14.replace(0, np.nan)
    mdi = 100 * minus.ewm(alpha=1/14, adjust=False).mean() / atr14.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    d["adx"] = dx.ewm(alpha=1/14, adjust=False).mean().fillna(50)
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


# E · FADE DE ROMPIMENTO — opera CONTRA o rompimento (rompimentos curtos falham
# com frequência). É a única hipótese que sobreviveu a treino/teste out-of-sample
# no EUR/USD 5min (55,5% no ano, 11 de 13 meses acima do breakeven de payout 90%).
# Aviso: NÃO se confirmou em 1 min, onde a amostra disponível apontou o contrário.
def strat_fade_rompimento(d: pd.DataFrame) -> pd.Series:
    return -strat_rompimento(d)


# F · EXAUSTÃO — após N velas seguidas no mesmo sentido, opera CONTRA.
def strat_exaustao(d: pd.DataFrame, n: int = 3) -> pd.Series:
    up_streak = d["bull"].rolling(n).sum() == n
    dn_streak = d["bear"].rolling(n).sum() == n
    move = (d["Close"] - d["Close"].shift(n)).abs() / d["avg_body"].replace(0, np.nan)
    mag = (move / 4).clip(0, 1).fillna(0)
    score = pd.Series(0.0, index=d.index)
    score[up_streak] = -(0.55 + 0.45 * mag[up_streak])
    score[dn_streak] = 0.55 + 0.45 * mag[dn_streak]
    return score.fillna(0.0)


# G · FADE DE VELA EXTREMA — vela com range >= 2x a média: opera contra o sentido dela.
def strat_fade_extremo(d: pd.DataFrame) -> pd.Series:
    ratio = (d["rng"] / d["rng"].rolling(20).mean().replace(0, np.nan)).fillna(0)
    big = ratio >= 2.0
    up, dn = big & d["bull"], big & d["bear"]
    mag = ((ratio - 2) / 2).clip(0, 1).fillna(0)
    score = pd.Series(0.0, index=d.index)
    score[up] = -(0.55 + 0.45 * mag[up])
    score[dn] = 0.55 + 0.45 * mag[dn]
    return score.fillna(0.0)


# H · Z-SCORE — preço muito longe da média de 20: aposta na volta (sem exigir RSI extremo).
def strat_zscore(d: pd.DataFrame, z: float = 1.8) -> pd.Series:
    sd = d["Close"].rolling(20).std(ddof=0).replace(0, np.nan)
    zs = ((d["Close"] - d["bb_mid"]) / sd).fillna(0)
    lo, hi = zs <= -z, zs >= z
    mag = ((zs.abs() - z) / 1.5).clip(0, 1).fillna(0)
    score = pd.Series(0.0, index=d.index)
    score[lo] = 0.55 + 0.45 * mag[lo]
    score[hi] = -(0.55 + 0.45 * mag[hi])
    return score.fillna(0.0)


# ---- Refinamentos (promovidos para validação independente ao vivo) ----
# I · G apenas em mercado lateral (ADX<25). J · Z-score mais exigente (z>=2.2).
# K · G e H concordando + lateral (o mais restritivo — e o mais suspeito de
#     estar superajustado: foi o melhor no teste do EUR/USD, 62,4%).
def strat_fade_lateral(d: pd.DataFrame) -> pd.Series:
    return strat_fade_extremo(d).where(d["adx"] < 25, 0.0)


def strat_zscore_forte(d: pd.DataFrame) -> pd.Series:
    return strat_zscore(d, z=2.2)


def strat_reversao_dupla(d: pd.DataFrame) -> pd.Series:
    g, h = strat_fade_extremo(d), strat_zscore(d)
    lateral = d["adx"] < 25
    score = pd.Series(0.0, index=d.index)
    score[(g > 0) & (h > 0) & lateral] = 0.85
    score[(g < 0) & (h < 0) & lateral] = -0.85
    return score


STRATEGIES = {
    "A · Tendência": strat_tendencia,
    "B · Reversão": strat_reversao,
    "C · Rompimento": strat_rompimento,
    "D · Confluência multi-TF": strat_confluencia,
    "E · Fade de rompimento": strat_fade_rompimento,
    "F · Exaustão": strat_exaustao,
    "G · Fade vela extrema": strat_fade_extremo,
    "H · Z-score reversão": strat_zscore,
    "I · Fade extremo lateral": strat_fade_lateral,
    "J · Z-score forte": strat_zscore_forte,
    "K · Reversão dupla": strat_reversao_dupla,
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
def backtest(d: pd.DataFrame, score: pd.Series, tie_mode: str = "refund") -> dict:
    """
    Entrada na ABERTURA da barra seguinte; acerto pela COR dessa vela.

    tie_mode:
      "refund" (padrão) — vela que fecha igual à abertura devolve a aposta:
                          o empate NÃO entra no cálculo da taxa de acerto.
                          É como a maioria das corretoras trata o empate.
      "loss"            — empate conta como derrota (mais conservador).

    Retorna trades (avaliados), wins, ties, win_rate e trades_brutos.
    """
    o_next = d["Open"].shift(-1)
    c_next = d["Close"].shift(-1)
    sig = score.where(score.abs() >= MIN_SCORE, 0.0)
    valid = (sig != 0) & o_next.notna() & c_next.notna()
    if not valid.any():
        return {"trades": 0, "wins": 0, "ties": 0, "raw": 0, "win_rate": float("nan")}
    up = c_next > o_next
    dn = c_next < o_next
    tie = c_next == o_next
    win = pd.Series(np.where(sig > 0, up, dn), index=d.index)
    raw = int(valid.sum())
    t = int((tie & valid).sum())
    w = int((win & valid).sum())
    n = raw - t if tie_mode == "refund" else raw
    return {"trades": n, "wins": w, "ties": t, "raw": raw,
            "win_rate": (w / n) if n else float("nan")}


def backtest_all(d: pd.DataFrame, tf: str) -> dict:
    """Roda todas as estratégias no mesmo DataFrame."""
    d = add_indicators(d)
    return {name: backtest(d, score_of(name, d, tf)) for name in STRATEGIES}


# ----------------------------------------------------------------------
# Estatística: intervalo de confiança e veredito contra o breakeven
# ----------------------------------------------------------------------
def wilson_ci(wins: int, trades: int, z: float = 1.96):
    """
    Intervalo de Wilson 95% (mais correto que a normal em amostras pequenas).
    Retorna (taxa, limite_inferior, limite_superior) em fração 0..1.
    """
    if not trades:
        return (float("nan"), float("nan"), float("nan"))
    p = wins / trades
    d = 1 + z * z / trades
    center = (p + z * z / (2 * trades)) / d
    half = z * math.sqrt(p * (1 - p) / trades + z * z / (4 * trades * trades)) / d
    return (p, max(0.0, center - half), min(1.0, center + half))


def breakeven(payout: float) -> float:
    """Win rate necessária para empatar. payout 0.80 -> 0.5556."""
    return 1.0 / (1.0 + payout)


def verdict(wins: int, trades: int, payout: float) -> str:
    """
    'acima' | 'abaixo' | 'inconclusivo' | 'sem dados'
    Só afirma vantagem se TODO o intervalo estiver acima do breakeven.
    """
    if not trades:
        return "sem dados"
    be = breakeven(payout)
    _, lo, hi = wilson_ci(wins, trades)
    if lo > be:
        return "acima"
    if hi < be:
        return "abaixo"
    return "inconclusivo"
