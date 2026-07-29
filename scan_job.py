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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from strategies import add_indicators, score_of, classify, wilson_ci
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

ADX_MAX = 20.0            # sobrescrito pela config do app (None = sem filtro)
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
GIST_CFG = "kairo_config.json"
GIST_STATE = "kairo_scanner_state.json"   # marcador do resumo diário (anti-duplo)


def aplica_config_do_usuario(token, gid):
    """
    Lê a config do app (kairo_config.json) e aplica ao scanner:
      • `estrategias`  -> quais estratégias gravar
      • `horarios_ativo` -> grade da corretora editada por você

    MOTIVO (bug relatado): a lista de estratégias vivia FIXA aqui. Trocar a
    seleção no app (ex.: tirar F, pôr H) não chegava ao scanner, que continuava
    gravando a estratégia antiga e sujando o histórico com entradas que você
    não opera mais. Agora o app é a fonte da verdade também nisto.

    A COORTE passa a carregar a assinatura das estratégias — assim, se a
    seleção mudar de novo, os períodos ficam separáveis na análise em vez de
    virarem uma mistura silenciosa.
    """
    global ESTRATEGIAS, COORTE
    try:
        r = requests.get(f"https://api.github.com/gists/{gid}", timeout=15,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"})
        c = r.json().get("files", {}).get(GIST_CFG, {}).get("content")
        cfg = json.loads(c) if c else {}
    except Exception as e:
        log(f"config do app indisponível ({type(e).__name__}) — padrões em uso.")
        return

    global ADX_MAX
    _adx_on = cfg.get("f_adx_on", True)
    ADX_MAX = float(cfg.get("f_adx_max", 20)) if _adx_on else None
    if ADX_MAX is not None:
        log(f"filtro de regime ativo: só grava com ADX < {ADX_MAX:.0f} (lateral).")

    sel = cfg.get("estrategias")
    if isinstance(sel, list) and sel:
        validas = [s for s in sel if s in CHIP]
        if validas:
            if set(validas) != set(ESTRATEGIAS):
                log(f"estratégias atualizadas pelo app: "
                    f"{'+'.join(CHIP[s] for s in validas)} "
                    f"(antes: {'+'.join(CHIP[s] for s in ESTRATEGIAS)})")
            ESTRATEGIAS = validas
    # assinatura das estratégias na coorte: períodos com seleções diferentes
    # não se misturam na análise
    _sig = "".join(sorted(CHIP[s] for s in ESTRATEGIAS))
    COORTE = f"{TF_MIN}m·{FORCA_MIN}·{MERCADO}·[{_sig}]"

    hor = cfg.get("horarios_ativo") or {}
    aplicados = 0
    for nome, bruto in hor.items():
        if nome in GRADE and isinstance(bruto, dict) and bruto:
            GRADE[nome] = bruto
            aplicados += 1
    if aplicados:
        log(f"grade do usuário aplicada em {aplicados} ativo(s).")


def aplica_grade_do_usuario(token, gid):
    """
    Sobrepõe à grade padrão o que o usuário EDITOU nos Ajustes do app
    (chave `horarios_ativo` do kairo_config.json — mesmo formato {dia: faixas}
    que o grade_core avalia). Fecha a última brecha de divergência: mudar um
    horário no app passa a valer também aqui. Se a config não existir ou vier
    quebrada, a grade padrão continua — o scanner NUNCA fica sem grade, porque
    a coorte foi pré-registrada com o filtro da corretora ligado.
    """
    try:
        r = requests.get(f"https://api.github.com/gists/{gid}", timeout=15,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"})
        c = r.json().get("files", {}).get(GIST_CFG, {}).get("content")
        cfg = json.loads(c) if c else {}
        hor = cfg.get("horarios_ativo") or {}
        aplicados = 0
        for nome, bruto in hor.items():
            if nome in GRADE and isinstance(bruto, dict) and bruto:
                GRADE[nome] = bruto
                aplicados += 1
        if aplicados:
            log(f"grade do usuário aplicada em {aplicados} ativo(s) (via config do app).")
    except Exception as e:
        log(f"config do app indisponível ({type(e).__name__}) — grade padrão em uso.")


def aberto_na_corretora(nome, ts_utc):
    """Ativo negociável na Bullex na abertura da vela? (delegado ao grade_core)"""
    bruto = GRADE.get(nome)
    t = pd.Timestamp(ts_utc)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    ag = t.tz_convert(BR_TZ)
    return aberto_em(bruto, ag.weekday(), ag.hour * 60 + ag.minute)


# ------------------------------- TELEGRAM ------------------------------------
TG_SEP = "━━━━━━━━━━━━━━"


def _pt(v, casas=1):
    """número -> texto pt-BR (vírgula decimal)."""
    return f"{v:.{casas}f}".replace(".", ",")


def telegram_send(txt):
    """Resumo pelo bot. Sem os secrets vira no-op — nada aqui depende disso."""
    tk = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    ch = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tk and ch):
        return False
    try:
        requests.post(f"https://api.telegram.org/bot{tk}/sendMessage",
                      json={"chat_id": ch, "text": txt, "parse_mode": "HTML"},
                      timeout=10)
        return True
    except Exception as e:
        log(f"telegram falhou: {type(e).__name__}")
        return False


def _dia_br(ts):
    """ts UTC do histórico -> data (ISO) em Brasília. '' se inválido."""
    try:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t
        return t.tz_convert(BR_TZ).date().isoformat()
    except Exception:
        return ""


def estado_load(token, gid):
    """Estado do scanner (marcadores de resumo, offset do Telegram) no Gist —
    o runner do Actions é efêmero, sem estado externo tudo repetiria."""
    try:
        r = requests.get(f"https://api.github.com/gists/{gid}", timeout=15,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"})
        c = r.json().get("files", {}).get(GIST_STATE, {}).get("content")
        return json.loads(c) if c else {}
    except Exception:
        return {}


def estado_save(token, gid, estado):
    try:
        requests.patch(f"https://api.github.com/gists/{gid}", timeout=15,
                       headers={"Authorization": f"Bearer {token}",
                                "Accept": "application/vnd.github+json"},
                       json={"files": {GIST_STATE: {"content": json.dumps(estado)}}})
    except Exception:
        pass


def _placar(regs):
    """
    (n, w, empates, prem_n, prem_w) de uma lista de registros.

    IGNORA o que os filtros bloquearam (bloq != None): esses registros existem
    só para medir se o filtro ajuda (grupo de controle do A/B) — não são
    operações. Contá-los inflava o resumo e divergia do que é operável.
    """
    regs = [h for h in regs if not h.get("bloq")]
    res = [h for h in regs if h.get("res") in ("ganhou", "perdeu")]
    w = sum(1 for h in res if h["res"] == "ganhou")
    emp = sum(1 for h in regs if h.get("res") == "empate")
    prem = [h for h in res if h.get("premium")]
    wp = sum(1 for h in prem if h["res"] == "ganhou")
    return len(res), w, emp, len(prem), wp


def resumo_diario(hist, estado):
    """Uma vez por dia, após as 18h de Brasília. Devolve True se enviou."""
    agora_br = datetime.now(timezone.utc).astimezone(BR_TZ)
    if agora_br.hour < 18:
        return False
    hoje = agora_br.date().isoformat()
    if estado.get("ultimo_resumo") == hoje:
        return False
    do_dia = [h for h in hist if _dia_br(h.get("ts")) == hoje and not h.get("bloq")]
    res = [h for h in do_dia if h.get("res") in ("ganhou", "perdeu")]
    w = sum(1 for h in res if h["res"] == "ganhou")
    n = len(res)
    emp = sum(1 for h in do_dia if h.get("res") == "empate")
    prem = [h for h in res if h.get("premium")]
    wp = sum(1 for h in prem if h["res"] == "ganhou")
    be = 100.0 / (1.0 + PAYOUT)
    # PREMIUM primeiro — é a coorte que o dono opera e a que a medição
    # out-of-sample favoreceu; o geral vira contexto logo abaixo.
    if prem:
        np_ = len(prem)
        wrp = wp / np_ * 100
        evp = (wp / np_ * (1 + PAYOUT) - 1) * 100
        lp = (f"💎 <b>PREMIUM</b>\n✅ {wp} · ❌ {np_ - wp} — <b>{_pt(wrp)}%</b> "
              f"· EV {'+' if evp > 0 else ''}{_pt(evp)}%/op")
    else:
        lp = "💎 <b>PREMIUM</b>\nSem operações hoje"
    if n:
        wr = w / n * 100
        ev = (w / n * (1 + PAYOUT) - 1) * 100
        linha = (f"⚡ <b>GERAL</b>\n✅ {w} · ❌ {n - w} · 🔄 {emp} — "
                 f"{_pt(wr)}% · EV {'+' if ev > 0 else ''}{_pt(ev)}%/op")
    else:
        linha = f"⚡ <b>GERAL</b>\nSem operações resolvidas ({emp} empate(s))"
    telegram_send(f"📊 <b>KAIRO — FECHAMENTO {agora_br:%d/%m}</b> 📊\n"
                  f"{TG_SEP}\n{lp}\n{TG_SEP}\n{linha}\n{TG_SEP}\n"
                  f"🎯 Breakeven: {_pt(be, 2)}% · Coorte {COORTE}\n"
                  f"🤖 Registro automático a cada 15 min")
    estado["ultimo_resumo"] = hoje
    return True


def resumo_semanal(hist, estado):
    """
    Domingo após as 18h BRT: fecha a semana (últimos 7 dias) com o que o diário
    não mostra — acumulado, IC de Wilson contra o breakeven e o veredito honesto
    de "foi sinal ou ruído". Uma vez por semana (marcador no estado).
    """
    agora_br = datetime.now(timezone.utc).astimezone(BR_TZ)
    if agora_br.weekday() != 6 or agora_br.hour < 18:
        return False
    iso = agora_br.isocalendar()
    chave = f"{iso.year}-W{iso.week:02d}"
    if estado.get("ultimo_semanal") == chave:
        return False
    ini = (agora_br.date() - timedelta(days=6)).isoformat()
    sem = [h for h in hist if _dia_br(h.get("ts")) >= ini and not h.get("bloq")]
    n, w, emp, np_, wp = _placar(sem)
    be = 1.0 / (1.0 + PAYOUT)
    if not n:
        corpo = "Nenhuma operação resolvida na semana."
    else:
        wr = w / n * 100
        ev = (w / n * (1 + PAYOUT) - 1) * 100
        _, lo, hi = wilson_ci(w, n)
        if n < 20:
            ver = "amostra pequena — sem veredito"
        elif lo > be:
            ver = "ACIMA do breakeven ✅ (IC inteiro acima)"
        elif hi < be:
            ver = "ABAIXO do breakeven ❌ (IC inteiro abaixo)"
        else:
            ver = "inconclusivo — dentro do ruído estatístico"
        corpo = (f"✅ {w} · ❌ {n - w} · 🔄 {emp} — <b>{_pt(wr)}%</b> · "
                 f"EV {'+' if ev > 0 else ''}{_pt(ev)}%/op\n"
                 f"📐 IC95: {_pt(lo * 100)}–{_pt(hi * 100)}% · BE {_pt(be * 100, 2)}%\n"
                 f"🧭 Veredito: <b>{ver}</b>")
        if np_:
            corpo += (f"\n{TG_SEP}\n💎 <b>PREMIUM</b>: ✅ {wp} · ❌ {np_ - wp} "
                      f"— {_pt(wp / np_ * 100)}%")
    telegram_send(f"📅 <b>KAIRO — SEMANA {chave}</b> 📅\n{TG_SEP}\n{corpo}\n{TG_SEP}\n"
                  f"🧠 Taxa alta com IC cruzando o breakeven ainda é ruído — "
                  f"só o IC inteiro acima conta como vantagem.")
    estado["ultimo_semanal"] = chave
    return True


def _status_txt(hist):
    """Retrato do momento para o comando /status."""
    agora_br = datetime.now(timezone.utc).astimezone(BR_TZ)
    hoje = agora_br.date().isoformat()
    do_dia = [h for h in hist if _dia_br(h.get("ts")) == hoje and not h.get("bloq")]
    n, w, emp, np_, wp = _placar(do_dia)
    pend = sum(1 for h in hist if h.get("res") is None and not h.get("bloq"))
    ult = max((str(h.get("ts")) for h in hist), default="—")
    if n:
        wr = w / n * 100
        ev = (w / n * (1 + PAYOUT) - 1) * 100
        linha = (f"📊 Hoje: ✅ {w} · ❌ {n - w} · 🔄 {emp} — <b>{_pt(wr)}%</b> "
                 f"· EV {'+' if ev > 0 else ''}{_pt(ev)}%/op")
        if np_:
            linha += f"\n💎 Premium: ✅ {wp} · ❌ {np_ - wp}"
    else:
        linha = f"📊 Hoje: sem operações resolvidas ({emp} empate(s))"
    return (f"📡 <b>KAIRO STATUS · {agora_br:%H:%M}</b> 📡\n{TG_SEP}\n{linha}\n"
            f"{TG_SEP}\n"
            f"⏳ Pendentes: {pend} · 📚 Registros: {len(hist)}\n"
            f"🕐 Último: {ult[:16].replace('T', ' ')} UTC\n"
            f"🤖 Coorte {COORTE} · scanner :01/:16/:31/:46")


def backup_mensal(token, gid, hist, estado):
    """
    Uma vez por mês grava um snapshot completo (kairo_backup_AAAA-MM.json) no
    próprio Gist. É o seguro do forward test: se o histórico principal for
    corrompido ou apagado por engano, o mês não se perde. Devolve True se gravou.
    """
    if not hist:
        return False
    mes = datetime.now(timezone.utc).astimezone(BR_TZ).strftime("%Y-%m")
    if estado.get("ultimo_backup") == mes:
        return False
    arq = f"kairo_backup_{mes}.json"
    try:
        r = requests.patch(f"https://api.github.com/gists/{gid}", timeout=30,
                           headers={"Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github+json"},
                           json={"files": {arq: {"content": json.dumps(
                               hist, ensure_ascii=False)}}})
        if r.status_code == 200:
            log(f"backup mensal gravado: {arq} ({len(hist)} registros).")
            estado["ultimo_backup"] = mes
            return True
    except Exception as e:
        log(f"backup mensal falhou: {type(e).__name__}")
    return False


def responde_comandos(hist, estado):
    """
    Atende o comando /status enviado ao bot. Polling do getUpdates a cada
    rodada do scanner — a resposta chega em até ~15 min (limite honesto do
    cron gratuito; tempo real exigiria servidor dedicado).
    SEGURANÇA: só responde ao chat configurado e só a comandos da lista fixa.
    Qualquer outro texto é DADO, não instrução — é ignorado.
    """
    tk = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    ch = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tk and ch):
        return False
    off = int(estado.get("tg_offset") or 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{tk}/getUpdates",
                         params={"offset": off + 1, "timeout": 0}, timeout=10)
        ups = r.json().get("result", []) or []
    except Exception as e:
        log(f"getUpdates falhou: {type(e).__name__}")
        return False
    mudou = False
    hist_mudou = False
    for u in ups:
        uid = int(u.get("update_id", 0))
        if uid > off:
            off, mudou = uid, True
        # BOTÃO "✅ Executei" (item 2): o clique chega como callback_query.
        # Whitelist estrita: só o prefixo exec| do chat configurado; o resto do
        # conteúdo é DADO e é ignorado.
        cb = u.get("callback_query") or {}
        if cb:
            dados = str(cb.get("data") or "")
            ccid = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
            if ccid == str(ch) and dados.startswith("exec|"):
                try:
                    _, a_, ck_, tf_, dir_ = dados.split("|", 4)
                    achou = False
                    for h in hist:
                        if (h.get("asset") == a_ and str(h.get("ck")) == ck_
                                and str(h.get("tf")) == tf_ and h.get("dir") == dir_):
                            if not h.get("exec"):
                                h["exec"] = True
                                hist_mudou = True
                            achou = True
                            break
                    try:
                        requests.post(f"https://api.telegram.org/bot"
                                      f"{os.environ.get('TELEGRAM_BOT_TOKEN','')}"
                                      f"/answerCallbackQuery",
                                      json={"callback_query_id": cb.get("id"),
                                            "text": ("✅ Marcada como executada!"
                                                     if achou else
                                                     "Registro ainda não chegou — "
                                                     "clique de novo em ~15 min.")},
                                      timeout=10)
                    except Exception:
                        pass
                except Exception as e:
                    log(f"callback inválido: {type(e).__name__}")
            continue
        msg = u.get("message") or {}
        txt = (msg.get("text") or "").strip().lower()
        cid = str((msg.get("chat") or {}).get("id", ""))
        if cid != str(ch):
            continue                       # chat desconhecido: ignora sempre
        if txt.startswith("/status") or txt.startswith("/start"):
            telegram_send(_status_txt(hist))
    if mudou:
        estado["tg_offset"] = off
    return mudou, hist_mudou


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
    # ADX exposto ao chamador: o corte por regime é marcado no REGISTRO
    # (bloq="tendencia"), nunca descartado — descartar mataria o grupo de
    # controle que permite medir se o filtro ajuda em cada par (item 4).
    _adx = float(u.get("adx", 0.0) or 0.0)
    adx_out = round(_adx, 1) if math.isfinite(_adx) else None
    return (saida, round(corpo, 1),
            (None if atrp is None else round(atrp, 1)), adx_out)


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

    aplica_config_do_usuario(token, gid)     # estratégias + grade vindas do app

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

    # ECONOMIA DE CRÉDITOS (diagnóstico do atraso >15min): o scanner gastava 7
    # créditos a cada 15 min, 24/7 = 672 dos 800 diários — na MESMA chave do
    # app. À tarde o teto chegava, a TD recusava e o app caía para o yfinance
    # (fonte mais atrasada), inflando o lag dos sinais. Com a corretora fechada
    # para TODOS os pares (16h-22h BRT e fim de semana), nenhuma vela seria
    # registrada mesmo (a grade veta) — buscar cotação era desperdício puro.
    _agora_chk = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    if not any(aberto_na_corretora(nome, _agora_chk) for nome in PARES):
        log("corretora fechada para todos os pares — sem busca de cotação (economia de créditos).")
        dados = {}
    else:
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
            sinais, corpo, atrp, adx_v = sinais_da_vela(fechadas)
            _bloq_regime = ("tendencia" if (ADX_MAX is not None and adx_v is not None
                                            and adx_v >= ADX_MAX) else None)
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
                    "coorte": COORTE, "bloq": _bloq_regime,
                    "premium": bool(prem), "prem_ver": PREMIUM_VER, "prem_falhas": falhas,
                    "q_corpo": corpo, "q_atrp": atrp, "q_adx": adx_v,
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

    # Telegram: resumos e comandos (tudo no-op sem os secrets). Estado carregado
    # UMA vez e salvo UMA vez — o Gist não vira ping-pong de PATCHes.
    estado = estado_load(token, gid)
    mudou = resumo_diario(hist, estado)
    mudou = resumo_semanal(hist, estado) or mudou
    mudou = backup_mensal(token, gid, hist, estado) or mudou
    _m_cmd, _m_hist = responde_comandos(hist, estado)
    mudou = _m_cmd or mudou
    if _m_hist:
        # cliques em "✅ Executei" alteraram registros: persiste no Gist
        ok = gist_save(token, gid, hist)
        log(f"marcações de execução sincronizadas. Gist {'OK' if ok else 'FALHOU'}.")
    if mudou:
        estado_save(token, gid, estado)


if __name__ == "__main__":
    main()
