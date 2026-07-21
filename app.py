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
import re                      # parser da grade de horários da corretora
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
# Nova York: os dados macro dos EUA saem em horário de lá, e o horário de verão
# americano começa e termina em datas diferentes do resto. Converter "08:30 ET"
# na mão erraria por uma hora em boa parte do ano.
try:
    NY_TZ = ZoneInfo("America/New_York")
except Exception:
    NY_TZ = timezone(timedelta(hours=-4))

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
    # USD/CHF REMOVIDO em 21/07/2026: a Bullex abre ele só 10:00-14:00 em dias
    # úteis (20h por semana, a menor janela de todas) e a expiração mínima é de
    # 5 min. Pouca oportunidade para o custo de varredura que ele impõe.
    # {"name": "USD/CHF", "yf": "USDCHF=X", "cur": ["USD", "CHF"], "type": "fx", "voz": "Dólar Franco"},
    # EUR/GBP entrou no lugar. ATENÇÃO: grade e payout ainda NÃO conferidos na
    # corretora — sem grade cadastrada ele segue o critério de sessão, e sem
    # payout próprio usa o padrão. Confira os dois antes de confiar nos números
    # dele; foi assim que o NZD/USD passou despercebido pagando 30%.
    {"name": "EUR/GBP", "yf": "EURGBP=X", "cur": ["EUR", "GBP"], "type": "fx", "voz": "Euro Libra"},
    # NZD/USD REMOVIDO em 21/07/2026. A Bullex paga 30% de payout nele, o que
    # põe o breakeven em 76,92%. A melhor taxa já medida neste sistema, em 62 mil
    # operações de backtest, foi ~54% — a 54% de acerto o par rende -29,8% por
    # operação. Não é um ativo ruim, é aritmeticamente injogável: nenhuma
    # estratégia, filtro ou horário resolve 23 pontos de diferença.
    # Se a corretora melhorar o payout dele, basta descomentar.
    # {"name": "NZD/USD", "yf": "NZDUSD=X", "cur": ["NZD", "USD"], "type": "fx", "voz": "Dólar Neozelandês"},
    {"name": "EUR/JPY", "yf": "EURJPY=X", "cur": ["EUR", "JPY"], "type": "fx", "voz": "Euro Iene"},
    {"name": "BTC/USD", "yf": "BTC-USD", "cur": [], "type": "crypto", "voz": "Bitcoin"},
    {"name": "ETH/USD", "yf": "ETH-USD", "cur": [], "type": "crypto", "voz": "Ethereum"},
]
# Cronograma REAL da Bullex, lido em Informações -> Condições de Negociação de
# cada ativo (a corretora já publica em UTC-3, o mesmo fuso de Brasília).
# Não é estimativa: cada linha veio da tela da corretora em 21/07/2026.
# Reconferir de tempos em tempos — corretora muda grade sem avisar, e o campo
# em Ajustes permite sobrescrever qualquer um destes.
GRADE_BULLEX_TXT = {
    # os majors com sessão dupla e reabertura no domingo à noite
    "EUR/USD": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    "GBP/USD": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    "USD/JPY": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    "EUR/JPY": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    # estes fecham no fim de semana inteiro e têm janela única
    "AUD/USD": "seg-sex 00:00-14:00",
    "USD/CAD": "seg-sex 03:00-15:00",
    "EUR/GBP": "seg-sex 03:00-15:00",   # conferido na Bullex 21/07/2026
    # cripto negocia 24/7 na corretora: sem grade = sem restrição
}
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


def _hhmm(txt):
    """'09:30' -> 570 minutos. None se não for um horário válido."""
    try:
        h, m = str(txt).strip().split(":")
        h, m = int(h), int(m)
        return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None
    except Exception:
        return None


# Dias da semana no padrão do Python: segunda=0 ... domingo=6.
DIAS_SIG = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5,
            "dom": 6}
DIAS_NOME = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def horas_operaveis(grade, nomes):
    """
    Horas (0-23, Brasília) em que ALGUM dos ativos abre em dia útil.

    O backtest agrega as 24 horas do dia, mas a corretora fecha o forex inteiro
    das 16h às 21h. Sem este recorte, o painel "melhores horários" podia eleger
    uma hora em que não há como operar — conselho pior que nenhum, porque ocupa
    o lugar de uma hora aproveitável no ranking.
    Dia útil como referência: fim de semana tem grade própria e recomendar
    horário com base nele descreveria outra coisa.
    """
    horas = set()
    com_grade = 0
    for nome in nomes:
        bruto = (grade or {}).get(nome)
        if not bruto:
            # IGNORA ativo sem grade em vez de deixá-lo "abrir as 24h". Antes,
            # um único ativo sem grade — cripto, ou um forex ainda não cadastrado
            # — liberava todas as horas e o filtro inteiro morria: a hora 17h
            # aparecia como operável mesmo com a Bullex fechada. Agora ele só
            # não contribui; quem decide são os ativos que têm grade.
            continue
        com_grade += 1
        for dsem in range(5):             # segunda a sexta
            faixas = (bruto.get(dsem, bruto.get(str(dsem)))
                      if isinstance(bruto, dict) else bruto) or []
            for par in faixas:
                if not isinstance(par, (list, tuple)) or len(par) != 2:
                    continue
                ini, fim = _hhmm(par[0]), _hhmm(par[1])
                if ini is None or fim is None:
                    continue
                h_i, h_f = ini // 60, (fim - 1) // 60
                if ini < fim:
                    horas |= set(range(h_i, h_f + 1))
                else:                      # faixa que atravessa a meia-noite
                    horas |= set(range(h_i, 24)) | set(range(0, h_f + 1))
    # nenhum ativo tinha grade: não há o que restringir, libera tudo
    return horas if com_grade else set(range(24))


def parse_grade(texto):
    """
    Texto -> {dia_da_semana: [[ini, fim], ...]}.

    O cronograma da corretora MUDA por dia: a Bullex mostra EUR/USD em
    00:00–15:30 e 22:00–23:59 de terça a quinta, mas na sexta só o período da
    manhã, sábado fechado e domingo só a noite. Uma faixa única aplicada a todos
    os dias liberaria sexta à noite e o sábado inteiro — exatamente as horas em
    que não há como operar.

    Sintaxe (grupos separados por ';', faixas por ','):
        seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59
    Sem prefixo de dia, vale para a semana toda:
        09:00-17:30
    Dia ausente = fechado naquele dia.
    """
    grade = {}
    if not texto:
        return grade
    for grupo in str(texto).split(";"):
        grupo = grupo.strip()
        if not grupo:
            continue
        dias, resto = None, grupo
        # prefixo de dias? ex.: "seg-qui 00:00-15:30" ou "dom 22:00-23:59"
        # inclui vogais acentuadas: "sáb" é a forma natural de escrever sábado,
        # e sem isso a linha inteira era descartada em silêncio.
        m = re.match(r"^([a-zà-úç]{3}(?:\s*-\s*[a-zà-úç]{3})?)\s+(.*)$", grupo, re.I)
        if m:
            spec, resto = m.group(1).lower().replace(" ", ""), m.group(2)
            if "-" in spec:
                a, b = spec.split("-", 1)
                if a in DIAS_SIG and b in DIAS_SIG:
                    ia, ib = DIAS_SIG[a], DIAS_SIG[b]
                    dias = ([ia] if ia == ib else
                            list(range(ia, ib + 1)) if ia < ib
                            else list(range(ia, 7)) + list(range(0, ib + 1)))
            elif spec in DIAS_SIG:
                dias = [DIAS_SIG[spec]]
        if dias is None:
            dias = list(range(7))          # sem prefixo: semana inteira
        faixas = []
        for parte in resto.split(","):
            parte = parte.strip()
            if "-" not in parte:
                continue
            ini, fim = parte.split("-", 1)
            if _hhmm(ini) is not None and _hhmm(fim) is not None:
                faixas.append([ini.strip(), fim.strip()])
        if faixas:
            for dsem in dias:
                grade.setdefault(dsem, []).extend(faixas)
    return grade


def fmt_grade(grade):
    """{dia: faixas} -> texto compacto, agrupando dias com o mesmo horário."""
    if not grade:
        return ""
    porh = {}
    for dsem in range(7):
        chave = ",".join(f"{a}-{b}" for a, b in grade.get(dsem, []))
        if chave:
            porh.setdefault(chave, []).append(dsem)
    partes = []
    for chave, dias in porh.items():
        dias.sort()
        # dias consecutivos viram intervalo (seg-qui), soltos ficam separados
        blocos, ini = [], dias[0]
        for i in range(1, len(dias) + 1):
            if i == len(dias) or dias[i] != dias[i - 1] + 1:
                fim = dias[i - 1]
                blocos.append(DIAS_NOME[ini] if ini == fim
                              else f"{DIAS_NOME[ini]}-{DIAS_NOME[fim]}")
                if i < len(dias):
                    ini = dias[i]
        partes.append(f"{'/'.join(blocos)} {chave}")
    return "; ".join(partes)


def aberto_na_corretora(nome, agora_utc, grade):
    """
    O ativo está negociável na corretora AGORA?

    A grade é do usuário, lida da tela da própria corretora — não existe API
    pública da Bullex e inventar horário seria pior que não ter: o app passaria
    a bloquear entradas válidas ou liberar as que não dá para operar.
    Sem grade cadastrada para o ativo, devolve True e o app segue no critério
    de sessão de mercado que já existia — nunca fecha o que não sabe.
    """
    bruto = (grade or {}).get(nome)
    if not bruto:
        return True
    ag = br(agora_utc)
    agora_min = ag.hour * 60 + ag.minute
    # Formato novo: {dia_da_semana: faixas}. Formato antigo (lista de faixas)
    # continua aceito e vale para todos os dias.
    if isinstance(bruto, dict):
        # o dia pode ter vindo do JSON como texto ("4") em vez de int
        faixas = bruto.get(ag.weekday(), bruto.get(str(ag.weekday())))
        if not faixas:
            # dia declarado na grade porém sem faixa = fechado neste dia
            return False if any(bruto.values()) else True
    else:
        faixas = bruto
    validas = 0
    for par in faixas:
        if not isinstance(par, (list, tuple)) or len(par) != 2:
            continue
        ini, fim = _hhmm(par[0]), _hhmm(par[1])
        if ini is None or fim is None:
            continue
        validas += 1
        # faixa que atravessa a meia-noite (ex.: 21:00 -> 06:00)
        dentro = (ini <= agora_min < fim) if ini < fim else (agora_min >= ini or agora_min < fim)
        if dentro:
            return True
    # Nenhuma faixa VÁLIDA: trata como sem restrição, não como fechado.
    # Uma grade corrompida (config truncada, Gist com lixo) fecharia o ativo
    # o dia inteiro em silêncio — falha na direção de parar de operar sem dizer
    # por quê. Melhor errar liberando: o critério de sessão continua valendo.
    return validas == 0


def hm_exp(ts, minutos):
    """
    "09:05 → 09:10": abertura da vela e expiração da opção.

    Existe por causa de uma confusão que custou uma investigação inteira. A
    corretora nomeia a vela pela EXPIRAÇÃO (a de 09:35 aparece lá como 09:40);
    o app nomeia pela ABERTURA. Ver "09:05" nos dois lugares e supor que é a
    mesma vela leva a comparar velas diferentes e concluir que o app errou o
    resultado — foi exatamente o que aconteceu. Mostrando os dois horários, a
    ambiguidade acaba na origem.
    """
    t = br(ts)
    return f'{t.strftime("%H:%M")} → {(t + timedelta(minutes=minutos)).strftime("%H:%M")}'


# ====================== ENTRADA PREMIUM ======================
# EXPERIMENTO PRÉ-REGISTRADO. Estes limiares são fixos, definidos ANTES de ver
# qualquer resultado, e cada um tem um mecanismo causal declarado. Nenhum foi
# escolhido por dar uma taxa bonita — se fosse assim, eu estaria desenhando o
# alvo em volta do tiro já dado, e o número na tela não valeria nada.
#
# PREMIUM_VER existe para impedir a fraude silenciosa: mudar um limiar sem
# trocar a versão faria o histórico misturar critérios diferentes na mesma taxa.
# Qualquer alteração aqui OBRIGA a subir a versão, e a comparação passa a contar
# só as operações da versão nova. É o que separa experimento de ajuste.
#
# O que este modo NÃO promete: taxa maior. A força já testou essa mesma aposta —
# forte 54,1% contra fraca 53,9% em 10.045 operações — e não separou nada.
# Aqui a hipótese é outra (regime + qualidade da vela + dado limpo), mas continua
# sendo hipótese até a amostra falar.
PREMIUM_VER = 1
PREMIUM_REGRAS = [
    ("2+ estratégias concordando",
     "duas leituras independentes erram junto menos que uma sozinha"),
    ("corpo do candle ≥ 35% do range",
     "a operação resolve pela COR da vela; corpo pequeno vira por ruído"),
    ("ATR entre os percentis 20 e 85",
     "mercado parado o spread come o movimento; em pico é moeda ao ar"),
    ("fora de janela de notícia",
     "em release de dado forte o preço ignora padrão técnico"),
    ("dado fresco (atraso ≤ 1 vela)",
     "sinal calculado sobre vela vencida descreve outro momento"),
    ("par dentro da sessão ativa",
     "fora da sessão a liquidez cai e o spread abre"),
]
PREM_CORPO_MIN = 35.0
PREM_ATR_LO, PREM_ATR_HI = 20.0, 85.0


def num_br(txt):
    """
    Número no padrão brasileiro: vírgula decimal, ponto de milhar.
    O app é em português e a corretora mostra 162,517 — exibir 162.517 ao lado
    disso destoa na hora. Recebe o texto já formatado e só troca os separadores,
    então a regra de casas decimais continua onde ela é decidida.
    """
    return (str(txt).replace(",", "\x00").replace(".", ",").replace("\x00", "."))


def nbf(v, casas=1):
    """
    Número em pt-BR a partir do valor. Existe porque aspas aninhadas dentro de
    f-string só passam a valer no Python 3.12 — passando o número em vez do
    texto, não há aspas nenhuma dentro da chave e roda em qualquer versão.
    """
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return num_br(f"{v:.{casas}f}")


def pct(v, casas=1):
    """Percentual em pt-BR. Não aceita valor inválido calado."""
    if v is None or not math.isfinite(v):
        return "—"
    return num_br(f"{v:.{casas}f}") + "%"


def wl(w, n, casas=1, fino=20):
    """
    "8W · 5L · 61,5%" — a contagem sempre junto da taxa.

    Uma taxa sozinha esconde o tamanho da amostra, e é o tamanho que decide se
    ela quer dizer alguma coisa: 60% pode ser 3 de 5 ou 600 de 1000. Com as duas
    contagens à vista, a fragilidade fica óbvia sem precisar ler o intervalo.
    """
    if not n:
        return '<span class="n">sem operações</span>'
    # abaixo de `fino` operações a taxa perde destaque: continua visível (esconder
    # atrás de "—" só parecia defeito), mas sem competir com as contagens.
    cls = "wl" if n >= fino else "wl fina"
    return (f'<span class="{cls}"><b class="good">{w}W</b>'
            f'<b class="bad">{n - w}L</b>'
            f'<i>{pct(w / n * 100, casas)}</i></span>')


def fmt_price(name, v):
    if v is None or not math.isfinite(v):
        return "—"
    if name.startswith(("BTC", "ETH")):
        return f"{v:,.0f}".replace(",", ".")
    return num_br(f"{v:.3f}") if "JPY" in name else num_br(f"{v:.5f}")


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


# Teto diário a partir do qual gastos SECUNDÁRIOS (radar) param. O radar é
# conforto; o sinal é o produto. Sem esta linha o radar consumiria a cota do dia
# e, na hora da entrada, não haveria crédito para buscar a vela — trocaríamos a
# função principal por uma auxiliar sem ninguém perceber.
TD_DIA_RESERVA = 420          # de 800: metade fica reservada para o sinal


def td_budget(n, secundario=False):
    """
    Reserva n créditos se couber na janela de 60 s. True = pode buscar.
    `secundario=True` (radar) respeita ainda o teto de reserva diária.
    """
    now = time.time()
    today = datetime.now(timezone.utc).date()
    with _td_lock:
        while _td_spent and now - _td_spent[0] > TD_WINDOW_S:
            _td_spent.popleft()
        if _td_day[1] != today:
            _td_day[0], _td_day[1] = 0, today
        if secundario and _td_day[0] + n > TD_DIA_RESERVA:
            return False
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


# ====================== COTAÇÃO AGORA (para o radar) ======================
# O radar precisa do preço NO MEIO da vela, e as velas fechadas não servem: a
# Twelve Data não devolve a vela em formação (foi medindo isso que apareceu o
# bug da vela atrasada). Aqui a busca é de preço pontual, não de vela.
#
# ORÇAMENTO: o radar só é consultado nos últimos segundos da vela e usa o mesmo
# contador de créditos do resto do app. Se não houver crédito sobrando, ele
# simplesmente não aparece — nunca tira crédito da busca que gera o sinal, que
# é a que importa de verdade.
def _preco_td(nomes):
    """{ativo: preço} via endpoint /price. 1 crédito por símbolo."""
    if not TD_KEY or not nomes:
        return {}
    if not td_budget(len(nomes), secundario=True):
        return {}
    import requests
    try:
        r = requests.get("https://api.twelvedata.com/price",
                         params={"symbol": ",".join(nomes), "apikey": TD_KEY},
                         timeout=6)
        j = r.json()
    except Exception:
        return {}
    out = {}
    if isinstance(j, dict) and "price" in j and len(nomes) == 1:
        try:
            out[nomes[0]] = float(j["price"])
        except (TypeError, ValueError):
            pass
        return out
    if isinstance(j, dict):
        for sym, blk in j.items():
            if isinstance(blk, dict) and "price" in blk:
                try:
                    out[sym] = float(blk["price"])
                except (TypeError, ValueError):
                    continue
    return out


def _preco_cripto(nome):
    """Preço spot da exchange. Sem chave e sem limite prático."""
    m = CRYPTO_SYMS.get(nome)
    if not m:
        return None
    import requests
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": m["binance"]}, timeout=5)
        if r.ok:
            return float(r.json()["price"])
    except Exception:
        pass
    try:
        r = requests.get(f"https://api.exchange.coinbase.com/products/{m['coinbase']}/ticker",
                         timeout=5)
        if r.ok:
            return float(r.json()["price"])
    except Exception:
        pass
    return None


@st.cache_data(ttl=120, show_spinner=False)
def precos_agora(nomes_fx, nomes_cripto, _balde):
    """
    `_balde` é a chave da vela: UMA consulta por vela, não uma a cada rerun.
    Com 3 consultas por vela o radar consumiria ~1.700 créditos/dia contra um
    teto de 800 — ele acabaria comendo a cota do sinal. Com uma, são ~576/dia
    no pior caso, e ainda assim o teto de reserva corta antes disso.
    O preço fica com até 60s de idade dentro da janela do radar; para dizer
    "olho nesse par" é suficiente, e o rodapé do painel avisa que é estimativa.
    """
    out = {}
    out.update(_preco_td(list(nomes_fx)))
    for n in nomes_cripto:
        p = _preco_cripto(n)
        if p is not None:
            out[n] = p
    return out


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
/* DEFINIÇÃO ÚNICA. Havia um segundo .block-container mais abaixo com
   padding-top:1.1rem que vencia por ordem — o 1.5rem daqui era letra
   morta. Duas declarações do mesmo seletor já causaram a sobreposição
   das abas antes; o valor efetivo agora está num lugar só. */
.block-container{padding-top:1.1rem;padding-bottom:4rem;max-width:1180px}
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

/* ---------- TABS ----------
   DEFINIÇÃO ÚNICA mais abaixo, em [data-testid="stTabs"]. Aqui existia um
   segundo bloco (.stTabs [data-baseweb=...]) com o estilo de sublinhado, e ele
   convivia com o de pílula: a aba ativa saía com fundo E sublinhado ao mesmo
   tempo, o que parecia acidente. É o mesmo tipo de duplicação que já tinha
   causado a "sobreposição" das abas antes — por isso ficou só uma. */

/* ---------- RÓTULOS DE WIDGET ----------
   A barra de controles usava micro-caixa-alta (.lbl) e as abas usavam o rótulo
   padrão do Streamlit, maior e em caixa mista. Eram duas tipografias brigando
   na mesma tela — o maior resquício de cara "amadora" que sobrou. Agora todo
   rótulo de widget segue o mesmo sistema. */
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] label,
[data-testid="stWidgetLabel"] div{
  font-size:.58rem!important;letter-spacing:.14em!important;
  text-transform:uppercase!important;font-weight:600!important;
  color:var(--mut)!important;line-height:1.5!important}
[data-testid="stWidgetLabel"]{margin-bottom:6px}
/* o "?" de ajuda ficava desalinhado ao lado do rótulo agora menor */
[data-testid="stTooltipIcon"] svg{width:13px;height:13px;opacity:.5}
[data-testid="stTooltipIcon"]:hover svg{opacity:.9}

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
/* min-height comum: só o último cartão tem legenda de duas linhas, e sem isso
   ele ficava mais alto que os vizinhos e a fileira saía torta.
   A legenda vai para o rodapé (margin-top:auto) para o número alinhar entre os
   cartões independentemente do tamanho do texto de baixo. */
.stat{background:linear-gradient(180deg,rgba(255,255,255,.018),transparent 60%),var(--surf);
  border:1px solid var(--line);border-radius:var(--r);
  padding:var(--pad-card);display:flex;flex-direction:column;gap:5px;min-width:0;
  min-height:104px;transition:border-color .18s ease}
.stat:hover{border-color:var(--line2)}
.stat .k{font-size:.58rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mut);font-weight:600}
.stat .v{font-family:'IBM Plex Mono',monospace;font-size:1.6rem;font-weight:600;
  color:var(--ink);line-height:1.1;font-variant-numeric:tabular-nums}
.stat .x{font-size:.66rem;color:var(--mut);line-height:1.35;margin-top:auto}

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
/* O iframe auxiliar não desenha nada — só roda o script que alimenta o cabeçalho
   e dispara a notificação. A API rejeita altura 0, então ele nascia com 1px; e
   1px claro sobre fundo escuro aparece como um risquinho no canto do card. O
   `height:0` do container perdia para a altura do próprio iframe.
   Solução: tirar o iframe do fluxo (absolute, 1x1, opacidade 0). Ele continua
   carregado e executando — ao contrário de display:none, que alguns navegadores
   usam para estrangular timers e quebraria o contador e o aviso de entrada. */
[data-testid="stElementContainer"]:has(.wheel-pass) + [data-testid="stElementContainer"]{
  position:relative}
[data-testid="stElementContainer"]:has(.wheel-pass) + [data-testid="stElementContainer"] iframe{
  position:absolute!important;top:0;left:0;
  width:1px!important;height:1px!important;opacity:0!important;
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
  gap:4px;padding:8px 0 0;
  box-shadow:0 10px 14px -12px rgba(0,0,0,.9)}
/* A régua embaixo das abas era um box-shadow de largura total e parecia borda
   esquecida atravessando a tela. Vira um degradê que nasce sólido à esquerda,
   onde as abas estão, e se dissolve à direita. */
[data-testid="stTabs"] [role="tablist"]::after{
  content:"";position:absolute;left:0;right:0;bottom:0;height:1px;
  background:linear-gradient(90deg,var(--line2) 0%,var(--line) 42%,transparent 92%);
  pointer-events:none}
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
  min-height:0!important;height:0!important;overflow:hidden!important;margin:0!important}

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
[data-testid="stTabs"] [role="tab"]{border-radius:9px 9px 0 0;padding:9px 16px!important;
  color:var(--mut);font-weight:600;font-size:.83rem;position:relative;
  transition:color .18s ease}
[data-testid="stTabs"] [role="tab"]:hover{color:var(--ink2)}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{color:var(--ink);background:transparent}
/* Sublinhado como marcador único (sem pílula). O ::after cresce a partir do
   centro, então trocar de aba tem movimento em vez de um salto seco. */
[data-testid="stTabs"] [role="tab"]::after{
  content:"";position:absolute;left:50%;right:50%;bottom:0;height:2px;
  border-radius:2px 2px 0 0;background:var(--buy);
  box-shadow:0 0 10px rgba(0,200,138,.45);
  transition:left .2s ease,right .2s ease}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]::after{left:10px;right:10px}
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
/* summary do expander: definido uma vez só, lá em cima */

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
/* hora em que a corretora não abre nenhum ativo: fica visível mas apagada, para
   não parecer que faltou dado — faltou mercado. */
.horas .hh.fechada{opacity:.3;text-decoration:line-through}
.horas .h-linha.be{position:absolute;left:16px;right:16px;top:52%;
  border-top:1px dashed var(--warn);opacity:.55}

/* Cartão secundário é link: precisa parecer clicável sem virar botão. */
a.card{display:block;text-decoration:none!important;color:inherit;
  transition:transform .12s,border-color .12s,box-shadow .12s}
a.card:hover{transform:translateY(-2px);border-color:var(--line2);
  box-shadow:0 6px 20px -12px rgba(0,0,0,.9)}
a.card .abrir{display:block;margin-top:9px;font-size:.6rem;letter-spacing:.08em;
  text-transform:uppercase;color:var(--mut);font-weight:600;opacity:0;
  transition:opacity .12s}
a.card:hover .abrir{opacity:1}
.volta{margin:-4px 0 var(--gap-bloco)}
.volta a{font-size:.7rem;color:var(--mut);text-decoration:none!important}
.volta a:hover{color:var(--ink2)}

/* legenda do backtest quando o resultado exibido já não descreve o agora */
.stale-cap{font-size:.72rem;color:var(--warn);line-height:1.5}
.stale-cap b{color:var(--warn);font-weight:700}

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
/* ---------- NÚMEROS ----------
   Dígito de largura fixa em tudo que atualiza sozinho. Sem isso o relógio, o
   atraso e as taxas mudam de largura a cada rerun e o texto ao lado "pula". */
.mono,.stat .v,.chip .cv,.tbl .n,.empty .e-side .v,
[data-testid="stMetricValue"]{font-variant-numeric:tabular-nums}

/* VÍRGULA DECIMAL EM FONTE MONO.
   Com a mudança para o padrão brasileiro, "66.7%" virou "66,7%" — e em
   monoespaçada TODO glifo ocupa uma célula inteira, inclusive a vírgula.
   O ponto, sendo baixo e centrado, passava despercebido; a vírgula abre um vão
   e o número sai lido como "66 , 7%".
   Inter tem algarismos tabulares de verdade: dígito de largura fixa (que é o
   que interessa para o número não dançar a cada rerun) e pontuação com largura
   própria. Então os números com casa decimal saem de mono e vão para Inter.
   O relógio e os contadores de tempo CONTINUAM em mono: lá não há vírgula e o
   alinhamento dos dois-pontos é justamente a vantagem. */
.stat .v,.wr,.sumbar .big,.sumbar .s-side i,.card .px,.tbl .mono,
[data-testid="stMetricValue"]{
  font-family:'Inter',-apple-system,sans-serif;
  font-variant-numeric:tabular-nums;letter-spacing:-.01em}

/* ---------- PROFUNDIDADE ----------
   Antes cabeçalho, barra de controles, cartões e tabelas dividiam a MESMA
   superfície e a MESMA borda — tudo no mesmo plano, sem hierarquia. Três
   níveis: fundo (--bg) → superfície (--surf) → elevada (--surf-alto), esta
   última só para o cabeçalho e o cartão em foco. */
.hdr{background:linear-gradient(180deg,rgba(255,255,255,.030),rgba(255,255,255,.004)),var(--surf2)!important;
  box-shadow:0 1px 0 rgba(255,255,255,.045) inset, 0 18px 30px -26px rgba(0,0,0,.95)}
.hero{box-shadow:0 20px 34px -28px rgba(0,0,0,.95)}

/* ---------- BASE DO CÁLCULO (aba Resultado) ----------
   Os três contadores ficam sempre visíveis, ao lado do seletor. A diferença
   entre "executadas" e "resolvidas" é a informação mais importante da aba: é
   ela que diz o quanto do número é a sua operação e o quanto é hipótese. */
/* ---------- SAÚDE DO EXPERIMENTO ----------
   A borda esquerda carrega o estado. Verde não é elogio: é "nada exige sua
   atenção aqui". Âmbar não é erro: é "isto muda o que os números significam". */
.sdrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;
  margin:var(--gap-curto) 0 var(--gap-bloco)}
.sd{background:var(--surf);border:1px solid var(--line);border-left:3px solid var(--line2);
  border-radius:var(--r);padding:12px 15px;display:flex;flex-direction:column;gap:3px;
  min-height:88px}
.sd .k{font-size:.56rem;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);
  font-weight:600}
.sd .v{font-size:1.05rem;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}
.sd .x{font-size:.63rem;color:var(--mut);line-height:1.45;margin-top:auto}
.sd-ok{border-left-color:var(--buy)}
.sd-warn{border-left-color:var(--warn)}
.sd-bad{border-left-color:var(--sell)}
.sd-bad .v{color:var(--sell)}
.sd-warn .v{color:var(--warn)}

.basebox{display:flex;flex-wrap:wrap;gap:10px}
.basebox span{display:flex;flex-direction:column;gap:2px;flex:1;min-width:120px;
  background:var(--surf);border:1px solid var(--line);border-radius:var(--r);
  padding:10px 14px;font-size:.62rem;color:var(--mut);line-height:1.35}
.basebox i{font-style:normal;font-size:1.15rem;font-weight:700;color:var(--ink);
  font-variant-numeric:tabular-nums}

/* ---------- CONTAGEM W/L ----------
   Vitórias e derrotas coladas na taxa, em toda tabela. A taxa sozinha esconde o
   tamanho da amostra — e é o tamanho que decide se ela significa algo. */
.wl{display:inline-flex;align-items:baseline;gap:7px;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.wl b{font-weight:700;font-size:.72rem;padding:1px 6px;border-radius:5px;
  background:var(--surf2);border:1px solid var(--line)}
.wl b.good{color:var(--buy)}
.wl b.bad{color:var(--sell)}
.wl i{font-style:normal;font-weight:700;font-size:.82rem;color:var(--ink)}
/* amostra fina: a taxa perde destaque para o número de operações não enganar */
.wl.fina i{color:var(--mut);font-weight:600}

/* ---------- PREMIUM (experimento pré-registrado) ----------
   Deliberadamente SEM cor de "melhor": nada de dourado, nada de verde. Premium
   aqui é uma hipótese em teste, não uma categoria superior — se o visual
   prometer superioridade antes de a amostra provar, o desenho está mentindo. */
.premreg{border:1px solid var(--line2);border-radius:var(--r);background:var(--surf);
  padding:12px 15px;margin:var(--gap-curto) 0 var(--gap-bloco)}
.pr-h{font-size:.58rem;letter-spacing:.13em;text-transform:uppercase;font-weight:700;
  color:var(--mut);margin-bottom:9px}
.pr-i{display:flex;flex-direction:column;gap:1px;padding:5px 0;
  border-top:1px solid var(--line)}
.pr-i:first-of-type{border-top:0}
.pr-i b{font-size:.74rem;font-weight:600;color:var(--ink2)}
.pr-i span{font-size:.64rem;color:var(--mut);line-height:1.45}
.pr-f{font-size:.63rem;color:var(--mut);line-height:1.55;margin-top:9px;
  padding-top:8px;border-top:1px solid var(--line)}
/* selo na entrada: neutro, do mesmo peso das outras pastilhas */
/* ponto Premium na tabela do histórico: discreto, só para localizar. Âmbar
   neutro, não verde de "melhor" — Premium é hipótese em teste, não vitória. */
.dot-prem{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--warn);margin-left:7px;vertical-align:middle;
  box-shadow:0 0 0 2px rgba(217,164,65,.18)}
.selo-prem{display:inline-flex;align-items:center;gap:5px;font-size:.56rem;
  letter-spacing:.11em;text-transform:uppercase;font-weight:700;color:var(--ink2);
  background:var(--surf2);border:1px solid var(--line2);border-radius:999px;
  padding:3px 9px;margin-left:8px}

/* ---------- RADAR ----------
   Linguagem visual PROPOSITALMENTE diferente da das entradas: borda tracejada
   (nada no app usa tracejado), âmbar em vez do verde/vermelho de COMPRA/VENDA,
   e o aviso "não é entrada" no cabeçalho. É um palpite sobre uma vela que ainda
   não fechou; se em algum momento parecer uma entrada, o desenho falhou. */
.radar{border:1px dashed rgba(217,164,65,.42);border-radius:var(--r);
  background:linear-gradient(180deg,rgba(217,164,65,.05),transparent 70%);
  padding:12px 16px 11px;margin:var(--gap-curto) 0 var(--gap-bloco)}
.rd-hd{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.rd-t{font-size:.6rem;letter-spacing:.14em;text-transform:uppercase;
  font-weight:700;color:var(--warn)}
.rd-warn{font-size:.56rem;letter-spacing:.1em;text-transform:uppercase;font-weight:700;
  color:var(--mut);border:1px solid var(--line2);border-radius:999px;padding:2px 8px}
.rd-item{display:flex;align-items:center;gap:10px;padding:5px 0}
.rd-nm{font-size:.82rem;font-weight:700;color:var(--ink2);min-width:88px}
.rd-tag{font-size:.58rem;letter-spacing:.1em;text-transform:uppercase;
  font-weight:700;color:var(--mut);min-width:56px}
.rd-bar{flex:1;height:4px;border-radius:3px;background:rgba(255,255,255,.07);
  overflow:hidden;min-width:60px;max-width:320px}
.rd-bar i{display:block;height:100%;background:var(--warn)}
.rd-pc{font-family:'Inter',sans-serif;font-variant-numeric:tabular-nums;
  font-size:.78rem;font-weight:700;color:var(--warn);min-width:46px;text-align:right}
.rd-st{font-size:.64rem;color:var(--mut);min-width:104px}
.rd-ft{font-size:.64rem;color:var(--mut);line-height:1.55;margin-top:8px;
  padding-top:8px;border-top:1px solid var(--line)}
@media(max-width:900px){
  .rd-bar{display:none}
  .rd-st{min-width:0}
}

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
# Ativo promovido ao destaque por clique. Vem da URL pelo mesmo mecanismo do
# "Ao vivo" e do "Foco" — sem isso não haveria como clicar num cartão de HTML.
ativo_sel = st.query_params.get("ativo", "")


def url_com(**kw):
    """Monta o link preservando os outros estados da URL."""
    from urllib.parse import quote
    base = {"live": "1" if auto_on else "0", "foco": "1" if foco else "0"}
    if ativo_sel:
        base["ativo"] = ativo_sel
    base.update({k: v for k, v in kw.items() if v is not None})
    base = {k: v for k, v in base.items() if v != ""}
    return "?" + "&".join(f"{k}={quote(str(v))}" for k, v in base.items())

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
    "horas_op": None,
    "payout": 80.0, "intervalo": 15, "so_confluencia": False,
    "fora_sessao": False, "audio": False, "sistema": True,
    "usar_janela": False, "janela": [9, 17],
    # BUG CORRIGIDO. Estes quatro eram GRAVADOS por cfg_save mas não constavam
    # aqui — e cfg_load filtra por `if k in CFG_PADRAO`, então descartava todos
    # na leitura. O valor por entrada voltava para 10 a cada nova sessão mesmo
    # com o Gist funcionando: o dado estava salvo, só não era lido de volta.
    # Eu tinha atribuído isso ao disco efêmero; era só metade da explicação.
    # Regra: toda chave salva em cfg_save PRECISA existir aqui.
    "stake": 10.0, "limite_on": False, "limite": 50.0, "notif": False,
    # ---- filtros de qualidade de entrada (cada um mede o próprio efeito) ----
    "f_corpo_on": False, "f_corpo_min": 35,        # corpo mínimo em % do range
    "f_atr_on": False, "f_atr_lo": 20, "f_atr_hi": 90,   # percentis de ATR aceitos
    "f_news_on": False, "f_news_min": 15, "f_news_txt": "",
    "cb_on": False, "cb_n": 30, "cb_pausa": 60,
    "radar": False, "premium": False,
    # Horário de negociação POR ATIVO, como a corretora pratica. O app já tinha
    # as sessões do mercado interbancário (Londres, Nova York...), mas isso não
    # é o que a corretora de binárias abre e fecha: ela tem grade própria, e o
    # sinal saía em ativo que você não tinha como operar.
    # Formato: {"EUR/USD": [["09:00","17:30"]], ...} em horário de Brasília.
    "horarios_ativo": {}, "usar_horarios_ativo": False,
    # "bullex" = grade real da corretora · "sessao" = sessões do interbancário
    "modo_horario": "bullex",
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
    # SINCRONIZAÇÃO INICIAL. O Gist só era gravado quando algo mudava, então
    # depois de configurar os secrets o histórico já existente ficava parado no
    # disco efêmero esperando o próximo sinal — e se o container reciclasse
    # antes disso, sumia justamente o que o Gist deveria proteger.
    # Se o remoto está vazio e há histórico local, envia na hora.
    if HIST_REMOTO and not remoto and h:
        gist_save(h)
    return h


def hist_save(h):
    out = [{**r, "ts": pd.Timestamp(r["ts"]).isoformat()} for r in h]
    try:
        with open(HIST_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    gist_save(out)



def ops_para_concluir(w, n, payout, teto=20000):
    """
    Quantas operações A MAIS seriam necessárias para o IC95 sair de cima do
    breakeven, mantida a taxa observada. Transforma "não conclusivo" — que hoje
    é um beco sem saída — num número. Às vezes a resposta honesta é "nunca":
    se a taxa está praticamente colada no breakeven, nenhum tamanho de amostra
    resolve, e é isso que o retorno None comunica.
    """
    if n <= 0:
        return None, None
    p = w / n
    be = breakeven(payout)
    lado = "acima" if p > be else "abaixo"
    m = n
    while m < teto:
        m = int(m * 1.15) + 10
        lo, hi = wilson_ci(int(round(p * m)), m)[1:]
        if (lado == "acima" and lo > be) or (lado == "abaixo" and hi < be):
            return max(0, m - n), lado
    return None, lado


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
    """
    Prejuízo do dia para o freio de perda. Conta TUDO que foi resolvido no dia,
    marcado ou não, e a escolha é deliberada.

    O padrão `exec=True` aqui é o mesmo que estava errado na aba Resultado, mas
    pelo motivo oposto: lá ele inflava o extrato com operações que você não fez;
    aqui ele torna o freio conservador. Se contasse só o marcado, bastaria você
    não ter marcado ainda — e você marca depois, não durante — para o freio
    nunca disparar justamente no dia ruim em que ele existe para agir.
    Errar para o lado de parar cedo demais custa uma operação; errar para o lado
    de não parar custa o limite que você mesmo definiu.
    """
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
# Ordem = fluxo de uso: operar → quanto rendeu → tem vantagem? → auditoria → config.
tab_sig, tab_res, tab_perf, tab_hist, tab_cfg = st.tabs(
    ["Sinais", "Resultado", "Desempenho", "Histórico", "Ajustes"])

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
        # min 0.01: com 0 o valor vira falso em Python e os sinais gravados
        # sumiam silenciosamente da aba Resultado, que filtra por ter valor.
        stake = st.number_input("Valor por entrada", min_value=0.01, step=1.0,
                                value=max(0.01, float(CFG.get("stake", 10.0))),
                                help="Gravado em cada sinal novo. Mudar aqui NÃO "
                                     "reescreve operações já registradas — elas "
                                     "guardam o valor que valia na hora.")
        lim_on = st.toggle("Parar após perder X no dia",
                           value=CFG.get("limite_on", False),
                           help="A única proteção que funciona contra decisão "
                                "emocional é a que você toma antes.")
        lim_val = st.number_input("Limite de perda no dia", min_value=0.0, step=10.0,
                                  value=float(CFG.get("limite", 50.0)),
                                  disabled=not lim_on)
        usar_janela = st.toggle("Operar só em horários escolhidos",
                                value=CFG.get("usar_janela", False))
        # Seleção por hora, não por faixa única: assim dá para operar 9h–10h,
        # parar, e voltar das 12h às 15h. Também casa com o painel de horários
        # do Desempenho, que mostra o desempenho hora a hora.
        _horas_salvas = CFG.get("horas_op")
        if _horas_salvas is None:                      # migra a faixa antiga
            _a, _b = CFG.get("janela", [9, 17])
            _horas_salvas = (list(range(_a, _b + 1)) if _a <= _b
                             else list(range(_a, 24)) + list(range(0, _b + 1)))
        horas_op = st.multiselect(
            "Horas permitidas (Brasília)", list(range(24)),
            default=[h for h in _horas_salvas if 0 <= h <= 23],
            format_func=lambda h: f"{h:02d}h–{(h+1) % 24:02d}h",
            disabled=not usar_janela,
            placeholder="Escolha as horas")
        if usar_janela and not horas_op:
            st.caption("Nenhuma hora marcada: o scanner ficará parado o dia todo.")
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
        radar_on = st.toggle("Radar de candidatos (60s antes da virada)",
                             value=CFG.get("radar", False),
                             help="Mostra quais pares estão perto de disparar, para "
                                  "você já deixar o ativo aberto na corretora. NÃO é "
                                  "entrada e nunca vai para o histórico — a vela ainda "
                                  "pode virar. Consome créditos da Twelve Data, com "
                                  "reserva garantida para o sinal.")

        # ---- filtros de qualidade -------------------------------------------
        # Todos são MEDIDOS: o sinal reprovado continua sendo gravado e apurado,
        # marcado com o motivo. Isso dá o contrafactual de graça — dá para ver
        # se o que o filtro cortou acertava mais ou menos do que o que passou.
        # Sem isso, ligar um filtro é fé, não medição.
        st.markdown("**Entrada Premium** — experimento pré-registrado")
        prem_on = st.toggle("Operar só entradas Premium",
                            value=CFG.get("premium", False),
                            help="Restringe a TELA às entradas que passam nos seis "
                                 "critérios. As demais continuam sendo gravadas e "
                                 "apuradas — é isso que permite comparar as duas "
                                 "taxas em vez de trocar uma pela outra às cegas.")
        st.markdown(
            '<div class="premreg"><div class="pr-h">Critérios · versão '
            f'{PREMIUM_VER} · fixados antes de qualquer resultado</div>'
            + "".join(f'<div class="pr-i"><b>{r}</b><span>{m}</span></div>'
                      for r, m in PREMIUM_REGRAS)
            + '<div class="pr-f">Cada critério tem um mecanismo, nenhum foi '
              'calibrado para dar taxa boa. Mudar qualquer limiar obriga a subir '
              'a versão, e a comparação recomeça — senão seria ajustar o teste '
              'depois de ver o gabarito.</div></div>', unsafe_allow_html=True)

        st.markdown("**Filtros de qualidade** — o que for reprovado não vira "
                    "entrada, mas continua sendo apurado para medir se o filtro "
                    "ajuda de verdade.")
        f_corpo_on = st.toggle("Ignorar velas sem corpo (anti-doji)",
                               value=CFG.get("f_corpo_on", False),
                               help="A entrada resolve pela COR da vela seguinte. "
                                    "Vela de corpo minúsculo vira por ruído puro.")
        f_corpo_min = st.slider("Corpo mínimo (% do range da vela)", 5, 80,
                                int(CFG.get("f_corpo_min", 35)), step=5,
                                disabled=not f_corpo_on)
        f_atr_on = st.toggle("Filtrar por regime de volatilidade (ATR)",
                             value=CFG.get("f_atr_on", False),
                             help="ATR muito baixo = mercado parado, o spread come "
                                  "o movimento. ATR muito alto = moeda ao ar.")
        f_atr_lo, f_atr_hi = st.slider(
            "Faixa aceita (percentil do ATR nas últimas 200 velas)", 0, 100,
            (int(CFG.get("f_atr_lo", 20)), int(CFG.get("f_atr_hi", 90))),
            step=5, disabled=not f_atr_on)
        f_news_on = st.toggle("Bloquear janelas de notícia de alto impacto",
                              value=CFG.get("f_news_on", False),
                              help="É o único filtro com causa óbvia: em release "
                                   "de dado forte o preço deixa de seguir padrão "
                                   "técnico nenhum.")
        f_news_min = st.slider("Bloquear ± minutos ao redor do evento", 5, 60,
                               int(CFG.get("f_news_min", 15)), step=5,
                               disabled=not f_news_on)
        f_news_txt = st.text_area(
            "Eventos extras (um por linha: AAAA-MM-DD HH:MM, horário de Brasília)",
            value=CFG.get("f_news_txt", ""), height=90, disabled=not f_news_on,
            placeholder="2026-07-29 15:00   FOMC\n2026-08-12 09:30   CPI",
            help="O app bloqueia sozinho só o que tem data fixa e previsível: "
                 "payroll (1ª sexta do mês, 8h30 de Nova York) e pedidos de "
                 "seguro-desemprego (toda quinta, 8h30 NY). CPI, FOMC, PIB e "
                 "afins mudam de data todo mês — esses você cola aqui, copiando "
                 "do calendário econômico da sua corretora. Não invento datas "
                 "que não tenho como saber.")
        st.markdown("**Freio automático**")
        cb_on = st.toggle("Pausar coorte que estiver perdendo",
                          value=CFG.get("cb_on", False),
                          help="Suspende automaticamente a configuração atual "
                               "quando as últimas N operações ficam abaixo do "
                               "breakeven com significância estatística.")
        cb_n = st.slider("Janela avaliada (últimas N operações)", 20, 100,
                         int(CFG.get("cb_n", 30)), step=10, disabled=not cb_on)
        cb_pausa = st.slider("Pausa após disparar (minutos)", 15, 240,
                             int(CFG.get("cb_pausa", 60)), step=15, disabled=not cb_on)
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
        notif_on = st.toggle("Notificação do navegador na entrada",
                             value=CFG.get("notif", False),
                             help="Permite fechar a aba do app e ainda ser avisado.")
        if notif_on:
            html_box("""
            <div style="font-family:Inter,sans-serif">
              <button id="n" style="background:rgba(0,200,138,.10);color:#00C88A;
                border:1px solid rgba(0,200,138,.32);border-radius:10px;padding:9px 15px;
                font-weight:600;cursor:pointer;font-size:.78rem;font-family:inherit">
                Permitir notificações</button>
              <div id="ns" style="color:#6F7B93;font-size:.68rem;margin-top:7px"></div>
            </div>
            <script>
            /* Pede pela janela pai: dentro do iframe o Chrome bloqueia o pedido
               de permissão, e o clique parecia não fazer nada. */
            var P = window.parent, N = P.Notification || window.Notification;
            var el = document.getElementById('ns');
            function est(){
              if(!N){ el.textContent='Este navegador não suporta notificações.'; return; }
              var p = N.permission;
              el.textContent = p==='granted' ? 'Notificações permitidas.'
                : p==='denied' ? 'Bloqueadas. Libere no cadeado da barra de endereço.'
                : 'Ainda não permitidas — clique no botão acima.';
            }
            document.getElementById('n').onclick=function(){
              if(!N){ est(); return; }
              try{ N.requestPermission().then(function(){ est(); }); }catch(e){ est(); }
            };
            est();
            </script>""", height=88)
    with o3:
        st.markdown("**Análise e atualização**")
        # ERA UM RÁDIO DE 80% OU 90% — e payout não é escolha binária. Com o real
        # em 85%, o app calculava breakeven de 55,56% quando o correto era 54,05%:
        # 1,51 pp de erro, o bastante para uma estratégia entre esses dois valores
        # aparecer como "abaixo do breakeven" estando ACIMA. O veredito de toda a
        # aba Desempenho dependia desse número.
        _pay_ini = CFG.get("payout", 80.0)
        if isinstance(_pay_ini, str):                    # migra "80%" -> 80.0
            _pay_ini = float(_pay_ini.replace("%", "").replace(",", ".") or 80)
        payout_pct = st.number_input(
            "Payout padrão da corretora (%)", min_value=50.0, max_value=100.0,
            step=0.5, value=float(min(100.0, max(50.0, _pay_ini))), format="%.1f",
            help="Use o payout MÉDIO que a corretora te paga hoje. É o número que "
                 "define o breakeven, e portanto todo veredito do app. Errar aqui "
                 "por 5 pontos move o breakeven em cerca de 1,5 ponto.")
        payout_lbl = f"{num_br(f'{payout_pct:.1f}')}%"
        st.caption(f"Breakeven correspondente: **{pct(100 / (1 + payout_pct / 100), 2)}** "
                   f"— é a taxa de acerto abaixo da qual a operação perde dinheiro "
                   f"no longo prazo, por construção.")
        st.caption("A pastilha *Ao vivo / Pausado* fica no cabeçalho, à direita.")
        every = st.slider("Intervalo (s)", 10, 60, int(CFG.get("intervalo", 15)),
                          step=5, disabled=not auto_on)
    st.markdown("**Horário de negociação** — qual calendário o app respeita para "
                "decidir se um ativo pode gerar entrada.")
    _MODOS = {
        "bullex": "Horário da Bullex (grade real da corretora)",
        "sessao": "Sessões do mercado forex (padrão)",
    }
    modo_horario = st.radio(
        "Calendário", list(_MODOS), format_func=lambda k: _MODOS[k],
        index=0 if CFG.get("modo_horario", "bullex") == "bullex" else 1,
        label_visibility="collapsed",
        help="A grade da corretora é a que importa para executar: de nada adianta "
             "um sinal num ativo cujo botão não existe na sua tela. As sessões do "
             "interbancário dizem quando há liquidez — útil para estudo, mas não "
             "corresponde ao que a Bullex abre e fecha.")
    usar_hor_ativo = modo_horario == "bullex"
    if usar_hor_ativo:
        st.caption("Grade já preenchida com o que está publicado em **Informações → "
                   "Condições de Negociação** de cada ativo na Bullex (lido em "
                   "21/07/2026). Corretora muda grade sem avisar — se notar "
                   "divergência, corrija no campo do ativo abaixo.")
    else:
        st.caption("Usando as sessões Sydney/Tóquio/Londres/Nova York. O app pode "
                   "sinalizar ativos que a Bullex tem fechados neste horário.")
    st.caption(
        "Copie de **Informações → Condições de Negociação** no ativo, na própria "
        "corretora (o cronograma dela já vem em UTC-3, o mesmo de Brasília). "
        "Grupos de dias separados por `;`, faixas por `,`:\n\n"
        "`seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59`\n\n"
        "Sem prefixo de dia, vale a semana toda: `09:00-17:30`. "
        "Dia que não aparecer fica **fechado**. Em branco = sem restrição.")
    _hor_salvo = CFG.get("horarios_ativo") or {}
    _hor_novo = {}
    hc1, hc2 = st.columns(2)
    for i, a in enumerate(ASSETS):
        _guard = _hor_salvo.get(a["name"])
        if isinstance(_guard, dict):
            _txt_ini = fmt_grade({int(k): v for k, v in _guard.items()})
        elif isinstance(_guard, list) and _guard:            # formato antigo
            _txt_ini = ", ".join(f"{p[0]}-{p[1]}" for p in _guard
                                 if isinstance(p, (list, tuple)) and len(p) == 2)
        else:
            # nunca preenchido: começa com a grade real da corretora, para você
            # não precisar digitar oito cronogramas na mão
            _txt_ini = GRADE_BULLEX_TXT.get(a["name"], "")
        with (hc1 if i % 2 == 0 else hc2):
            _v = st.text_input(
                a["name"], value=_txt_ini, key=f"hor_{a['name']}",
                placeholder=("24h — deixe em branco" if a["type"] == "crypto"
                             else "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30"),
                disabled=not usar_hor_ativo)
        _g = parse_grade(_v)
        if _g:
            _hor_novo[a["name"]] = {str(k): v for k, v in _g.items()}
    # avisa sobre texto digitado que não virou grade, em vez de ignorar calado
    _invalidos = [a["name"] for a in ASSETS
                  if st.session_state.get(f"hor_{a['name']}", "").strip()
                  and a["name"] not in _hor_novo]
    if _invalidos:
        st.warning(f"Horário não reconhecido em: {', '.join(_invalidos)}. "
                   f"Esses ativos ficaram **sem** restrição — o app não bloqueia "
                   f"o que não conseguiu entender.")
    if usar_hor_ativo and _hor_novo:
        _linhas_g = ""
        for nome, g in _hor_novo.items():
            _ab = aberto_na_corretora(nome, datetime.now(timezone.utc), _hor_novo)
            _linhas_g += (f'<tr><td class="nm">{nome}</td>'
                          f'<td class="n">{fmt_grade({int(k): v for k, v in g.items()})}</td>'
                          f'<td><span class="verd {"v-good" if _ab else "v-mid"}">'
                          f'{"aberto agora" if _ab else "fechado agora"}</span></td></tr>')
        st.markdown(f'<table class="tbl"><tr><th>Ativo</th><th>Grade lida</th>'
                    f'<th>Agora</th></tr>{_linhas_g}</table>', unsafe_allow_html=True)
        st.caption("Confira acima como o app entendeu cada grade — se a leitura não "
                   "bater com a tela da corretora, o texto está errado.")

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
    # Payout baixo não é "um pouco pior": ele move o breakeven para um patamar
    # que nenhuma estratégia medida aqui alcança. Vale gritar, não sussurrar.
    _inviaveis = {n: p for n, p in _pay_ovr.items() if 100 / (1 + p) > 60}
    if _inviaveis:
        _l = " · ".join(f"<b>{n}</b> paga {pct(p*100, 0)}, precisaria de "
                        f"{pct(100/(1+p), 1)} de acerto"
                        for n, p in _inviaveis.items())
        st.markdown(
            f'<div class="win alert"><span class="pt"></span><div class="msg">'
            f'<b>Payout inviável.</b> {_l}. A melhor taxa já medida neste sistema, '
            f'em 62 mil operações de backtest, foi de ~54%. Operar esses ativos é '
            f'perda garantida no longo prazo — não é questão de estratégia, é '
            f'aritmética. Considere removê-los da varredura em <i>Mercados</i> ou '
            f'simplesmente não operá-los.</div></div>', unsafe_allow_html=True)
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
    "mercado": mercado, "payout": float(payout_pct), "intervalo": int(every),
    "so_confluencia": bool(only_conf), "fora_sessao": bool(show_closed),
    "audio": bool(audio_on), "sistema": bool(sistema_on),
    "usar_janela": bool(usar_janela), "horas_op": [int(h) for h in horas_op],
    "stake": float(stake), "limite_on": bool(lim_on), "limite": float(lim_val),
    "notif": bool(notif_on),
    "f_corpo_on": bool(f_corpo_on), "f_corpo_min": int(f_corpo_min),
    "f_atr_on": bool(f_atr_on), "f_atr_lo": int(f_atr_lo), "f_atr_hi": int(f_atr_hi),
    "f_news_on": bool(f_news_on), "f_news_min": int(f_news_min),
    "f_news_txt": str(f_news_txt), "cb_on": bool(cb_on), "cb_n": int(cb_n),
    "radar": bool(radar_on), "premium": bool(prem_on),
    "horarios_ativo": _hor_novo, "usar_horarios_ativo": bool(usar_hor_ativo),
    "modo_horario": modo_horario,
    "cb_pausa": int(cb_pausa),
})

PAYOUT = float(payout_pct) / 100.0
BE = breakeven(PAYOUT) * 100
PAY_OVR = _pay_ovr


def payout_de(nome):
    """Payout do ativo (o específico, se informado; senão o padrão)."""
    return PAY_OVR.get(nome, PAYOUT)

now = datetime.now(timezone.utc)
interval, minutes = TF_YF[TF], int(TF)


# ---------- filtro de notícias de alto impacto ----------
# Só entram aqui eventos de data DETERMINÍSTICA. Payroll é sempre a 1ª sexta do
# mês e os pedidos de seguro-desemprego são sempre na quinta, ambos às 8h30 de
# Nova York. CPI, PIB e FOMC mudam de data — chutar essas datas produziria
# bloqueios errados, então elas ficam na lista manual.
def eventos_recorrentes(dia_ny):
    """Horários (em NY) de eventos previsíveis no dia. dia_ny é um `date`."""
    ev = []
    if dia_ny.weekday() == 3:                                  # quinta
        ev.append((8, 30, "pedidos de seguro-desemprego (EUA)"))
    if dia_ny.weekday() == 4 and dia_ny.day <= 7:              # 1ª sexta do mês
        ev.append((8, 30, "payroll (EUA)"))
    return ev


def eventos_manuais(txt):
    """Lê 'AAAA-MM-DD HH:MM  rótulo' por linha, em horário de Brasília."""
    out = []
    for linha in (txt or "").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        partes = linha.split(maxsplit=2)
        if len(partes) < 2:
            continue
        try:
            dt = datetime.strptime(f"{partes[0]} {partes[1]}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue                                   # linha malformada é ignorada
        rot = partes[2].strip() if len(partes) > 2 else "evento"
        out.append((dt.replace(tzinfo=BR_TZ).astimezone(timezone.utc), rot))
    return out


def janela_de_noticia(agora_utc, minutos, txt):
    """(bloqueado, rótulo). Cobre ± `minutos` ao redor de cada evento."""
    marg = timedelta(minutes=minutos)
    ny = agora_utc.astimezone(NY_TZ)
    alvos = []
    for dia in (ny.date() - timedelta(days=1), ny.date(), ny.date() + timedelta(days=1)):
        for hh, mm, rot in eventos_recorrentes(dia):
            alvos.append((datetime(dia.year, dia.month, dia.day, hh, mm,
                                   tzinfo=NY_TZ).astimezone(timezone.utc), rot))
    alvos += eventos_manuais(txt)
    for quando, rot in alvos:
        if abs(agora_utc - quando) <= marg:
            return True, f"{rot} às {hm(quando)}"
    return False, ""


noticia_ativa, noticia_rot = ((False, "") if not f_news_on
                              else janela_de_noticia(now, int(f_news_min), f_news_txt))

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
# Valores dos WIDGETS, não do CFG: cfg_save troca o dicionário em session_state
# por um novo, e a variável CFG continua apontando para o antigo — a grade só
# passaria a valer no rerun seguinte, e mudar o horário pareceria não funcionar.
GRADE_CORRETORA = _hor_novo
USAR_GRADE = bool(usar_hor_ativo)


def negociavel(a, quando):
    """Só a sessão de mercado. O veto da corretora é aplicado à parte."""
    return pair_open(a, quando)


open_assets = [a for a in _universo if negociavel(a, now)]
scan_list = _universo if show_closed else open_assets
# O veto da corretora vale SEMPRE que estiver ligado, inclusive com "incluir
# pares fora de sessão". São coisas diferentes: aquele toggle é sobre liquidez
# do interbancário; este é sobre o botão existir na sua tela da Bullex. De nada
# adianta um sinal num ativo que a corretora não deixa você operar.
fechados_corretora = []
if USAR_GRADE:
    fechados_corretora = [a["name"] for a in scan_list
                          if not aberto_na_corretora(a["name"], now, GRADE_CORRETORA)]
    scan_list = [a for a in scan_list if a["name"] not in fechados_corretora]

# ANÁLISE HISTÓRICA usa o universo INTEIRO, não o que está aberto agora.
# BUG CORRIGIDO: o backtest da aba Desempenho rodava sobre scan_list, e desde
# que a grade da corretora passou a esvaziar essa lista fora do horário, a aba
# inteira zerava — 62 mil operações viravam nada às 15h31. São perguntas
# diferentes: "posso operar isto agora?" é scan_list; "esta estratégia teve
# vantagem no último mês?" não depende de a corretora estar aberta neste
# instante. O filtro de mercado (forex/cripto) continua valendo, porque aí a
# pergunta é sobre o que você quer analisar.
analise_list = _universo

# BUSCAR DADOS ≠ GERAR SINAL, e confundir os dois deixava operações penduradas.
# Uma entrada aberta às 15:25 expira às 15:30 — mas às 15:30 o ativo já fechou
# na corretora e saiu de scan_list; sem os dados dele, record_and_resolve não
# apura e o sinal fica "aguardando" para sempre. Isso aconteceria justamente no
# fim de toda sessão, que é quando há entradas em aberto.
# Então: busca dados de quem pode gerar sinal MAIS de quem tem sinal pendente.
_pendentes = {h["asset"] for h in hist_load()
              if h.get("res") is None and h.get("tf") == minutes}
_nomes_scan = {a["name"] for a in scan_list}
fetch_list = scan_list + [a for a in _universo
                          if a["name"] in _pendentes and a["name"] not in _nomes_scan]
t_scan0 = time.perf_counter()
data, fetch_s = get_data_live(fetch_list, interval, minutes)
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
qualidade = {}                       # ativo -> métricas da vela de sinal
fechadas_por_ativo = {}              # ativo -> velas fechadas (reuso do radar)
for a in scan_list:
    if a["name"] in bloqueados:        # dado vencido: não vira entrada
        continue
    df = data.get(a["name"])
    if df is None or len(df) < 60:
        continue
    # BUG CORRIGIDO. Antes era `df.iloc[:-1]`, com o comentário "descarta a vela
    # em formação". Isso valia quando a fonte era o yfinance, que devolve a vela
    # parcial em andamento. A Twelve Data — hoje a fonte primária do forex — NÃO
    # devolve: a última linha dela já é a vela FECHADA. O resultado é que o corte
    # cego jogava fora justamente a vela mais recente e a estratégia decidia sobre
    # a anterior, uma vela inteira atrasada. Para as estratégias de fade/reversão
    # (G, I, J, K), que dependem da vela imediatamente anterior ter sido extrema,
    # isso é fatal: elas liam o extremo de 10 minutos atrás.
    # O sintoma media 5,5 min de atraso a 32s da virada — ou seja, um timeframe
    # inteiro. E as duas metades do código se contradiziam: data_diag tratava
    # essa mesma vela como "a última que já deveria ter fechado" (correto) e o
    # scanner a tratava como "em formação" (errado).
    # Agora o corte é por TIMESTAMP, não por posição: fica tudo que fechou antes
    # da vela atual, funcione a fonte como funcionar.
    _abre_atual = pd.Timestamp(candle_key(minutes) * minutes * 60, unit="s")
    _fechadas = df[df.index < _abre_atual]
    if len(_fechadas) < 60:
        continue
    d = add_indicators(_fechadas)
    # Métricas de qualidade da vela de sinal. Medidas SEMPRE, mesmo com os
    # filtros desligados: elas são gravadas no histórico e é o que permite,
    # depois, perguntar "meus doji acertavam menos?" sem ter cortado nada antes.
    _u = d.iloc[-1]
    _rng = float(_u.get("rng", 0.0) or 0.0)
    _corpo_pct = (float(_u["body"]) / _rng * 100.0) if _rng > 0 else 0.0
    _serie_atr = d["atr"].tail(200).dropna()
    _atr_pct = (float((_serie_atr <= float(_u["atr"])).mean() * 100.0)
                if len(_serie_atr) >= 30 and math.isfinite(float(_u["atr"])) else None)
    qualidade[a["name"]] = {"corpo": round(_corpo_pct, 1),
                            "atrp": None if _atr_pct is None else round(_atr_pct, 1)}
    # Guardado para o radar reaproveitar: são as MESMAS velas fechadas já
    # buscadas, então o radar não custa requisição nenhuma além do preço spot.
    fechadas_por_ativo[a["name"]] = _fechadas
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
dentro_janela = (_h_br in set(horas_op)) if usar_janela else True
# Limite de perda diária: consulta o histórico já gravado. Fica ANTES do gate
# porque, uma vez atingido, nenhuma entrada nova deve ser gerada nem registrada.
_perda_hoje = 0.0
_bloqueio_perda = False
if lim_on and lim_val > 0:
    _perda_hoje = pnl_do_dia(hist_load(), br(now).date())
    _bloqueio_perda = _perda_hoje <= -abs(lim_val)

COORTE = (f"{minutes}m·{min_force}"
          f"{'·2+' if only_conf else ''}·{mercado}")

# ---- freio automático por coorte ----
# Dispara quando o LIMITE SUPERIOR de Wilson das últimas N operações desta mesma
# configuração fica abaixo do breakeven: não é "perdi umas seguidas", é "mesmo
# sendo otimista com o intervalo, essa configuração está perdendo dinheiro".
# A pausa conta a partir da última operação avaliada, então ela se solta sozinha
# — se dependesse de uma nova operação para reavaliar, ficaria travada para sempre.
def circuit_breaker(hist_, coorte, n, minutos_pausa, payout):
    fech = [h for h in hist_ if h.get("coorte") == coorte
            and not h.get("bloq") and h.get("res") in ("ganhou", "perdeu")]
    if len(fech) < n:
        return False, "", 0, 0
    ult = fech[-n:]
    w = sum(1 for h in ult if h["res"] == "ganhou")
    _, _, hi = wilson_ci(w, len(ult))
    if hi >= breakeven(payout):
        return False, "", len(ult), w
    ts = max(pd.Timestamp(h["ts"]) for h in ult)
    solta = br(ts) + timedelta(minutes=minutos_pausa)
    if br(now) >= solta:
        return False, "", len(ult), w
    return True, (f"{w}/{len(ult)} nas últimas operações desta configuração — "
                  f"teto do intervalo em {pct(hi*100, 1)}, abaixo do breakeven de "
                  f"{pct(breakeven(payout)*100, 1)}. Volta às "
                  f"{solta.strftime('%H:%M')}."), len(ult), w


_cb_ativo, _cb_msg, _cb_n_aval, _cb_w = (
    circuit_breaker(hist_load(), COORTE, int(cb_n), int(cb_pausa), PAYOUT)
    if cb_on else (False, "", 0, 0))

operando = sistema_on and dentro_janela and not _bloqueio_perda and not _cb_ativo

entries = list(agg.values()) if operando else []
minf = {"FRACA": 1, "MÉDIA": 2, "FORTE": 3}[min_force]
entries = [e for e in entries if FORCE_ORDER[e["force"]] >= minf]
if only_conf:
    entries = [e for e in entries if len(e["strats"]) > 1]
entries.sort(key=lambda e: (len(e["strats"]), FORCE_ORDER[e["force"]], e["score"]), reverse=True)


# ---- filtros de qualidade: separam, não descartam ----
# O sinal reprovado sai da tela (você não vai operá-lo) mas continua indo para o
# histórico com o motivo do corte, e é apurado igual. É o que transforma cada
# filtro em experimento medido em vez de palpite: no fim dá para comparar a taxa
# de acerto do que passou com a do que foi cortado. Se o cortado acertava MAIS,
# o filtro está custando dinheiro — e você só descobre isso guardando o cortado.
def motivo_corte(e):
    q = qualidade.get(e["a"]["name"], {})
    if noticia_ativa:
        return "noticia"
    if f_corpo_on and q.get("corpo") is not None and q["corpo"] < float(f_corpo_min):
        return "corpo"
    if f_atr_on and q.get("atrp") is not None and not (
            float(f_atr_lo) <= q["atrp"] <= float(f_atr_hi)):
        return "atr"
    return None


def avalia_premium(e):
    """
    (é_premium, [motivos de reprovação]). Avaliado em TODO sinal, esteja o modo
    Premium ligado ou não — é isso que permite comparar premium contra o fluxo
    normal sem precisar operar só um dos dois e esperar meses por cada resposta.
    """
    nome = e["a"]["name"]
    q = qualidade.get(nome, {})
    falhas = []
    if len(e["strats"]) < 2:
        falhas.append("sem confluência")
    if q.get("corpo") is not None and q["corpo"] < PREM_CORPO_MIN:
        falhas.append("corpo pequeno")
    if q.get("atrp") is not None and not (PREM_ATR_LO <= q["atrp"] <= PREM_ATR_HI):
        falhas.append("ATR fora da faixa")
    if noticia_ativa:
        falhas.append("janela de notícia")
    _lg = lag_ativo.get(nome)
    if isinstance(_lg, (int, float)) and math.isfinite(_lg) and _lg > minutes + 0.5:
        falhas.append("dado atrasado")
    if not pair_open(e["a"], now):
        falhas.append("fora de sessão")
    return (not falhas), falhas


for _e in entries:
    _e["bloq"] = motivo_corte(_e)
    _e["premium"], _e["prem_falhas"] = avalia_premium(_e)
entries_todos = entries
entries = [e for e in entries_todos if not e["bloq"]]
cortados = [e for e in entries_todos if e["bloq"]]
# "Operar só Premium" esconde as não-premium da tela, mas elas CONTINUAM sendo
# gravadas e apuradas. Se sumissem, a comparação premium × normal morreria no
# dia em que você ligasse o modo — e aí nunca daria para saber se ele ajuda.
if prem_on:
    entries = [e for e in entries if e.get("premium")]


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


def forcas_backtest(d, score, acc):
    """
    Mesma regra do backtest(), agregando por FAIXA DE FORÇA em vez de por hora.

    A régua é idêntica à do ao vivo — classify() usa |score| >= 0,80 FORTE,
    >= 0,60 MÉDIA, >= MIN_SCORE FRACA — para as duas colunas da tabela serem
    comparáveis de fato. Se aqui usasse outro corte, "backtest × ao vivo"
    estaria comparando coisas diferentes e a diferença não significaria nada.
    """
    o_next, c_next = d["Open"].shift(-1), d["Close"].shift(-1)
    mag = score.abs()
    ok = (mag >= MIN_SCORE) & o_next.notna() & c_next.notna() & (c_next != o_next)
    if not bool(ok.any()):
        return
    win = pd.Series(np.where(score > 0, c_next > o_next, c_next < o_next), index=d.index)
    for k, cond in (("FORTE", mag >= 0.80),
                    ("MEDIA", (mag >= 0.60) & (mag < 0.80)),
                    ("FRACA", (mag >= MIN_SCORE) & (mag < 0.60))):
        m = ok & cond
        acc[k][0] += int(m.sum())
        acc[k][1] += int((m & win).sum())


def run_perf():
    """
    Backtest de todas as estratégias.

    Os laços estão com o ATIVO por fora e a estratégia por dentro de propósito:
    add_indicators (EMA, RSI, MACD, Bollinger, ADX sobre ~6 mil velas) é a conta
    mais cara daqui e depende só do ativo, não da estratégia. Com o laço na ordem
    inversa ela rodava 11× por ativo — 99 vezes no total em vez de 9.
    """
    # BUG CORRIGIDO: `now.date()` é a data em UTC, e o índice das velas também.
    # Depois das 21h de Brasília já é o dia seguinte em UTC, então "Hoje" passava
    # a conter só as velas desde a meia-noite UTC — 4 ou 5 operações, com taxas
    # que não significavam nada. Todo o resto do app usa horário de Brasília;
    # esta coluna era a exceção que ninguém tinha notado.
    today = br(now).date()
    dhist = get_data_hist(analise_list, interval)   # universo inteiro: ver nota acima
    out = {n: {"hoje": [0, 0], "per": [0, 0]} for n in STRATEGIES}
    horas = {h: [0, 0] for h in range(24)}          # hora BRT -> [ops, acertos]
    forcas = {"FORTE": [0, 0], "MEDIA": [0, 0], "FRACA": [0, 0]}   # força -> [ops, acertos]
    ativos = {}                                     # ativo -> [ops, acertos] (só estratégias em uso)
    for a in analise_list:
        df = dhist.get(a["name"])
        if df is None or len(df) < 80:
            continue
        d = add_indicators(df)                      # uma vez por ativo
        # o índice está em UTC: desloca para Brasília antes de comparar a data
        m = (d.index - pd.Timedelta(hours=3)).date == today
        tem_hoje = m.any()
        d_hoje = d[m] if tem_hoje else None
        # Hora de Brasília de cada vela: o índice está em UTC.
        hb = (d.index.hour - 3) % 24
        for name in STRATEGIES:
            sc = score_of(name, d, interval)
            r = backtest(d, sc)
            acc = out[name]
            acc["per"][0] += r["trades"]; acc["per"][1] += r["wins"]
            # acumula POR ATIVO, só as estratégias em uso: comparar ativos numa
            # estratégia que você não opera não ajuda a decidir onde operar.
            if name in sel_strats:
                _av = ativos.setdefault(a["name"], [0, 0])
                _av[0] += r["trades"]; _av[1] += r["wins"]
            if tem_hoje:
                rd = backtest(d_hoje, sc[m])
                acc["hoje"][0] += rd["trades"]; acc["hoje"][1] += rd["wins"]
            # Recorte por hora só das estratégias em uso: perguntar "que horas
            # operar" sobre estratégias que você não usa não responde nada.
            if name in sel_strats:
                horas_backtest(d, sc, hb, horas)
                forcas_backtest(d, sc, forcas)
    return {"est": out, "horas": horas, "forcas": forcas, "ativos": ativos}


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
            "chave": (interval, len(analise_list), tuple(sorted(sel_strats))),
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
        "executei": bool(r.get("exec", False)), "prontidao": r.get("prontidao", ""),
        "coorte": r.get("coorte", ""),
        "motivo": r.get("motivo", ""),
        "atraso_min": r.get("lag", ""), "fonte": r.get("src", ""),
        "payout": r.get("payout", ""),
        "cortado_por": r.get("bloq") or "",
        "premium": "sim" if r.get("premium") else "não",
        "premium_reprovou": ", ".join(r.get("prem_falhas") or []),
        "corpo_pct": r.get("q_corpo", ""), "atr_percentil": r.get("q_atrp", ""),
        # prova do resultado — para conferir contra o feed da corretora
        "apuracao_abertura": r.get("ap_open", ""), "apuracao_fech": r.get("ap_close", ""),
        "apuracao_var": r.get("ap_var", ""), "apuracao_fonte": r.get("ap_src", ""),
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
                         # Configuração vigente na hora do sinal. Sem isso, mudar
                         # a força mínima ou o filtro de confluência passava a
                         # misturar experimentos diferentes na mesma taxa, e o
                         # número agregado deixava de responder qualquer coisa.
                         "cfg_forca": min_force, "cfg_conf": bool(only_conf),
                         "cfg_mkt": mercado,
                         "coorte": COORTE,
                         # motivo do corte (None = passou nos filtros e foi
                         # exibido como entrada) + as métricas que decidiram.
                         # Guardar as métricas mesmo com o filtro desligado é o
                         # que permite calibrar o limiar depois com dado real.
                         "bloq": e.get("bloq"),
                         # marca do experimento Premium, gravada em todo sinal
                         "premium": bool(e.get("premium")),
                         "prem_ver": PREMIUM_VER,
                         "prem_falhas": e.get("prem_falhas") or [],
                         "q_corpo": qualidade.get(nome, {}).get("corpo"),
                         "q_atrp": qualidade.get(nome, {}).get("atrp"),
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
            # Prova do resultado: exatamente os números que decidiram ganhou/perdeu,
            # e de qual feed vieram. A opção binária liquida no feed DA CORRETORA,
            # que não é este; quando der divergência, dá para comparar os dois
            # preços na hora em vez de discutir no achismo.
            h["ap_open"] = round(op, 6)
            h["ap_close"] = round(cl, 6)
            h["ap_var"] = round(cl - op, 6)
            h["ap_src"] = st.session_state.get("fontes", {}).get(h["asset"], "?")
            changed = True
    if len(hist) > 3000:
        del hist[:len(hist) - 3000]
    if changed:
        hist_save(hist)
    return hist


# Grava TUDO (inclusive o que os filtros cortaram) para que o cortado também
# seja apurado. `hist` — a lista que alimenta Resultado, Desempenho e Histórico —
# vê só o que virou entrada de verdade; misturar o cortado ali inflaria a
# amostra com operações que você nunca fez.
hist_todos = record_and_resolve(entries_todos, data, minutes, window_open)
hist = [h for h in hist_todos if not h.get("bloq")]
hist_cortados = [h for h in hist_todos if h.get("bloq")]


# ============================== RADAR ==============================
# NÃO é sinal e NÃO pode virar um. Serve para uma coisa só: dizer qual ativo
# deixar aberto na corretora nos segundos que antecedem a virada, para você não
# perder tempo procurando o par quando a entrada de verdade sair.
#
# O que ele faz: monta uma vela PARCIAL com o preço de agora e roda as mesmas
# estratégias sobre ela. Se o score provisório já passou do limiar, aquele ativo
# é candidato.
#
# LIMITE HONESTO DA APROXIMAÇÃO: do meio da vela só conhecemos abertura e preço
# atual — a máxima e a mínima percorridas dentro dela não estão disponíveis sem
# dado tick a tick. Então os pavios são estimados pelo que o preço já andou, e
# ficam SUBESTIMADOS. Para as estratégias de pavio/vela extrema (E, F, G, I) o
# score provisório tende a sair menor que o final. É mais um motivo para o radar
# ser lido como "olho nesse par", nunca como previsão.
RADAR_JANELA = 60          # só nos últimos 60s da vela: antes disso não informa nada
radar = []
radar_ativo = (radar_on and auto_on and operando and not window_open
               and 0 < secs_to_next <= RADAR_JANELA and not dados_atrasados)
if radar_ativo:
    _fx = [a["name"] for a in scan_list
           if a["type"] == "fx" and a["name"] in fechadas_por_ativo]
    _cr = [a["name"] for a in scan_list
           if a["type"] == "crypto" and a["name"] in fechadas_por_ativo]
    _precos = precos_agora(tuple(_fx), tuple(_cr), candle_key(minutes))
    for a in scan_list:
        nome = a["name"]
        base = fechadas_por_ativo.get(nome)
        px = _precos.get(nome)
        if base is None or px is None or len(base) < 60:
            continue
        ab = float(base["Close"].iloc[-1])          # abertura da vela em formação
        parcial = pd.DataFrame(
            [{"Open": ab, "High": max(ab, px), "Low": min(ab, px), "Close": px}],
            index=[pd.Timestamp(candle_key(minutes) * minutes * 60, unit="s")])
        try:
            dp = add_indicators(pd.concat([base, parcial]))
        except Exception:
            continue
        melhor = None
        for nm in sel_strats:
            try:
                sc = score_of(nm, dp, interval)
            except Exception:
                continue
            if not len(sc):
                continue
            v = float(sc.iloc[-1])
            if not math.isfinite(v):
                continue
            if melhor is None or abs(v) > abs(melhor[1]):
                melhor = (nm, v)
        if melhor is None or abs(melhor[1]) < MIN_SCORE * 0.75:
            continue                                 # longe demais: não é candidato
        radar.append({"ativo": nome, "estrategia": melhor[0], "score": melhor[1],
                      "pct": min(999.0, abs(melhor[1]) / MIN_SCORE * 100.0),
                      "dir": "COMPRA" if melhor[1] > 0 else "VENDA",
                      "px": px, "var": px - ab})
    radar.sort(key=lambda r: r["pct"], reverse=True)

    # ---- medição da conversão ----
    # Sem isto o radar seria julgado por impressão. Guarda os candidatos desta
    # vela; na vela seguinte confere quais viraram entrada de fato.
    st.session_state["radar_pend"] = {"ck": candle_key(minutes),
                                      "ativos": [r["ativo"] for r in radar]}

_rp = st.session_state.get("radar_pend")
if _rp and _rp.get("ck") == candle_key(minutes) - 1 and _rp.get("ativos") is not None:
    _virou = {e["a"]["name"] for e in entries_todos}
    _acc = st.session_state.setdefault("radar_conv", [0, 0])   # [candidatos, viraram]
    _acc[0] += len(_rp["ativos"])
    _acc[1] += sum(1 for n in _rp["ativos"] if n in _virou)
    st.session_state["radar_pend"] = None



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


def hero_html(e, cvela, tag_destaque="Melhor entrada"):
    cls = "buy" if e["dir"] == "COMPRA" else "sell"
    ar = "▲" if e["dir"] == "COMPRA" else "▼"
    return f"""<div class="hero {cls}">
      <div class="hero-main">
        <div class="h-tag">{tag_destaque}</div>
        <div class="h-pair">{e["a"]["name"]}{'<span class="selo-prem">premium</span>' if e.get("premium") else ""}</div>
        <div class="h-dir"><span class="ar">{ar}</span>{e["dir"]}</div>
        <div class="fb">{bars(e["force"])}<span class="lbl2">Força {FL[e["force"]].lower()}</span></div>
      </div>
      <div class="hero-side">
        <div class="h-row"><span class="h-k">Preço atual</span>
          <span class="h-v mono">{fmt_price(e["a"]["name"], e.get("px"))}</span></div>
        <div class="h-row"><span class="h-k">Vela · expira</span>
          <span class="h-v mono">{cvela}</span></div>
        <div class="h-row"><span class="h-k">Estratégias</span>{chips(e, big=True)}</div>
      </div></div>"""


def card_html(e):
    """Cartão clicável: promove o ativo ao destaque, onde as estratégias
    aparecem por extenso. Com pares correlacionados (EUR/JPY e USD/JPY, por
    exemplo) é preciso poder comparar os dois de perto antes de escolher um."""
    cls = "buy" if e["dir"] == "COMPRA" else "sell"
    ar = "▲" if e["dir"] == "COMPRA" else "▼"
    return (f'<a class="card {cls}" target="_self" href="{url_com(ativo=e["a"]["name"])}" '
            f'title="Abrir {e["a"]["name"]} em destaque">'
            f'<div class="top"></div><div class="body">'
            f'<div class="row1"><span class="p">{e["a"]["name"]}</span>'
            f'<span class="px">{fmt_price(e["a"]["name"], e.get("px"))}</span></div>'
            f'<div class="d">{ar} {e["dir"]}</div>'
            f'<div class="fb">{bars(e["force"])}<span class="lbl2">{FL[e["force"]].lower()}</span></div>'
            f'{chips(e)}<span class="abrir">ver estratégias →</span></div></a>')


# ============================== ABA SINAIS ==============================
with tab_sig:
    _ts_vela = pd.Timestamp(candle_key(minutes) * minutes * 60, unit="s")
    cvela = hm(_ts_vela)
    # abertura → expiração: o mesmo par de horários que a corretora mostra,
    # para não haver dúvida sobre qual vela é qual.
    cvela_exp = hm_exp(_ts_vela, minutes)

    # --- estado da janela de entrada ---
    if window_open:
        st.markdown(f'<div class="win ok"><span class="pt"></span><div class="msg">'
                    f'<b>Entrada válida agora</b> — vela {cvela_exp}. '
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
            val += f' · {nbf(lag, 1)}min'
        cs.append(chip(SRC.get(src, src), val, alerta=(src == "yfinance")))
    cs.append(chip("Busca", f"{nbf(fetch_s, 1)}s"))
    # Latência da VIRADA: só faz sentido medir no rerun disparado pela troca de
    # vela. Num carregamento manual no meio da vela, _age é a idade da vela e não
    # mede nada — por isso só grava dentro da janela de entrada, e nas demais
    # execuções mostra a última medida válida.
    if primeiro_da_vela:
        st.session_state["turn_lat"] = _age + (time.perf_counter() - t_scan0)
    tl = st.session_state.get("turn_lat")
    cs.append(chip("Sinal pronto em",
                   f"+{nbf(tl, 1)}s da virada" if tl is not None else "aguardando virada",
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
    # Ativo fechado na corretora sumia da varredura em silêncio: o contador
    # caía de 9 para 5 e não havia como saber se era grade, sessão ou defeito.
    if fechados_corretora:
        cs.append(chip("Fechados na corretora",
                       ", ".join(sorted(fechados_corretora))[:60]))
    err = st.session_state.get("td_erro")
    if err:
        cs.append(chip("Twelve Data", "indisponível", alerta=True))
    # Resumo colapsado: o detalhe das fontes só importa quando algo está errado.
    # Aberto por padrão apenas se houver alerta.
    problema = bool(err) or bool(bloqueados) or any(
        v == "yfinance" for k, v in _f.items() if k in varridos)
    lag_pior = max(lag_fonte.values()) if lag_fonte else 0.0
    resumo = ("Fontes com problema" if problema
              else f"Fontes OK · atraso {nbf(lag_pior, 1)}min"
                   + (f" · sinal +{nbf(tl, 1)}s" if tl is not None else ""))
    st.markdown(
        f'<details class="diagbox"{" open" if problema else ""}>'
        f'<summary><span class="dot {"bad" if problema else "ok"}"></span>{resumo}</summary>'
        f'<div class="diag">{"".join(cs)}</div></details>', unsafe_allow_html=True)
    if err:
        st.caption(f"Twelve Data: {err}")

    # ---- RADAR ----
    # Tratamento visual deliberadamente DIFERENTE do das entradas: borda
    # tracejada, cor âmbar (nunca o verde/vermelho de COMPRA/VENDA) e o rótulo
    # "não é entrada" no próprio cabeçalho. Se um dia isto parecer uma entrada,
    # o design falhou — a confusão custaria uma operação fora da regra testada.
    if radar_ativo and radar:
        _linhas_r = ""
        for r in radar:
            _pl = min(100.0, r["pct"])
            _pronto = r["pct"] >= 100
            _linhas_r += (
                f'<div class="rd-item">'
                f'<span class="rd-nm">{r["ativo"]}</span>'
                f'<span class="rd-tag">{r["dir"].lower()}</span>'
                f'<span class="rd-bar"><i style="width:{_pl:.0f}%"></i></span>'
                f'<span class="rd-pc">{pct(r["pct"], 0)}</span>'
                f'<span class="rd-st">{"acima do limiar" if _pronto else "perto"}</span>'
                f'</div>')
        _cv = st.session_state.get("radar_conv", [0, 0])
        _conv = (f'{_cv[1]}/{_cv[0]} candidatos viraram entrada '
                 f'({pct(_cv[1] / _cv[0] * 100, 0)})' if _cv[0] else
                 'ainda sem amostra para dizer quanto isso acerta')
        st.markdown(
            f'<div class="radar"><div class="rd-hd">'
            f'<span class="rd-t">Radar · {int(secs_to_next)}s para a virada</span>'
            f'<span class="rd-warn">não é entrada</span></div>'
            f'{_linhas_r}'
            f'<div class="rd-ft">Deixe o par aberto na corretora. O sinal só existe '
            f'quando a vela fechar, e pode não sair — a vela ainda pode virar. '
            f'Máxima e mínima da vela em formação não são conhecidas, então o '
            f'percentual sai subestimado nas estratégias de pavio. '
            f'Até agora: {_conv}.</div></div>', unsafe_allow_html=True)

    # Sem este aviso, um filtro ligado deixaria a tela vazia sem explicação e
    # pareceria bug — foi exatamente assim que o gate de janela de entrada
    # confundiu antes.
    if noticia_ativa:
        st.markdown(
            f'<div class="win alert"><span class="pt"></span><div class="msg">'
            f'<b>Janela de notícia — entradas suspensas</b><br>{noticia_rot}. '
            f'Bloqueio de ±{int(f_news_min)} min. Os sinais que aparecerem agora '
            f'continuam sendo gravados e apurados, marcados como cortados, para '
            f'medir depois se evitar notícia realmente ajudou.</div></div>',
            unsafe_allow_html=True)
    elif cortados:
        _q = {}
        for _e in cortados:
            _q[_e["bloq"]] = _q.get(_e["bloq"], 0) + 1
        _rot = {"corpo": "vela sem corpo", "atr": "volatilidade fora da faixa",
                "noticia": "janela de notícia"}
        _txt = ", ".join(f"{v} por {_rot.get(k, k)}" for k, v in sorted(_q.items()))
        st.caption(f"{len(cortados)} sinal(is) cortado(s) pelos filtros de "
                   f"qualidade: {_txt}. Continuam no histórico para medição.")

    if not operando:
        if _bloqueio_perda:
            _motivo = (f"Limite de perda do dia atingido: {_perda_hoje:.2f} de "
                       f"−{abs(lim_val):.2f}. O sistema para até amanhã.")
        elif _cb_ativo:
            _motivo = f"Freio automático disparado. {_cb_msg}"
        elif not sistema_on:
            _motivo = "Sistema desligado."
        else:
            _hs = ", ".join(f"{h:02d}h" for h in sorted(horas_op)) or "nenhuma"
            _motivo = (f"Fora do horário de operação. Agora são {_h_br:02d}h e as "
                       f"horas liberadas são: {_hs}.")
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
        # Se você clicou num cartão, aquele ativo assume o destaque. Sem clique,
        # vale a ordenação normal (mais estratégias concordando primeiro).
        _idx = next((i for i, e in enumerate(entries)
                     if e["a"]["name"] == ativo_sel), 0)
        _destaque = entries[_idx]
        _manual = _idx > 0
        # Realce só quando a entrada em destaque REALMENTE mudou (ativo, direção
        # ou vela). Sem isso o cartão brilharia a cada rerun, que é o problema
        # que acabamos de tirar da tela.
        _chave = (_destaque["a"]["name"], _destaque["dir"], candle_key(minutes))
        novo_sinal = st.session_state.get("ultimo_sinal") != _chave
        st.session_state["ultimo_sinal"] = _chave
        _cls = f'hero{dim}{" novo" if novo_sinal else ""} '
        st.markdown(hero_html(_destaque, cvela_exp,
                              "Entrada selecionada" if _manual else "Melhor entrada"
                              ).replace('class="hero ', f'class="{_cls}'),
                    unsafe_allow_html=True)
        if _manual:
            st.markdown(
                f'<div class="volta"><a target="_self" href="{url_com(ativo="")}">'
                f'← voltar para a melhor entrada ({entries[0]["a"]["name"]})</a></div>',
                unsafe_allow_html=True)
        rest = [e for i, e in enumerate(entries) if i != _idx]
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
                f'<b>Exposição concentrada</b> — {_txt}. Essas entradas não são '
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
            f'<div class="regbar"><span class="k">Registrado na vela {cvela_exp}</span>'
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
          <div class="e-side"><span class="k">Próxima janela</span>
            <span class="v mono">{int(secs_to_next // 60):02d}:{int(secs_to_next % 60):02d}</span>
          </div>
          </div>""", unsafe_allow_html=True)

    if audio_on:
        if entries:
            top = entries[0]
            ests = ", ".join(_short(s) for s in top["strats"])
            pl = "estratégias" if len(top["strats"]) > 1 else "estratégia"
            fala = (f"Entrada agora. {top['a']['voz']}. {top['dir']}. "
                    f"{pl} {ests}. Força {FL[top['force']].lower()}.")
            titulo_notif = (f"{top['a']['name']} · {top['dir']} · "
                            f"força {FL[top['force']].lower()} · {ests}")
        else:
            fala = ""
            titulo_notif = ""
        # Só o MOTOR de fala fica aqui, invisível. O botão de ativar mudou para a
        # aba Ajustes: liberar o áudio é configuração, não parte da operação.
        # (O navegador exige um clique do usuário antes de permitir voz.)
        st.markdown('<div class="wheel-pass"></div>', unsafe_allow_html=True)
        html_box(f"""
        <script>
        var TF={int(TF)}, FALA={fala!r}, NOTIF={str(bool(notif_on)).lower()};
        var TITULO={titulo_notif!r};
        function say(t){{try{{var u=new SpeechSynthesisUtterance(t);u.lang='pt-BR';u.rate=1.05;
          window.speechSynthesis.cancel();window.speechSynthesis.speak(u);}}catch(e){{}}}}
        function notificar(t){{try{{
          var N = window.parent.Notification || window.Notification;
          if(N && N.permission === 'granted')
            new N('Kairo · entrada agora', {{body:t, tag:'kairo-entrada'}});
        }}catch(e){{}}}}
        (function(){{if(!FALA)return;
          var per=TF*60,n=Date.now()/1000,pos=n%per,c=Math.floor(n/per);
          if(pos>={ENTRY_WINDOW})return;
          if(window.parent.sessionStorage.getItem('dito')==String(c))return;
          window.parent.sessionStorage.setItem('dito',String(c));
          if(window.parent.sessionStorage.getItem('voz')==='1') say(FALA);
          if(NOTIF) notificar(TITULO);
        }})();
        </script>""", height=1)

# ============================== ABA RESULTADO ==============================
with tab_res:
    # BUG CORRIGIDO AQUI, e era grave.
    # Esta aba filtrava por `h.get("exec", True)` — padrão TRUE — enquanto a aba
    # Histórico monta o checkbox com `h.get("exec", False)` — padrão FALSE. As
    # duas discordavam sobre a MESMA operação: no Histórico o sinal aparecia
    # desmarcado ("não executei") e no Resultado ele entrava no dinheiro como se
    # tivesse sido operado. Na prática a aba somava lucro e prejuízo de operações
    # que nunca saíram do papel, e bastava nunca tocar no checkbox para o valor
    # ficar inflado. Era esse o cálculo estranho.
    # Agora não há padrão implícito: as duas bases são explícitas e você escolhe.
    _resolvido = ("ganhou", "perdeu", "empate")
    _base_todos = [h for h in hist if h["res"] in _resolvido and h.get("stake")]
    _base_exec = [h for h in _base_todos if h.get("exec")]

    st.markdown('<div class="sect">Base do cálculo</div>', unsafe_allow_html=True)
    _b1, _b2 = st.columns([1.6, 2.4], vertical_alignment="center")
    with _b1:
        _modo = st.radio(
            "Base", ["Só o que executei", "Todos os sinais"],
            index=0, horizontal=False, label_visibility="collapsed",
            help="«Executei» é o seu dinheiro de verdade. «Todos os sinais» é o "
                 "desempenho do app supondo que você tivesse operado tudo — útil "
                 "para avaliar o sistema, não para conferir o extrato.")
    _so_exec = _modo == "Só o que executei"
    _res_base = _base_exec if _so_exec else _base_todos

    # ---- filtro por força do sinal ----
    # A força é função direta da magnitude do score (FORTE >= 0,80 · MÉDIA >= 0,60
    # · FRACA >= 0,50). "Sinal mais forte acerta mais" é a hipótese mais natural do
    # sistema e nunca tinha sido testada — o dado já estava gravado em todo sinal,
    # só não havia por onde olhar.
    _f_forca = st.multiselect("Força do sinal", ["FRACA", "MEDIA", "FORTE"],
                              default=[], format_func=lambda v: FL[v].capitalize(),
                              placeholder="Todas as forças",
                              key="res_forca")
    if _f_forca:
        _res_base = [h for h in _res_base if h.get("force") in _f_forca]
    with _b2:
        _nao_marcadas = len(_base_todos) - len(_base_exec)
        st.markdown(
            f'<div class="basebox">'
            f'<span><i class="mono">{len(_base_exec)}</i>marcadas como executadas</span>'
            f'<span><i class="mono">{len(_base_todos)}</i>sinais resolvidos com valor</span>'
            f'<span><i class="mono">{_nao_marcadas}</i>não marcadas</span></div>',
            unsafe_allow_html=True)
    if _so_exec and _nao_marcadas:
        st.caption(f"{_nao_marcadas} operação(ões) resolvida(s) ainda não foram marcadas "
                   f"em Histórico → «O que você executou de fato». Enquanto não forem, "
                   f"ficam fora deste número — o que é o comportamento correto, mas "
                   f"significa que o valor abaixo pode estar incompleto.")
    if not _so_exec:
        st.markdown(
            '<div class="win alert"><span class="pt"></span><div class="msg">'
            '<b>Este não é o seu extrato.</b> Você está vendo o resultado como se '
            'tivesse operado todos os sinais, inclusive os que não operou. Serve para '
            'julgar o sistema; não serve para conferir dinheiro.</div></div>',
            unsafe_allow_html=True)

    # ---- a configuração foi perdida? ----
    # Sem Gist, o kairo_config.json vive no disco efêmero do Streamlit Cloud e
    # some a cada rebuild — inclusive a cada deploy meu. O valor por entrada
    # voltava para o padrão de 10 sem avisar, e as operações seguintes eram
    # gravadas com o valor errado. Comparar o que está configurado agora com o
    # que os sinais recentes gravaram expõe isso na hora.
    _rec = [h for h in hist if isinstance(h.get("stake"), (int, float)) and h["stake"]]
    if _rec:
        _ult = float(sorted(_rec, key=lambda x: x["ts"])[-1]["stake"])
        if abs(_ult - float(stake)) > 0.005:
            st.markdown(
                f'<div class="win alert"><span class="pt"></span><div class="msg">'
                f'<b>O valor por entrada mudou.</b> O último sinal foi gravado com '
                f'<b>{_ult:.2f}</b> e agora está configurado <b>{float(stake):.2f}</b>. '
                f'Se você não mudou de propósito, a configuração se perdeu num '
                f'reinício do servidor — sem o Gist configurado isso acontece a cada '
                f'rebuild do app. Corrija em Ajustes antes de operar mais, senão as '
                f'próximas operações entram com o valor errado.</div></div>',
                unsafe_allow_html=True)

    # ---- corrigir valor gravado errado ----
    # O botão antigo só preenchia valor AUSENTE. Quando a configuração se perde
    # num rebuild, o valor volta ao padrão e as operações seguintes são gravadas
    # com um número errado — não ausente, errado — e nenhuma ferramenta cobria
    # esse caso. Reescrever histórico é sério, então isto fica atrás de uma
    # confirmação explícita e recomenda baixar o CSV antes.
    _divergentes = [h for h in hist_todos
                    if isinstance(h.get("stake"), (int, float)) and h["stake"]
                    and abs(float(h["stake"]) - float(stake)) > 0.005]
    if _divergentes:
        _vals = sorted({round(float(h["stake"]), 2) for h in _divergentes})
        st.markdown(
            f'<div class="win alert"><span class="pt"></span><div class="msg">'
            f'<b>{len(_divergentes)} operação(ões) com valor diferente do configurado.</b> '
            f'Gravadas com {", ".join(f"{v:.2f}" for v in _vals)}; o valor atual é '
            f'{float(stake):.2f}. Se isso foi uma perda de configuração e todas foram '
            f'feitas com {float(stake):.2f}, dá para uniformizar abaixo. Se você mudou '
            f'de valor de propósito ao longo do teste, <b>não use</b> — os valores '
            f'gravados estão certos e reescrevê-los falsificaria o resultado.'
            f'</div></div>', unsafe_allow_html=True)
        _u1, _u2 = st.columns([1.4, 3], vertical_alignment="center")
        with _u1:
            _confirma = st.checkbox("Confirmo a reescrita", key="ck_stake_uniforme")
            if st.button(f"Uniformizar em {float(stake):.2f}", key="btn_stake_uni",
                         disabled=not _confirma):
                for h in hist_todos:
                    if isinstance(h.get("stake"), (int, float)) and h["stake"]:
                        h["stake"] = float(stake)
                hist_save(hist_todos)
                st.rerun()
        with _u2:
            st.caption("Isto altera o histórico e não tem desfazer. Baixe o CSV em "
                       "Histórico → Backup e importação antes, se quiser poder voltar. "
                       "Só o valor por entrada muda; resultado, horário e preços de "
                       "apuração ficam intactos.")

    # Sinais gravados antes de existir "valor por entrada" — ou com valor zerado —
    # não entram no cálculo financeiro. Antes sumiam sem explicação nenhuma.
    _sem_valor = [h for h in hist
                  if (h.get("exec") if _so_exec else True)
                  and h["res"] in _resolvido and not h.get("stake")]
    if _sem_valor:
        st.markdown(
            f'<div class="win alert"><span class="pt"></span><div class="msg">'
            f'<b>{len(_sem_valor)} operação(ões) fora deste cálculo</b> por não terem '
            f'valor de entrada gravado — são anteriores a esse campo existir. Elas '
            f'continuam contando nas estatísticas de acerto, só não no resultado '
            f'financeiro. Use o botão abaixo se todas foram feitas com o mesmo '
            f'valor.</div></div>', unsafe_allow_html=True)
        _c1, _c2 = st.columns([1.2, 3], vertical_alignment="center")
        with _c1:
            if st.button(f"Aplicar {stake:.2f} às antigas", key="btn_stake_retro"):
                for h in hist:
                    if not h.get("stake") and h["res"] in ("ganhou", "perdeu", "empate"):
                        h["stake"] = float(stake)
                # salva a lista COMPLETA: `hist` é uma visão sem os cortados,
                # e gravá-la apagaria o histórico dos filtros do disco.
                hist_save(hist_todos)
                st.rerun()
        with _c2:
            st.caption("Preenche o valor só onde está faltando. Operações que já têm "
                       "valor gravado não são tocadas.")

    if not _res_base:
        st.markdown('<div class="sect">Resultado financeiro</div>', unsafe_allow_html=True)
        st.caption(
            ("Nenhuma operação marcada como executada ainda. Vá em Histórico → «O que "
             "você executou de fato» e marque o que você operou de verdade — é sobre "
             "isso que este número é calculado."
             if _so_exec else
             "Ainda não há sinais resolvidos com valor registrado. Defina o *Valor por "
             "entrada* em Ajustes."))
    else:
        def _stats(itens, payout_sim=None):
            """Dinheiro E veredito do mesmo recorte, sempre juntos.
            payout_sim recalcula o resultado como se o payout fosse outro."""
            f = [h for h in itens if h["res"] in ("ganhou", "perdeu")]
            n, w = len(f), sum(1 for h in f if h["res"] == "ganhou")
            if payout_sim is None:
                money = sum(pnl_de(h) for h in itens)
                pay = (sum(float(h.get("payout") or .8) for h in f) / n) if n else PAYOUT
            else:
                pay = payout_sim
                money = sum(float(h["stake"]) * pay if h["res"] == "ganhou"
                            else -float(h["stake"])
                            for h in f)
            v = verdict(w, n, pay) if n else "sem dados"
            lo = wilson_ci(w, n)[1] * 100 if n else 0.0
            hi = wilson_ci(w, n)[2] * 100 if n else 0.0
            return {"money": money, "n": n, "w": w, "pay": pay, "verd": v,
                    "taxa": (w / n * 100) if n else float("nan"),
                    "lo": lo, "hi": hi, "be": breakeven(pay) * 100}

        VT = {"acima": ("good", "acima do breakeven"),
              "abaixo": ("bad", "abaixo do breakeven"),
              "inconclusivo": ("mid", "não conclusivo"),
              "sem dados": ("mid", "sem dados")}

        def bloco_valor(rot, itens):
            """Regra da aba: nenhum valor aparece sem o veredito do mesmo recorte."""
            d = _stats(itens)
            if not d["n"]:
                return (f'<div class="stat"><span class="k">{rot}</span>'
                        f'<span class="v mid">—</span>'
                        f'<span class="x">nenhuma operação</span></div>')
            cls, txt = VT[d["verd"]]
            sinal = "good" if d["money"] > 0 else ("bad" if d["money"] < 0 else "mid")
            return (f'<div class="stat"><span class="k">{rot}</span>'
                    f'<span class="v {sinal}">{d["money"]:+.2f}</span>'
                    f'<span class="x">{d["w"]}W · {d["n"] - d["w"]}L · '
                    f'{pct(d["taxa"], 1)} · IC95 {d["lo"]:.0f}–{pct(d["hi"], 0)} · '
                    f'<b class="{cls}">{txt}</b></span></div>')

        _hoje = br(now).date()
        _sem = [h for h in _res_base if (_hoje - br(h["ts"]).date()).days <= 6]
        _mes = [h for h in _res_base if (_hoje - br(h["ts"]).date()).days <= 29]
        _dia = [h for h in _res_base if br(h["ts"]).date() == _hoje]

        st.markdown('<div class="sect">Resultado por período</div>', unsafe_allow_html=True)
        st.markdown('<div class="statrow">'
                    + bloco_valor("Hoje", _dia)
                    + bloco_valor("Últimos 7 dias", _sem)
                    + bloco_valor("Últimos 30 dias", _mes)
                    + bloco_valor("Tudo", _res_base)
                    + '</div>', unsafe_allow_html=True)
        st.markdown('<div class="note">Cada valor vem com o veredito do mesmo recorte '
                    'de propósito. Ficar positivo por dias seguidos é perfeitamente '
                    'normal sem haver vantagem nenhuma — o dinheiro sozinho não '
                    'distingue sorte de método.</div>', unsafe_allow_html=True)

        # ---------- curva de capital ----------
        _cap = sorted(_res_base, key=lambda x: x["ts"])
        if len(_cap) >= 8:
            eq, pico, ddmax, ddpct, acc = [], 0.0, 0.0, 0.0, 0.0
            for h in _cap:
                acc += pnl_de(h)
                eq.append(acc)
                pico = max(pico, acc)
                if pico - acc > ddmax:
                    ddmax = pico - acc
                    ddpct = (ddmax / pico * 100) if pico > 0 else 0.0
            _pm = sum(float(h.get("payout") or .8) for h in _cap) / len(_cap)
            _sm = sum(float(h.get("stake") or 0) for h in _cap) / len(_cap)
            ref = [_sm * (0.5 * _pm - 0.5) * (i + 1) for i in range(len(_cap))]
            _all = eq + ref + [0.0]
            _lo2, _hi2 = min(_all), max(_all)
            _rg = max(_hi2 - _lo2, 1e-9)
            H2 = 40.0

            def _y2(v):
                return H2 - (v - _lo2) / _rg * H2

            _p1 = " ".join(f"{i/(len(eq)-1)*100:.2f},{_y2(v):.2f}" for i, v in enumerate(eq))
            _p2 = " ".join(f"{i/(len(ref)-1)*100:.2f},{_y2(v):.2f}" for i, v in enumerate(ref))
            _c = "var(--buy)" if eq[-1] >= 0 else "var(--sell)"
            st.markdown('<div class="sect">Curva de capital</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="curva"><div class="c-head">'
                f'<span class="k">Resultado acumulado</span>'
                f'<span class="lg"><i class="be"></i>moeda (50% de acerto)</span></div>'
                f'<svg viewBox="0 0 100 {H2}" preserveAspectRatio="none">'
                f'<line x1="0" y1="{_y2(0):.2f}" x2="100" y2="{_y2(0):.2f}" '
                f'stroke="var(--line2)" stroke-width=".3"/>'
                f'<polyline points="{_p2}" fill="none" stroke="var(--warn)" stroke-width=".7" '
                f'stroke-dasharray="2 2" vector-effect="non-scaling-stroke" opacity=".8"/>'
                f'<polyline points="{_p1}" fill="none" stroke="{_c}" stroke-width="1" '
                f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>'
                f'<div class="c-foot"><span>{len(_cap)} operações</span>'
                f'<span class="mono">resultado {eq[-1]:+.2f} · pior queda {ddmax:.2f}'
                f'{f" ({pct(ddpct, 0)} do pico)" if ddpct else ""}</span></div></div>',
                unsafe_allow_html=True)
            st.markdown(
                f'<div class="note">A tracejada é o que uma <b>moeda</b> renderia nas '
                f'mesmas apostas: com payout médio de {pct(_pm*100, 0)}, acertar metade das '
                f'vezes dá prejuízo constante. Estar acima dela não é vantagem — é apenas '
                f'não estar perdendo no ritmo do acaso.<br><b>Pior queda</b> é a maior '
                f'distância entre um pico e o vale seguinte. É ela, e não a taxa de '
                f'acerto, que define o tamanho de banca necessário para aguentar a '
                f'estratégia sem quebrar no meio.</div>', unsafe_allow_html=True)

        # ---------- quebra por recorte ----------
        def tabela_quebra(titulo, chave, itens):
            grupos = {}
            for h in itens:
                for k in (chave(h) if isinstance(chave(h), list) else [chave(h)]):
                    grupos.setdefault(k, []).append(h)
            if not grupos:
                return
            linhas = ""
            for k, v in sorted(grupos.items(),
                               key=lambda kv: -sum(pnl_de(x) for x in kv[1])):
                d = _stats(v)
                cls, txt = VT[d["verd"]]
                sinal = "good" if d["money"] > 0 else ("bad" if d["money"] < 0 else "mid")
                linhas += (f'<tr><td class="nm">{k}</td>'
                           f'<td class="mono {sinal}" style="font-weight:700">'
                           f'{d["money"]:+.2f}</td>'
                           f'<td class="n">{wl(d["w"], d["n"])}</td>'
                           f'<td class="n mono">{pct(d["pay"]*100, 0)}</td>'
                           f'<td><span class="verd v-{cls if cls != "good" else "good"}">'
                           f'{txt}</span></td></tr>')
            st.markdown(f'<div class="sect">{titulo}</div>', unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>{titulo.split("por ")[-1].capitalize()}</th>'
                        f'<th>Resultado</th><th>Acerto · W/L</th><th>Payout</th>'
                        f'<th>Veredito</th></tr>{linhas}</table>', unsafe_allow_html=True)

        # A força vem primeiro: se ela separar o joio do trigo, é a alavanca mais
        # simples que existe — basta subir a força mínima em Ajustes. Nenhuma
        # outra quebra é acionável com um controle só.
        tabela_quebra("Resultado por força do sinal",
                      lambda h: FL.get(h.get("force", "FRACA"),
                                       h.get("force", "FRACA")).capitalize(),
                      _res_base)
        st.markdown(
            '<div class="note">A força é a magnitude do score: <b>forte</b> a partir '
            'de 0,80 · <b>média</b> de 0,60 · <b>fraca</b> de 0,50. Se as linhas não '
            'se separarem, a força não está dizendo nada sobre acerto — e aí subir a '
            'força mínima só reduz o número de operações sem melhorar nada. '
            'Atenção ao tamanho de cada amostra: dividir em três faz cada faixa '
            'levar três vezes mais tempo para concluir qualquer coisa.</div>',
            unsafe_allow_html=True)
        tabela_quebra("Resultado por estratégia", lambda h: h["strats"], _res_base)
        tabela_quebra("Resultado por ativo", lambda h: h["asset"], _res_base)
        tabela_quebra("Resultado por timeframe",
                      lambda h: f'{h.get("tf", "—")} min', _res_base)
        st.markdown('<div class="note">Uma estratégia com boa taxa de acerto pode perder '
                    'dinheiro por operar num par de payout menor. Em taxa isso é '
                    'invisível; em dinheiro, não.</div>', unsafe_allow_html=True)

        # ---------- simulador de payout ----------
        st.markdown('<div class="sect">E se o payout fosse outro?</div>',
                    unsafe_allow_html=True)
        _sim = st.slider("Payout simulado", 70, 95,
                         int(round(_stats(_res_base)["pay"] * 100)), step=1,
                         format="%d%%")
        d0 = _stats(_res_base)
        d1 = _stats(_res_base, payout_sim=_sim / 100)
        _dif = d1["money"] - d0["money"]
        st.markdown(
            f'<div class="sumbar {"good" if d1["verd"] == "acima" else "mid"}">'
            f'<div class="s-main">Com payout de <b>{_sim}%</b>, as mesmas '
            f'{d1["n"]} operações dariam <b class="big '
            f'{"good" if d1["money"] > 0 else "bad"}">{d1["money"]:+.2f}</b></div>'
            f'<div class="s-side">'
            f'<span><i>{_dif:+.2f}</i>diferença</span>'
            f'<span><i>{pct(d1["be"], 1)}</i>breakeven</span>'
            f'<span><i>{pct(d1["taxa"], 1)}</i>sua taxa</span>'
            f'<span><i>{VT[d1["verd"]][1]}</i>veredito</span></div></div>',
            unsafe_allow_html=True)
        st.markdown(
            f'<div class="note">Este é o controle mais acionável do sistema. As melhores '
            f'estratégias medidas aqui ficam entre 53% e 55% de acerto, e cada ponto de '
            f'payout move o breakeven: <b>{pct(100/1.80, 2)}</b> a 80% · '
            f'<b>{pct(100/1.85, 2)}</b> a 85% · <b>{pct(100/1.90, 2)}</b> a 90%. '
            f'Seu payout atual é <b>{pct(payout_pct, 1)}</b>, breakeven '
            f'<b>{pct(100/(1 + payout_pct/100), 2)}</b>. Negociar payout com a corretora '
            f'move mais o resultado do que trocar de estratégia — e é a única variável '
            f'sob seu controle direto.</div>', unsafe_allow_html=True)

        # ---------- sequência atual ----------
        _ordem = [h for h in sorted(_res_base, key=lambda x: x["ts"])
                  if h["res"] in ("ganhou", "perdeu")]
        if _ordem:
            _seq, _tipo = 0, _ordem[-1]["res"]
            for h in reversed(_ordem):
                if h["res"] == _tipo:
                    _seq += 1
                else:
                    break
            _hoje_pnl = sum(pnl_de(h) for h in _dia)
            # O "faltam X para o limite" tem que usar a MESMA conta do freio, senão
            # o texto promete uma folga que o freio não reconhece.
            _pnl_freio = pnl_do_dia(hist, _hoje)
            _rest = (abs(lim_val) + _pnl_freio) if lim_on else None
            _extra = (f' · faltam <b>{_rest:.2f}</b> para o limite diário'
                      if _rest is not None and _rest > 0 else
                      (' · <b>limite diário atingido</b>' if _rest is not None else ''))
            _obs_freio = ('' if not lim_on or abs(_pnl_freio - _hoje_pnl) < 0.005 else
                          f'<br><span class="n">O freio diário conta todas as operações '
                          f'resolvidas hoje ({_pnl_freio:+.2f}), marcadas ou não, para '
                          f'não deixar de agir só porque você ainda não marcou.</span>')
            st.markdown(
                f'<div class="note">Sequência atual: <b>{_seq} '
                f'{"vitória" if _tipo == "ganhou" else "perda"}'
                f'{"s" if _seq > 1 else ""} seguida{"s" if _seq > 1 else ""}</b>. '
                f'Resultado de hoje: <b>{_hoje_pnl:+.2f}</b>{_extra}.'
                f'{_obs_freio}</div>',
                unsafe_allow_html=True)


# ============================== ABA DESEMPENHO ==============================
with tab_perf:
    # ---------- SAÚDE DO EXPERIMENTO ----------
    # Antes disto, saber se o teste estava íntegro exigia caçar em cinco abas.
    # Num único dia o valor por entrada voltou para o padrão sem avisar e o
    # backtest zerou — os dois passaram despercebidos por horas justamente
    # porque nada olhava o experimento como um todo. Este bloco responde as
    # perguntas que precedem qualquer taxa: o dado está sendo salvo? a
    # configuração mudou no meio? há operação presa? quanto falta para concluir?
    _saude = []

    def _card_saude(rot, val, obs, estado="ok"):
        _saude.append(f'<div class="sd sd-{estado}"><span class="k">{rot}</span>'
                      f'<span class="v">{val}</span><span class="x">{obs}</span></div>')

    _fech_tot = [h for h in hist if h.get("res") in ("ganhou", "perdeu")]
    _abertos_tot = [h for h in hist if h.get("res") is None]

    # 1) persistência
    if HIST_REMOTO:
        _card_saude("Backup", "Gist ativo",
                    "sobrevive a reinício e a deploy", "ok")
    else:
        _card_saude("Backup", "só no disco",
                    "tudo abaixo some no próximo rebuild", "bad")

    # 2) mistura de coortes — a armadilha silenciosa do forward test
    _co = {h.get("coorte") for h in _fech_tot if h.get("coorte")}
    if len(_co) <= 1:
        _card_saude("Configuração", "estável",
                    "todas as operações na mesma coorte", "ok")
    else:
        _card_saude("Configuração", f"{len(_co)} coortes",
                    "a taxa agregada não descreve nenhuma delas", "warn")

    # 3) operações que nunca vão fechar
    _presos_s = [h for h in _abertos_tot if h.get("tf") != minutes]
    if not _presos_s:
        _card_saude("Em aberto", f"{len(_abertos_tot)}",
                    "aguardando a vela fechar", "ok")
    else:
        _card_saude("Em aberto", f"{len(_abertos_tot)}",
                    f"{len(_presos_s)} de outro timeframe: só apuram se você "
                    f"voltar a ele", "warn")

    # 4) ritmo e prazo até o veredito
    if _fech_tot:
        _dias = max(1, len({br(h["ts"]).date() for h in _fech_tot}))
        _ritmo = len(_fech_tot) / _dias
        _w_tot = sum(1 for h in _fech_tot if h["res"] == "ganhou")
        _falta, _lado = ops_para_concluir(_w_tot, len(_fech_tot), PAYOUT)
        if _falta:
            _card_saude("Até concluir", f"{_falta} ops",
                        f"~{_falta/_ritmo:.0f} dias no ritmo de "
                        f"{_ritmo:.0f}/dia", "ok")
        else:
            _card_saude("Até concluir", "—",
                        "diferença pequena demais para qualquer amostra "
                        "resolver com este payout", "warn")
    else:
        _card_saude("Até concluir", "—", "sem operações resolvidas ainda", "warn")

    st.markdown('<div class="sect">Saúde do experimento</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="sdrow">{"".join(_saude)}</div>', unsafe_allow_html=True)

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
            return (f'<span class="wr faint">{pct(p*100, 1)}</span>'
                    f'<span class="ci">IC95 {lo*100:.0f}–{pct(hi*100, 0)}</span><br>'
                    f'<span class="n">{w}W · {n - w}L</span>'
                    f'<span class="verd v-faint">amostra pequena</span>')
        cls = "good" if v == "acima" else ("bad" if v == "abaixo" else "mid")
        return (f'<span class="wr {cls}">{pct(p*100, 1)}</span>'
                f'<span class="ci">IC95 {lo*100:.0f}–{pct(hi*100, 0)}</span><br>'
                f'<span class="n">{w}W · {n - w}L</span>'
                f'<span class="verd {vc}">{vt}</span>')

    # Recálculo sob demanda: nada aqui roda sozinho, senão trava a aba Sinais.
    cp = get_perf()

    # Primeiro cálculo automático. Não roda dentro da janela de entrada para não
    # disputar o instante crítico com a varredura do sinal — nesse caso espera a
    # janela fechar. Depois disso, só recalcula quando você pede.
    if cp is None and not window_open:
        with st.spinner("Rodando o backtest pela primeira vez…"):
            cp = get_perf(calcular=True)

    IDADE_MAX = 5 * 60          # acima disso o recorte "Hoje" já não descreve o dia
    _idade = ((datetime.now(timezone.utc) - cp["quando"]).total_seconds()
              if cp else 0.0)
    _mudou_ctx = bool(cp) and cp["chave"] != (interval, len(analise_list),
                                              tuple(sorted(sel_strats)))
    _velho = bool(cp) and _idade > IDADE_MAX
    _alerta = _mudou_ctx or _velho

    bc1, bc2 = st.columns([0.62, 4], vertical_alignment="center")
    with bc1:
        pedir = st.button("Calcular" if cp is None else "Recalcular",
                          use_container_width=True, key="btn_perf",
                          type="primary" if _alerta else "secondary")
    with bc2:
        if cp is None:
            st.caption("Aguardando a janela de entrada fechar para rodar o backtest "
                       "— ele não disputa o instante do sinal.")
        else:
            q = f'calculado às {hm(cp["quando"])} em {nbf(cp["levou"], 1)}s'
            if _mudou_ctx:
                st.markdown(f'<div class="stale-cap">{q} — com <b>outro timeframe ou '
                            f'lista de ativos</b>. Recalcule.</div>',
                            unsafe_allow_html=True)
            elif _velho:
                st.markdown(f'<div class="stale-cap">{q}, há <b>{_idade/60:.0f} min</b>. '
                            f'A coluna “Hoje” muda a cada vela nova — recalcule para '
                            f'ver o dia corrente.</div>', unsafe_allow_html=True)
            else:
                st.caption(q)
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
                    f'· breakeven {pct(BE, 2)}</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl">{cab}{linhas(em_uso)}</table>', unsafe_allow_html=True)

        st.markdown('<div class="sect">Todas as estratégias · ordenado pela taxa do período'
                    '</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl">{cab}{linhas(outras)}</table>', unsafe_allow_html=True)

        # ---------- BACKTEST × AO VIVO ----------
        # O backtest não paga atraso de dados, execução nem spread. Se a taxa ao
        # vivo fica muito abaixo da simulada com amostra suficiente, isso não é
        # azar: é a diferença entre o mundo simulado e o real.
        _viv = {}
        for h in hist:
            if h["res"] not in ("ganhou", "perdeu"):
                continue
            for _s in h["strats"]:
                _viv.setdefault(_s, [0, 0])
                _viv[_s][0] += 1
                _viv[_s][1] += h["res"] == "ganhou"
        _linhas_cmp = ""
        for name in ranked:
            _sig = name.split(" · ")[0]
            nv, wv = _viv.get(_sig, [0, 0])
            n_bt, w_bt = perf[name]["per"]   # backtest (nao confundir com o helper num_br)
            if nv < 30 or n_bt == 0:
                continue
            pv, pb = wv / nv * 100, w_bt / n_bt * 100
            d = pv - pb
            cls = "bad" if d <= -5 else ("good" if d >= 5 else "mid")
            _linhas_cmp += (f'<tr><td class="nm">{name}</td>'
                            f'<td class="n">{wl(w_bt, n_bt)}</td>'
                            f'<td class="n">{wl(wv, nv)}</td>'
                            f'<td class="mono {cls}" style="font-weight:700">{d:+.1f} pp</td>'
                            f'</tr>')
        if _linhas_cmp:
            st.markdown('<div class="sect">Backtest × ao vivo</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Estratégia</th>'
                        f'<th>Backtest</th><th>Ao vivo</th><th>Diferença</th></tr>'
                        f'{_linhas_cmp}</table>', unsafe_allow_html=True)
            st.markdown('<div class="note">Uma queda consistente do backtest para o '
                        'ao vivo mede o custo do mundo real: dado que chega atrasado, '
                        'entrada que sai fora da abertura, preço que não é o mesmo. '
                        'Só aparecem estratégias com pelo menos 30 operações ao vivo — '
                        'abaixo disso a diferença é ruído.</div>',
                        unsafe_allow_html=True)

        # ---------- FORÇA DO SINAL: BACKTEST × AO VIVO ----------
        # No backtest a força sai direto do score, sem depender de nada gravado:
        # é a mesma régua do ao vivo (>=0,80 forte · >=0,60 média · >=0,50 fraca)
        # aplicada às mesmas velas. Assim as duas colunas são comparáveis de fato.
        # `.get` com padrão: um cache gravado por uma versão anterior do app não
        # tem esta chave, e sem o padrão a aba quebraria até o próximo recálculo.
        _bt_forca = (cp["dados"].get("forcas")
                     or {"FORTE": [0, 0], "MEDIA": [0, 0], "FRACA": [0, 0]})
        _viv_forca = {}
        for h in hist:
            if h.get("res") in ("ganhou", "perdeu") and h.get("tf") == minutes:
                _a = _viv_forca.setdefault(h.get("force", "FRACA"), [0, 0])
                _a[0] += 1
                _a[1] += h["res"] == "ganhou"

        if any(v[0] for v in _bt_forca.values()):
            _lf = ""
            for _k in ("FORTE", "MEDIA", "FRACA"):
                _nb_, _wb_ = _bt_forca[_k]
                _nv_, _wv_ = _viv_forca.get(_k, [0, 0])
                # Esconder a taxa abaixo de 20 operações foi um erro: o "—" parecia
                # defeito e não informava nada. A taxa aparece SEMPRE, ao lado das
                # contagens W/L — com "3W · 7L" à vista ninguém confunde 30% de 10
                # operações com 30% de mil. Abaixo de 20 ela só perde o destaque.
                _bt_txt = wl(_wb_, _nb_) if _nb_ else '<span class="n">—</span>'
                _vv_txt = wl(_wv_, _nv_)
                if _nb_ and _nv_:
                    _dd_ = _wv_ / _nv_ * 100 - _wb_ / _nb_ * 100
                    _dif_ = f'{_dd_:+.1f} pp'
                    _cls_ = ("mid" if _nv_ < 20 else
                             ("bad" if _dd_ <= -5 else ("good" if _dd_ >= 5 else "mid")))
                    if _nv_ < 20:
                        _dif_ += ' <span class="n">(amostra fina)</span>'
                else:
                    _dif_, _cls_ = '<span class="n">sem dados ao vivo</span>', ""
                _lf += (f'<tr><td class="nm">{FL[_k].capitalize()}</td>'
                        f'<td class="n">{_bt_txt}</td>'
                        f'<td class="n">{_vv_txt}</td>'
                        f'<td class="mono {_cls_}" style="font-weight:700">{_dif_}</td></tr>')
            st.markdown('<div class="sect">Força do sinal · backtest × ao vivo</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Força</th><th>Backtest</th>'
                        f'<th>Ao vivo</th><th>Diferença</th></tr>{_lf}</table>',
                        unsafe_allow_html=True)
            _be_f = breakeven(PAYOUT) * 100
            st.markdown(
                f'<div class="note">A pergunta que esta tabela responde é se score '
                f'maior acerta mais. No backtest a resposta sai na hora, com dezenas '
                f'de milhares de velas; ao vivo demora, e por isso a coluna só mostra '
                f'taxa a partir de 20 operações.<br>'
                f'Compare as linhas <b>entre si</b>, não com o breakeven de '
                f'{pct(_be_f, 1)}. Se forte e fraca derem a mesma coisa no backtest, a '
                f'força não carrega informação e restringir por ela só reduz o número '
                f'de operações. Se separarem no backtest mas não ao vivo, o que se '
                f'perdeu no caminho é execução, não estratégia.</div>',
                unsafe_allow_html=True)
            if min_force != "FRACA":
                st.caption(
                    # `min_force` vem do slider e JÁ está com acento ("MÉDIA");
                    # FL é indexado sem acento e explodia com KeyError aqui.
                    f"A *força mínima* está em **{min_force.lower()}**, então a "
                    f"coluna «ao vivo» nunca vai receber operações abaixo disso — o que "
                    f"não é gravado não pode ser analisado depois. Para comparar as três "
                    f"faixas ao vivo, deixe a força mínima em **fraca** por um período.")

        # ---------- BACKTEST POR ATIVO ----------
        # Qual PAR tem melhor comportamento histórico com as estratégias em uso.
        # A coluna que faz a diferença é o payout ao lado: taxa alta num par de
        # payout ruim ainda perde dinheiro — foi assim que o NZD/USD passou
        # despercebido a 30%. Ordena por MARGEM (taxa menos breakeven do próprio
        # ativo), não por taxa crua, justamente para o payout entrar na conta.
        _bt_ativos = cp["dados"].get("ativos") or {}
        if _bt_ativos:
            _linhas_a = []
            for nome, (n_, w_) in _bt_ativos.items():
                if n_ < 200:               # amostra pequena: mostra, mas sem ordenar bem
                    _margem = -999
                    _tx = (w_ / n_ * 100) if n_ else 0.0
                    _be = breakeven(payout_de(nome)) * 100
                else:
                    _tx = w_ / n_ * 100
                    _be = breakeven(payout_de(nome)) * 100
                    _margem = _tx - _be
                _linhas_a.append((nome, n_, w_, _tx, _be, _margem))
            _linhas_a.sort(key=lambda x: -x[5])
            linhas = ""
            for nome, n_, w_, _tx, _be, _margem in _linhas_a:
                if n_ < 200:
                    linhas += (f'<tr><td class="nm">{nome}</td>'
                               f'<td class="n">{wl(w_, n_)}</td>'
                               f'<td class="n mono">{pct(payout_de(nome)*100, 0)}</td>'
                               f'<td class="n mono">{pct(_be, 1)}</td>'
                               f'<td class="n"><span class="n">amostra pequena</span></td></tr>')
                    continue
                _cls = "good" if _margem >= 0.5 else ("bad" if _margem <= -0.5 else "mid")
                linhas += (f'<tr><td class="nm">{nome}</td>'
                           f'<td class="n">{wl(w_, n_)}</td>'
                           f'<td class="n mono">{pct(payout_de(nome)*100, 0)}</td>'
                           f'<td class="n mono">{pct(_be, 1)}</td>'
                           f'<td class="mono {_cls}" style="font-weight:700">'
                           f'{_margem:+.1f} pp</td></tr>')
            st.markdown('<div class="sect">Backtest por ativo</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Ativo</th><th>Acerto · W/L</th>'
                        f'<th>Payout</th><th>Breakeven</th><th>Margem</th></tr>'
                        f'{linhas}</table>', unsafe_allow_html=True)
            st.markdown(
                '<div class="note">A <b>margem</b> é a taxa menos o breakeven do '
                'próprio ativo — é ela, não a taxa, que diz se o par dá dinheiro, '
                'porque embute o payout. Um ativo de acerto alto e payout baixo '
                'aparece com margem negativa: foi o caso do NZD/USD (removido). '
                'Isto é backtest sobre dezenas de milhares de velas, não a sua '
                'operação real — serve para escolher onde focar, e some do vermelho '
                'para o verde conforme você melhora payout com a corretora.</div>',
                unsafe_allow_html=True)

        # ---------- MELHORES HORÁRIOS ----------
        tot_h = sum(v[0] for v in horas.values())
        if tot_h >= 500:
            st.markdown(f'<div class="sect">Horários · {mercado.lower()} · estratégias '
                        f'em uso · horário de Brasília</div>', unsafe_allow_html=True)
            N_H = 150                      # mínimo por hora para a barra valer algo
            # Horas em que a corretora abre ALGUM ativo. Fora delas o backtest
            # até tem dado, mas recomendar essas horas seria conselho impossível
            # de seguir: a Bullex fecha o forex inteiro das 16h às 21h.
            # BUG CORRIGIDO: passava o universo INTEIRO, incluindo cripto — que
            # não tem grade e libera as 24h, matando o filtro. O gráfico é "só
            # forex" (o próprio título diz), então o recorte de horas operáveis
            # tem que olhar só os ativos que ele mostra. Com cripto no meio, a
            # hora 17h aparecia como "boa" mesmo com a Bullex fechada.
            _h_ok = (horas_operaveis(GRADE_CORRETORA,
                                     [a["name"] for a in analise_list if a["type"] == "fx"])
                     if USAR_GRADE else set(range(24)))
            col = ""
            for h_ in range(24):
                n_, w_ = horas[h_]
                if USAR_GRADE and h_ not in _h_ok:
                    col += (f'<div class="hcol" title="{h_:02d}h · corretora fechada">'
                            f'<i class="vazio"></i>'
                            f'<span class="hh fechada">{h_:02d}</span></div>')
                    continue
                if n_ < N_H:
                    col += (f'<div class="hcol"><i class="vazio"></i>'
                            f'<span class="hh">{h_:02d}</span></div>')
                    continue
                p_ = w_ / n_ * 100
                _, lo_, hi_ = wilson_ci(w_, n_)
                acima = lo_ * 100 > BE      # IC inteiro acima: o critério de sempre
                cls_ = "bom" if acima else ("ruim" if p_ < BE else "neutro")
                alt = max(6, min(100, (p_ - 40) / 20 * 100))
                col += (f'<div class="hcol" title="{h_:02d}h · {w_}W · {n_ - w_}L · {pct(p_, 1)} '
                        f'(IC95 {lo_*100:.0f}–{pct(hi_*100, 0)})">'
                        f'<i class="{cls_}" style="height:{alt:.0f}%"></i>'
                        f'<span class="hh">{h_:02d}</span></div>')
            melhores = [(h_, horas[h_]) for h_ in range(24)
                        if horas[h_][0] >= N_H
                        and (h_ in _h_ok or not USAR_GRADE)
                        and wilson_ci(horas[h_][1], horas[h_][0])[1] * 100 > BE]
            melhores.sort(key=lambda kv: -kv[1][1] / kv[1][0])
            if melhores:
                txt = " · ".join(f"<b>{h_:02d}h</b> {pct(v[1]/v[0]*100, 1)}"
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
                        f'({pct(BE, 1)}); barras cinza têm menos de {N_H} operações.</div>',
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
        # Cinco filtros numa linha só. Antes eram quatro aqui e o quinto
        # (Configuração) ocupava a largura inteira sozinho na linha de baixo,
        # com o "?" solto lá na ponta — desequilibrava a grade toda.
        f1, f2, f3, f4, f5, f6 = st.columns([1.5, 1.1, 0.8, 0.8, 1.0, 1.3])
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
        with f5:
            _coortes = sorted({h.get("coorte") for h in hist if h.get("coorte")})
            f_coo = st.multiselect(
                "Configuração", _coortes, default=[],
                placeholder="Todas as configurações",
                help="Cada sinal guarda a configuração que estava ativa quando foi "
                     "gerado. Comparar taxas entre configurações diferentes não "
                     "responde nada — filtre por uma de cada vez.")
        with f6:
            # A força já vinha gravada em todo sinal desde o começo; faltava por
            # onde olhar. É a quebra mais acionável que existe: se ela separar,
            # basta subir a força mínima em Ajustes.
            f_frc = st.multiselect("Força", ["FRACA", "MEDIA", "FORTE"], default=[],
                                   format_func=lambda v: FL[v].capitalize(),
                                   placeholder="Todas")
        # Filtro Premium: separado dos demais porque a pergunta é diferente —
        # "quais entradas passaram nos seis critérios?". Marcar "Premium" isola
        # exatamente as operações que o painel Premium × normal está medindo.
        f_prem = st.radio("Premium", ["Todas", "Só premium", "Só não-premium"],
                          horizontal=True, index=0)
        vis = [h for h in hist
               if (not f_est or any(x in f_est for x in h["strats"]))
               and (not f_res or (h["res"] or "aguardando") in f_res)
               and (not f_tf or h.get("tf") in f_tf)
               and (not f_mkt or TIPO_ATIVO.get(h["asset"]) in f_mkt)
               and (not f_coo or h.get("coorte") in f_coo)
               and (not f_frc or h.get("force") in f_frc)
               and (f_prem == "Todas"
                    or (f_prem == "Só premium" and h.get("premium"))
                    or (f_prem == "Só não-premium" and not h.get("premium")))]
        filtrado = len(vis) != len(hist)
        if filtrado:
            st.caption(f"Filtro ativo: {len(vis)} de {len(hist)} sinais. "
                       f"Todos os números abaixo consideram apenas esse recorte.")
        if not vis:
            # Nada de st.stop() aqui: ele encerraria o script inteiro e levaria
            # junto o rodapé. As seções abaixo já degradam bem com lista vazia.
            st.info("Nenhum sinal atende aos filtros selecionados.")

        _mix = {h.get("coorte") for h in vis if h.get("coorte")}
        if len(_mix) > 1:
            st.markdown(
                f'<div class="win alert"><span class="pt"></span><div class="msg">'
                f'<b>Este recorte mistura {len(_mix)} configurações diferentes.</b> '
                f'Sinais gerados com força mínima, filtro ou mercado distintos estão '
                f'somados na mesma taxa — o número resultante não descreve nenhuma '
                f'delas. Use o filtro <i>Configuração</i> para olhar uma de cada vez.'
                f'</div></div>', unsafe_allow_html=True)

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
            taxa_txt = f'{pct(taxa, 1)}'
            sub = (f'IC95 {lo*100:.0f}–{pct(hi*100, 0)} · {txt} '
                   f'(breakeven {pct(_be_amostra, 1)})')
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
            + stat("Ganhos × perdas",
                   f'<span class="good">{g}</span>W · '
                   f'<span class="bad">{len(fechados) - g}</span>L',
                   f"de {len(fechados)} operações")
            + stat("Taxa do forward test", taxa_txt, sub, tcls)
            + '</div>', unsafe_allow_html=True)
        st.caption(f"Breakeven {pct(BE, 2)} com payout {payout_lbl}.")

        # ---- quanto falta para o veredito sair do "não conclusivo" ----
        if fechados and v == "inconclusivo":
            _falta, _lado = ops_para_concluir(g, len(fechados), _pay_amostra)
            if _falta:
                _dia_med = max(1, len(fechados) / max(1, len(
                    {br(h["ts"]).date() for h in fechados})))
                _dias = _falta / _dia_med
                st.markdown(
                    f'<div class="note"><b>Faltam cerca de {_falta} operações</b> para '
                    f'o intervalo de confiança sair inteiro {_lado} do breakeven, '
                    f'mantida a taxa atual de {pct(taxa, 1)}. No seu ritmo '
                    f'(~{_dia_med:.0f} por dia), isso é aproximadamente '
                    f'<b>{_dias:.0f} dias</b> de operação.</div>',
                    unsafe_allow_html=True)
            else:
                st.markdown(
                    f'<div class="note"><b>Nenhum tamanho de amostra resolveria isso.</b> '
                    f'Com {pct(taxa, 1)} contra um breakeven de {pct(_be_amostra, 1)}, a '
                    f'diferença é pequena demais: mesmo com 20 mil operações o intervalo '
                    f'continuaria cruzando a linha. Na prática significa que, com este '
                    f'payout, não há vantagem a ser encontrada — o caminho é negociar '
                    f'payout melhor, não acumular mais dados.</div>',
                    unsafe_allow_html=True)

        # ---- teste das duas metades ----
        # Se as duas metades discordam muito, o número agregado não descreve nada
        # estável. É o teste mais barato contra a ilusão de ter achado alguma coisa.
        _ord = sorted(fechados, key=lambda x: x["ts"])
        if len(_ord) >= 30:
            _meio = len(_ord) // 2
            _a, _b = _ord[:_meio], _ord[_meio:]
            _wa = sum(1 for x in _a if x["res"] == "ganhou")
            _wb = sum(1 for x in _b if x["res"] == "ganhou")
            _pa, _pb = _wa / len(_a) * 100, _wb / len(_b) * 100
            _dif = abs(_pa - _pb)
            _cls = "bad" if _dif >= 10 else ("mid" if _dif >= 5 else "good")
            _msg = ("as duas metades discordam bastante — o número agregado não "
                    "descreve um comportamento estável" if _dif >= 10 else
                    "diferença moderada entre as metades" if _dif >= 5 else
                    "as duas metades concordam, o que dá alguma confiança na estabilidade")
            st.markdown(
                f'<div class="note"><b>Teste das duas metades:</b> '
                f'primeira <b class="{_cls}">{pct(_pa, 1)}</b> ({len(_a)} ops) · '
                f'segunda <b class="{_cls}">{pct(_pb, 1)}</b> ({len(_b)} ops) · '
                f'diferença de {_dif:.1f} pontos — {_msg}.</div>',
                unsafe_allow_html=True)

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
                f'<span class="lg"><i class="be"></i>breakeven {pct(BE, 1)}</span></div>'
                f'<svg viewBox="0 0 100 {H}" preserveAspectRatio="none">'
                f'<line x1="0" y1="{y_be:.2f}" x2="100" y2="{y_be:.2f}" '
                f'stroke="var(--warn)" stroke-width=".4" stroke-dasharray="2 2" opacity=".8"/>'
                f'<polyline points="{pts}" fill="none" stroke="{cor}" stroke-width=".9" '
                f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>'
                f'<div class="c-foot"><span>1ª op</span>'
                f'<span class="mono">{pct(acum[-1], 1)} em {len(acum)} ops</span></div></div>',
                unsafe_allow_html=True)


        VTXT = {"acima": ('v-good', 'acima do breakeven'),
                "abaixo": ('v-bad', 'abaixo do breakeven'),
                "inconclusivo": ('v-mid', 'não conclusivo'),
                "sem dados": ('v-mid', 'sem dados')}

        def linha_ic(rot, sub, sinais, payout):
            """Linha de tabela com n, taxa, IC95 e veredito para um recorte."""
            f = [h for h in sinais if h["res"] in ("ganhou", "perdeu")]
            if not f:
                # 5 células, o mesmo do ramo normal. Quando "Ops" e "Acerto"
                # viraram uma coluna só, este ramo ficou com uma a mais e
                # desalinhava a tabela inteira sempre que um recorte vinha vazio.
                return (f'<tr><td class="nm">{rot}</td><td class="n">{sub}</td>'
                        f'<td class="n">{wl(0, 0)}</td><td class="n">—</td>'
                        f'<td><span class="verd v-mid">sem dados</span></td></tr>')
            w = sum(1 for h in f if h["res"] == "ganhou")
            _p, _lo, _hi = wilson_ci(w, len(f))
            cls, t = VTXT[verdict(w, len(f), payout)]
            return (f'<tr><td class="nm">{rot}</td><td class="n">{sub}</td>'
                    f'<td class="n">{wl(w, len(f))}</td>'
                    f'<td class="n mono">{_lo*100:.0f}–{pct(_hi*100, 0)}</td>'
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
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Tipo</th><th>Acerto · W/L</th>'
                        f'<th>IC95</th><th>Veredito</th></tr>{linhas}</table>',
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
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Tipo</th><th>Acerto · W/L</th>'
                        f'<th>IC95</th><th>Veredito</th></tr>{linhas}</table>',
                        unsafe_allow_html=True)
            st.caption("Só ganha sentido com algumas centenas de operações. Até lá os "
                       "intervalos vão ficar largos e o veredito, não conclusivo — isso é "
                       "o esperado, não um defeito.")

        # ---- PREMIUM × FLUXO NORMAL ----
        # A comparação que decide o experimento. Só entram operações gravadas com
        # a versão ATUAL dos critérios: misturar versões seria somar dois testes
        # diferentes na mesma taxa.
        _pv = [h for h in vis if h.get("prem_ver") == PREMIUM_VER
               and h["res"] in ("ganhou", "perdeu")]
        if _pv:
            _prem = [h for h in _pv if h.get("premium")]
            _norm = [h for h in _pv if not h.get("premium")]
            linhas = (linha_ic("Premium", f"v{PREMIUM_VER}", _prem, payout_do(_prem or _pv))
                      + linha_ic("Não premium", "fluxo normal", _norm,
                                 payout_do(_norm or _pv)))
            st.markdown('<div class="sect">Premium × fluxo normal</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Versão</th>'
                        f'<th>Acerto · W/L</th><th>IC95</th><th>Veredito</th>'
                        f'</tr>{linhas}</table>', unsafe_allow_html=True)

            # quanto falta para o Premium sair do "não conclusivo"
            _wp = sum(1 for h in _prem if h["res"] == "ganhou")
            if _prem:
                _pay_p = payout_do(_prem)
                _falta, _lado = ops_para_concluir(_wp, len(_prem), _pay_p)
                _tx = _wp / len(_prem) * 100
                if _falta:
                    st.markdown(
                        f'<div class="note">Premium está em <b>{pct(_tx, 1)}</b> '
                        f'({_wp}W · {len(_prem) - _wp}L). Mantida essa taxa, faltam '
                        f'cerca de <b>{_falta} operações premium</b> para o intervalo '
                        f'sair inteiro {_lado} do breakeven de '
                        f'{pct(breakeven(_pay_p) * 100, 1)}.</div>',
                        unsafe_allow_html=True)
                else:
                    st.markdown(
                        f'<div class="note">Premium está em <b>{pct(_tx, 1)}</b> contra '
                        f'um breakeven de {pct(breakeven(_pay_p) * 100, 1)}, e '
                        f'<b>nenhum tamanho de amostra resolveria</b> essa diferença — '
                        f'ela é pequena demais. Com este payout não há vantagem a '
                        f'encontrar por aqui; o caminho é payout melhor.</div>',
                        unsafe_allow_html=True)

            # por que os sinais reprovam: mostra qual critério está mordendo
            _mot_p = {}
            for h in _pv:
                for f_ in (h.get("prem_falhas") or []):
                    _mot_p[f_] = _mot_p.get(f_, 0) + 1
            if _mot_p:
                st.caption(
                    "Motivos de reprovação: "
                    + " · ".join(f"**{v}** {k}" for k, v in
                                 sorted(_mot_p.items(), key=lambda kv: -kv[1]))
                    + ". Um critério que reprova quase tudo está só reduzindo "
                      "amostra; um que nunca reprova não está filtrando nada.")
            st.caption(
                "Leia comparando as duas linhas. Se Premium não ficar claramente "
                "acima de «não premium», os seis critérios não estão comprando "
                "acerto — estão só comprando menos operações. E é assim que essa "
                "hipótese morre, se for para morrer: com número, não com opinião.")

        # ---- a força do sinal prevê alguma coisa? ----
        # Hipótese mais natural do sistema e nunca testada: score maior deveria
        # acertar mais. Se as três linhas ficarem coladas, a força não carrega
        # informação sobre acerto — e subir a força mínima passa a ser só perder
        # operação em troca de nada.
        _por_forca = {}
        for h in vis:
            _por_forca.setdefault(h.get("force", "FRACA"), []).append(h)
        if len(_por_forca) > 1:
            linhas = "".join(
                linha_ic(FL.get(k, k).capitalize(), "força", _por_forca[k],
                         payout_do(_por_forca[k]))
                for k in ("FORTE", "MEDIA", "FRACA") if k in _por_forca)
            st.markdown('<div class="sect">A força do sinal prevê acerto?</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Força</th><th>Tipo</th><th>Acerto · W/L</th>'
                        f'<th>IC95</th><th>Veredito</th>'
                        f'</tr>{linhas}</table>', unsafe_allow_html=True)
            st.caption(
                "Compare as linhas entre si, não cada uma com o breakeven. Se forte e "
                "fraca derem praticamente o mesmo, a força não separa nada — e aí não "
                "há por que restringir por ela. Lembre que dividir a amostra em três "
                "triplica o tempo até qualquer faixa concluir alguma coisa.")
        elif len(_por_forca) == 1:
            _k = next(iter(_por_forca))
            st.caption(
                f"Só há sinais de força **{FL.get(_k, _k).lower()}** no histórico, "
                f"então não dá para comparar forças. Para medir isso, a *força mínima* "
                f"em Ajustes precisa estar em **fraca** — ela decide o que chega a ser "
                f"gravado, e o que não é gravado nunca poderá ser analisado.")

        # ---- efeito do SEU atraso de execução ----
        # A vantagem que a estratégia mede está no movimento abertura->fechamento.
        # Comprando no meio da vela você já pagou o que o preço andou e aposta só
        # no que sobrou; sem sinal novo apontando para esse pedaço, ele tende a
        # se comportar como moeda ao ar. E 50% fica ABAIXO do breakeven — ou seja,
        # a expectativa é que atraso não piore um pouco, e sim jogue a operação
        # para o lado errado da linha. Aqui isso deixa de ser teoria minha.
        _ORD_PRO = ["na virada", "até 30s", "até 1min", "mais de 1min"]
        _exec_pro = [h for h in vis if h.get("exec") and h.get("prontidao")
                     and h["res"] in ("ganhou", "perdeu")]
        if _exec_pro:
            _por_pro = {}
            for h in _exec_pro:
                _por_pro.setdefault(h["prontidao"], []).append(h)
            linhas = ""
            for k in _ORD_PRO:
                v = _por_pro.get(k)
                if v:
                    linhas += linha_ic(k, "entrada", v, payout_do(v))
            _sem = [h for h in vis if h.get("exec") and not h.get("prontidao")
                    and h["res"] in ("ganhou", "perdeu")]
            st.markdown('<div class="sect">Efeito do seu atraso de execução</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Quando entrei</th><th>Tipo</th>'
                        f'<th>Acerto · W/L</th><th>IC95</th><th>Veredito</th>'
                        f'</tr>{linhas}</table>', unsafe_allow_html=True)
            st.caption(
                "Comprar no meio da vela não é a mesma operação que comprar na "
                "virada: o preço já andou e você aposta só no que sobra. A "
                "expectativa é que as linhas de baixo caiam na direção de 50%, que "
                "está abaixo do seu breakeven — mas é expectativa minha, e o "
                "número aqui é que decide."
                + (f" {len(_sem)} operação(ões) executada(s) ainda sem esse campo "
                   f"preenchido ficam de fora." if _sem else ""))

        # ---- os filtros de qualidade estão ajudando? ----
        # A comparação que interessa não é "quanto acertei", é "o que cortei
        # acertava menos do que o que passou?". Se o cortado acerta MAIS, o
        # filtro está tirando dinheiro do seu bolso, e sem esta tabela isso
        # ficaria invisível — o corte simplesmente não apareceria em lugar nenhum.
        _cortes = [h for h in hist_cortados if h.get("bloq")]
        if _cortes:
            _ROT = {"corpo": "cortado · vela sem corpo",
                    "atr": "cortado · volatilidade fora da faixa",
                    "noticia": "cortado · janela de notícia"}
            _por = {}
            for h in _cortes:
                _por.setdefault(h["bloq"], []).append(h)
            linhas = linha_ic("Passou nos filtros", "operado", vis, payout_do(vis))
            for k, v in sorted(_por.items()):
                linhas += linha_ic(_ROT.get(k, k), "não operado", v, payout_do(v))
            st.markdown('<div class="sect">Os filtros estão ajudando?</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Recorte</th><th>Tipo</th><th>Acerto · W/L</th>'
                        f'<th>IC95</th><th>Veredito</th></tr>{linhas}</table>',
                        unsafe_allow_html=True)
            st.caption("Leia a comparação, não o veredito de cada linha: o filtro só "
                       "vale a pena se o que ele cortou acertar MENOS do que o que "
                       "passou. Se acertar mais, desligue-o — ele está custando "
                       "operações boas. As linhas de corte não entram no resultado "
                       "financeiro, porque essas entradas não foram feitas.")

        # ---- calibração: qual limiar faria sentido? ----
        # Funciona mesmo com os filtros DESLIGADOS, porque as métricas da vela
        # são gravadas em todo sinal. É o jeito de escolher o corte olhando o
        # seu próprio histórico em vez de chutar um número redondo.
        def faixas(chave, rotulo, cortes, sufixo="%"):
            base = [h for h in hist_todos
                    if isinstance(h.get(chave), (int, float))
                    and h.get("res") in ("ganhou", "perdeu")]
            if len(base) < 20:
                return ""
            linhas_ = ""
            for lo, hi in zip(cortes[:-1], cortes[1:]):
                grupo = [h for h in base if lo <= h[chave] < hi]
                linhas_ += linha_ic(f"{lo:.0f}–{hi:.0f}{sufixo}", rotulo, grupo,
                                    payout_do(grupo) if grupo else PAYOUT)
            return linhas_

        _cal = (faixas("q_corpo", "corpo da vela", [0, 20, 35, 50, 70, 101])
                + faixas("q_atrp", "percentil de ATR", [0, 20, 40, 60, 80, 101]))
        if _cal:
            st.markdown('<div class="sect">Calibração dos limiares</div>',
                        unsafe_allow_html=True)
            st.markdown(f'<table class="tbl"><tr><th>Faixa</th><th>Métrica</th><th>Acerto · W/L</th>'
                        f'<th>IC95</th><th>Veredito</th></tr>{_cal}</table>',
                        unsafe_allow_html=True)
            st.caption("Cuidado com esta tabela: são 10 faixas testadas de uma vez, "
                       "então alguma vai parecer boa por acaso. Só mude um limiar se "
                       "a faixa ruim for consistentemente ruim ao longo de semanas — "
                       "escolher a melhor célula de hoje é como acertar o alvo depois "
                       "de atirar.")

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
                sub = (f'<span class="{cls_} mono" style="font-weight:700">{pct(tx, 1)}</span>'
                       f'<span class="n"> · {w_}/{len(f_)} resolvidos</span>')
            else:
                sub = '<span class="n">nenhuma resolvida</span>'
            rows += (f'<tr class="daysep"><td colspan="8">'   # 8 = nº de colunas
                     f'<span class="dlbl">{dia_lbl}</span>{sub}</td></tr>')
            for h in itens:
                vc, vt = VERD.get(h["res"], ("v-mid", "aguardando"))
                r = f'<span class="verd {vc}">{vt}</span>'
                dcls = "good" if h["dir"] == "COMPRA" else "bad"
                arw = "▲" if h["dir"] == "COMPRA" else "▼"
                chips_h = "".join(f'<span class="sc">{x}</span>' for x in h["strats"])
                _lg = h.get("lag")
                lag_txt = (f'{nbf(_lg, 1)}min' if isinstance(_lg, (int, float))
                           and math.isfinite(_lg) else "—")
                # Prova da apuração: os dois preços que decidiram o resultado.
                # Divergência com a corretora vira conferência de 10 segundos.
                _ao, _ac = h.get("ap_open"), h.get("ap_close")
                if isinstance(_ao, (int, float)) and isinstance(_ac, (int, float)):
                    _dcl = "good" if _ac > _ao else ("bad" if _ac < _ao else "")
                    ap_txt = (f'<span class="mono">{fmt_price(h["asset"], _ao)}'
                              f' → <span class="{_dcl}">{fmt_price(h["asset"], _ac)}</span>'
                              f'</span>')
                else:
                    ap_txt = '<span class="n">—</span>'
                # selo Premium: um ponto discreto ao lado do ativo, para
                # localizar de relance quais entradas passaram nos seis critérios
                _pm = ('<span class="dot-prem" title="Entrada premium: passou nos '
                       'seis critérios"></span>' if h.get("premium") else "")
                rows += (f'<tr><td class="n">{hm(h["ts"])}</td>'
                         f'<td class="nm">{h["asset"]}{_pm}</td>'
                         f'<td class="n mono">{h.get("tf", "—")}m</td>'
                         f'<td class="{dcls}" style="font-weight:800">{arw} {h["dir"]}</td>'
                         f'<td>{chips_h}</td>'
                         f'<td class="n">{ap_txt}</td>'
                         f'<td class="n mono">{lag_txt}</td><td>{r}</td></tr>')
        st.markdown(f'<div class="sect">Sinais registrados · {len(vis)} de {len(hist)}</div>', unsafe_allow_html=True)
        st.markdown(f'<table class="tbl"><tr><th>Hora</th><th>Ativo</th><th>TF</th>'
                    f'<th>Direção</th><th>Estratégias</th>'
                    f'<th>Abertura → fecham.</th><th>Atraso</th>'
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
        MOTIVOS = ["", "perdi a janela", "não gostei do setup",
                   "exposição concentrada", "limite/saldo", "outro"]
        # Prontidão da entrada. A vantagem que a estratégia mede está no
        # movimento ABERTURA -> FECHAMENTO. Comprando no meio da vela você paga
        # o que o preço já andou e aposta só no que sobrou — outra operação,
        # com outro ponto de partida. Sem separar as duas, a taxa agregada
        # mistura entrada limpa com entrada atrasada e não descreve nenhuma.
        PRONTIDAO = ["", "na virada", "até 30s", "até 1min", "mais de 1min"]
        _tab = pd.DataFrame([{
            "id": f'{h["asset"]}|{h["dir"]}|{h.get("ck")}|{h.get("tf")}',
            "Executei": bool(h.get("exec", False)),
            "Quando entrei": h.get("prontidao", ""),
            "Se não, por quê": h.get("motivo", ""),
            "Vela · expira": (br(h["ts"]).strftime("%d/%m ")
                              + hm_exp(h["ts"], h.get("tf") or minutes)),
            "Ativo": h["asset"], "Direção": h["dir"],
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
                "Quando entrei": st.column_config.SelectboxColumn(
                    "Quando entrei", options=PRONTIDAO, width="small",
                    help="Em que momento da vela você comprou. Entrada atrasada é "
                         "outra operação: o preço já andou e você aposta só no que "
                         "sobra. Preencher aqui é o que permite medir quanto isso "
                         "custa — o painel fica logo abaixo."),
                "Se não, por quê": st.column_config.SelectboxColumn(
                    "Se não, por quê", options=MOTIVOS, width="medium",
                    help="Saber o motivo revela se o atraso do app está custando "
                         "entradas, ou se a filtragem é sua."),
            },
            disabled=["Vela · expira", "Ativo", "Direção", "TF", "Estratégias",
                      "Resultado"])
        _mudou = False
        _mapa = dict(zip(_ed["id"], _ed["Executei"]))
        _mot = dict(zip(_ed["id"], _ed["Se não, por quê"]))
        _pro = dict(zip(_ed["id"], _ed["Quando entrei"]))
        for h in hist:
            k = f'{h["asset"]}|{h["dir"]}|{h.get("ck")}|{h.get("tf")}'
            if k in _mapa and bool(h.get("exec", False)) != bool(_mapa[k]):
                h["exec"] = bool(_mapa[k]); _mudou = True
            if k in _mot and (h.get("motivo") or "") != (_mot[k] or ""):
                h["motivo"] = _mot[k] or ""; _mudou = True
            if k in _pro and (h.get("prontidao") or "") != (_pro[k] or ""):
                h["prontidao"] = _pro[k] or ""; _mudou = True
        if _mudou:
            hist_save(hist_todos)          # ver comentário na aba Resultado
            st.rerun()

        _mots = {}
        for h in vis:
            if not h.get("exec") and h.get("motivo"):
                _mots[h["motivo"]] = _mots.get(h["motivo"], 0) + 1
        if _mots:
            _tot_m = sum(_mots.values())
            _txt_m = " · ".join(f'<b>{v}</b> {k}' for k, v in
                                sorted(_mots.items(), key=lambda kv: -kv[1]))
            _perdi = _mots.get("perdi a janela", 0)
            _obs = ("" if not _perdi else
                    f' Perder a janela em {pct(_perdi/_tot_m*100, 0)} dos casos é um '
                    f'problema de latência, não de estratégia — vale olhar a pastilha '
                    f'"sinal pronto em" na aba Sinais.')
            st.markdown(f'<div class="note"><b>Por que não executou:</b> {_txt_m}.'
                        f'{_obs}</div>', unsafe_allow_html=True)

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
                f'{_we}/{len(_exec)} = <b>{pct(_pe*100, 1)}</b> · IC95 '
                f'{_loe*100:.0f}–{pct(_hie*100, 0)} · {_tv}. Esta é a medida da sua '
                f'operação; a taxa geral acima inclui sinais que você não pegou.</div>',
                unsafe_allow_html=True)

    st.markdown('<div class="sect">Backup e importação</div>', unsafe_allow_html=True)
    b1, b2, b3 = st.columns([1, 1.4, 1])
    with b1:
        # Exporta TUDO, inclusive o que os filtros cortaram (coluna cortado_por).
        # O backup precisa poder reconstruir o histórico inteiro.
        df_h = hist_df(hist_todos)
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
