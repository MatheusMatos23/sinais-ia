#!/usr/bin/env python3
"""
Kairo — scanner de fundo (forward test M15).

Roda no GitHub Actions, sem navegador. A cada execução ele faz BACKFILL: olha as
velas de 15 min que já FECHARAM desde a última vez, reconstrói qual teria sido o
sinal na ABERTURA de cada uma (indicadores sobre as velas anteriores) e grava no
mesmo Gist que o app lê. Como trabalha só com velas completas, o horário exato da
execução não importa — se o GitHub atrasar ou pular uma rodada, a próxima recupera.

Isso resolve os dois furos do app ao vivo: os buracos de captura (dependia de aba
aberta na virada) e a janela de 20s frágil com dado atrasado.

Segredos (env, definidos nos GitHub Secrets do repositório):
  TWELVE_DATA_KEY  — chave da Twelve Data
  GH_TOKEN         — token GitHub com escopo `gist` (grava o histórico)
  GIST_ID          — id do Gist privado com sinais_historico.json

Nada aqui é recomendação financeira. Uso educacional/demonstração.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from strategies import add_indicators, score_of, classify
# Auditoria C-01: grade da corretora em fonte ÚNICA, compartilhada com o app.
from grade_core import GRADE_BULLEX_TXT, parse_grade, aberto_em

BR_TZ = ZoneInfo("America/Sao_Paulo")

# ----------------------------- CONFIGURAÇÃO FIXA -----------------------------
# Pré-registrada: o scanner não muda de config sozinho. Mexer aqui parte a coorte,
# então qualquer alteração deve vir com decisão consciente (e reinício do teste).
TF_MIN = 15
TF_TD = "15min"
TF_SCORE = "15m"
PARES = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY"]
ESTRATEGIAS = [
    "E · Fade de rompimento", "F · Exaustão", "G · Fade vela extrema",
    "I · Fade extremo lateral", "J · Z-score forte", "K · Reversão dupla",
]
FORCA_MIN = "FRACA"                 # grava tudo; a análise por força vem depois
# FORWARD-ONLY: só registra velas que fecharam há no máximo este tempo. Assim o
# scanner é um COMPLEMENTO (preenche o que o app perdeu na virada), NUNCA um
# backtest — jamais reconstrói velas antigas. 45 min cobre ~3 velas M15, folga
# suficiente para um atraso/pulo do cron do GitHub.
JANELA_GAP_MIN = 45
PAYOUT = 0.85
STAKE = 100.0
MERCADO = "Só forex"
COORTE = f"{TF_MIN}m·{FORCA_MIN}·{MERCADO}"
PREMIUM_VER = 1
PREM_CORPO_MIN = 35.0
PREM_ATR_LO, PREM_ATR_HI = 20.0, 85.0

FORCE_ORDER = {"FRACA": 1, "MEDIA": 2, "FORTE": 3}
GIST_FILE = "sinais_historico.json"
MAX_HIST = 5000

CHIP = {"A · Tendência": "A", "B · Reversão": "B", "C · Rompimento": "C",
        "D · Confluência multi-TF": "D", "E · Fade de rompimento": "E",
        "F · Exaustão": "F", "G · Fade vela extrema": "G", "H · Z-score reversão": "H",
        "I · Fade extremo lateral": "I", "J · Z-score forte": "J",
        "K · Reversão dupla": "K"}

# ------------------------- GRADE DA CORRETORA (BULLEX) -----------------------
# Fonte única em grade_core.py (auditoria C-01): o app e o scanner leem o MESMO
# texto e o MESMO avaliador — não há mais duas cópias para divergirem.
GRADE = {nome: parse_grade(txt) for nome, txt in GRADE_BULLEX_TXT.items()}


def aberto_na_corretora(nome, ts_utc):
    """Ativo negociável na Bullex na abertura da vela? (delegado ao grade_core)"""
    bruto = GRADE.get(nome)
    t = pd.Timestamp(ts_utc)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    ag = t.tz_convert(BR_TZ)
    return aberto_em(bruto, ag.weekday(), ag.hour * 60 + ag.minute)


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# ------------------------------ TWELVE DATA ---------------------------------
def td_fetch(key, symbols, outputsize=250):
    """{símbolo: DataFrame OHLC (UTC naive, crescente)} — uma chamada, N créditos."""
    try:
        r = requests.get("https://api.twelvedata.com/time_series",
                         params={"symbol": ",".join(symbols), "interval": TF_TD,
                                 "outputsize": outputsize, "timezone": "UTC",
                                 "apikey": key, "format": "JSON"}, timeout=30)
        j = r.json()
    except Exception as e:
        log(f"erro na requisição TD: {e}")
        return {}

    def to_df(values):
        rows = []
        for v in values:
            try:
                rows.append((pd.Timestamp(v["datetime"]), float(v["open"]),
                             float(v["high"]), float(v["low"]), float(v["close"])))
            except Exception:
                continue
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["dt", "Open", "High", "Low", "Close"]).set_index("dt")
        df = df.sort_index()
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        return df

    out = {}
    if isinstance(j, dict) and "values" in j and len(symbols) == 1:
        d = to_df(j["values"])
        if d is not None:
            out[symbols[0]] = d
        return out
    if isinstance(j, dict) and j.get("code") in (429, 401, 403):
        log(f"TD recusou: {j.get('message')}")
        return {}
    if isinstance(j, dict):
        for sym, blk in j.items():
            if isinstance(blk, dict) and blk.get("status") == "ok" and "values" in blk:
                d = to_df(blk["values"])
                if d is not None:
                    out[sym] = d
    return out


# --------------------------------- GIST -------------------------------------
def gist_load(token, gid):
    try:
        r = requests.get(f"https://api.github.com/gists/{gid}", timeout=15,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            log(f"Gist load HTTP {r.status_code}")
            return None
        c = r.json().get("files", {}).get(GIST_FILE, {}).get("content")
        return json.loads(c) if c else []
    except Exception as e:
        log(f"erro lendo Gist: {e}")
        return None


def gist_save(token, gid, hist):
    try:
        r = requests.patch(f"https://api.github.com/gists/{gid}", timeout=20,
                           headers={"Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github+json"},
                           json={"files": {GIST_FILE: {"content": json.dumps(hist, ensure_ascii=False)}}})
        return r.status_code == 200
    except Exception as e:
        log(f"erro gravando Gist: {e}")
        return False


# ------------------------------ SINAL POR VELA ------------------------------
def sinais_da_vela(fechadas):
    """
    Reproduz a agregação do app: para cada estratégia, classifica o score sobre as
    velas FECHADAS antes da vela de entrada; junta por direção. Retorna lista de
    {dir, force, strats} e as métricas de qualidade da última vela.
    """
    if len(fechadas) < 60:
        return [], None, None
    d = add_indicators(fechadas)
    agg = {}
    for nm in ESTRATEGIAS:
        try:
            sc = score_of(nm, d, TF_SCORE)
        except Exception:
            continue
        if not len(sc):
            continue
        last = float(sc.iloc[-1])
        r = classify(last)
        if not r:
            continue
        direc, forca = r
        e = agg.setdefault(direc, {"force": forca, "strats": []})
        e["strats"].append(nm)
        if FORCE_ORDER[forca] > FORCE_ORDER[e["force"]]:
            e["force"] = forca
    # qualidade da última vela fechada (a que as estratégias leram)
    u = d.iloc[-1]
    rng = float(u.get("rng", 0.0) or 0.0)
    corpo = (float(u["body"]) / rng * 100.0) if rng > 0 else 0.0
    serie_atr = d["atr"].tail(200).dropna()
    atrp = (float((serie_atr <= float(u["atr"])).mean() * 100.0)
            if len(serie_atr) >= 30 and math.isfinite(float(u["atr"])) else None)
    saida = [{"dir": k, "force": v["force"], "strats": v["strats"]} for k, v in agg.items()]
    return saida, round(corpo, 1), (None if atrp is None else round(atrp, 1))


def avalia_premium(strats, corpo, atrp):
    """Critérios mensuráveis pelo scanner (confluência + corpo + ATR)."""
    falhas = []
    if len(strats) < 2:
        falhas.append("sem confluência")
    if corpo is not None and corpo < PREM_CORPO_MIN:
        falhas.append("corpo pequeno")
    if atrp is not None and not (PREM_ATR_LO <= atrp <= PREM_ATR_HI):
        falhas.append("ATR fora da faixa")
    return (not falhas), falhas


def main():
    key = os.environ.get("TWELVE_DATA_KEY", "")
    token = os.environ.get("GH_TOKEN", "")
    gid = os.environ.get("GIST_ID", "")
    if not (key and token and gid):
        log("faltam segredos (TWELVE_DATA_KEY / GH_TOKEN / GIST_ID)"); sys.exit(1)

    hist = gist_load(token, gid)
    if hist is None:
        log("não consegui ler o histórico — abortando sem gravar"); sys.exit(1)

    # PODA: remove qualquer registro gravado com o ativo FECHADO na Bullex na
    # abertura da vela. Enforça o invariante "coorte só de horário operável" e,
    # de quebra, limpa numa passada os sinais de madrugada/fim de semana que o
    # scanner antigo (sem grade) deixou no Gist.
    antes = len(hist)
    hist = [h for h in hist
            if aberto_na_corretora(h.get("asset"), pd.Timestamp(h.get("ts")))]
    podados = antes - len(hist)
    if podados:
        log(f"podados {podados} registro(s) fora do horário da corretora.")
    vistos = {(h.get("asset"), h.get("ck"), h.get("tf")) for h in hist}
    dirty = podados > 0

    dados = td_fetch(key, PARES, outputsize=250)
    if not dados:
        log("TD não devolveu dados — abortando sem gravar"); sys.exit(1)

    agora = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    per = TF_MIN * 60
    novos = 0

    for nome in PARES:
        df = dados.get(nome)
        if df is None or len(df) < 70:
            continue
        for t_abre in df.index:
            t_close = t_abre + pd.Timedelta(minutes=TF_MIN)
            # só velas JÁ FECHADAS
            if t_close > agora:
                continue
            # FORWARD-ONLY: ignora vela que fechou há muito tempo. Registrá-la
            # seria reconstruir passado (backtest), não teste real. O scanner só
            # cobre a lacuna recente que o app não gravou ao vivo.
            if (agora - t_close) > pd.Timedelta(minutes=JANELA_GAP_MIN):
                continue
            ck = int(t_abre.value // 10**9 // per)
            if (nome, ck, TF_MIN) in vistos:
                continue
            # respeita a grade da corretora: ativo fechado na abertura = não conta
            if not aberto_na_corretora(nome, t_abre):
                continue
            fechadas = df[df.index < t_abre]
            sinais, corpo, atrp = sinais_da_vela(fechadas)
            if not sinais:
                continue
            row = df.loc[t_abre]
            op, cl = float(row["Open"]), float(row["Close"])
            hi, lo = float(row["High"]), float(row["Low"])
            if cl == op:
                res = "empate"
                emp_susp = (hi - lo) > 0
            else:
                res, emp_susp = None, False
            for s in sinais:
                if res is None:
                    venceu = (cl > op) == (s["dir"] == "COMPRA")
                    r_final = "ganhou" if venceu else "perdeu"
                else:
                    r_final = "empate"
                prem, falhas = avalia_premium(s["strats"], corpo, atrp)
                hist.append({
                    "ck": ck, "ts": t_abre.isoformat(), "asset": nome, "dir": s["dir"],
                    "force": s["force"], "strats": [CHIP.get(x, x) for x in s["strats"]],
                    "tf": TF_MIN, "res": r_final, "janela": True,
                    "cfg_forca": FORCA_MIN, "cfg_conf": False, "cfg_mkt": MERCADO,
                    "coorte": COORTE, "bloq": None,
                    "premium": bool(prem), "prem_ver": PREMIUM_VER, "prem_falhas": falhas,
                    "q_corpo": corpo, "q_atrp": atrp,
                    "lag": 0.0, "src": "twelvedata (scanner)",
                    "payout": PAYOUT, "stake": STAKE, "exec": False, "prontidao": "",
                    "ap_open": round(op, 6), "ap_close": round(cl, 6),
                    "ap_var": round(cl - op, 6), "ap_high": round(hi, 6),
                    "ap_low": round(lo, 6), "ap_src": "twelvedata (scanner)",
                    "emp_suspeito": bool(emp_susp),
                })
                vistos.add((nome, ck, TF_MIN))
                novos += 1

    if novos or dirty:
        hist.sort(key=lambda h: str(h.get("ts")))
        if len(hist) > MAX_HIST:
            del hist[:len(hist) - MAX_HIST]
        ok = gist_save(token, gid, hist)
        log(f"{novos} novo(s), {podados} podado(s). Gist {'OK' if ok else 'FALHOU'}. Total {len(hist)}.")
        if not ok:
            sys.exit(1)
    else:
        log("nenhuma vela nova e nada a podar.")


if __name__ == "__main__":
    main()
