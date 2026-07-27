"""
grade_core.py — grade de horários da corretora (código PURO, sem Streamlit).

Auditoria Fase 4 (achado C-01): o parser e o avaliador da grade existiam em
DUAS cópias — app.py e scan_job.py. Cópias divergem: bastava ajustar o horário
de um ativo num lado e esquecer o outro para o app e o scanner discordarem
sobre "aberto na corretora", contaminando a coorte em silêncio. Este módulo é
a fonte única; os dois consumidores importam daqui.

Convenções:
- Horários da grade são no fuso de BRASÍLIA (como a corretora exibe).
- Dias no padrão Python: segunda=0 ... domingo=6.
- Grade = {dia_da_semana: [[ini, fim], ...]}; dia ausente = fechado; grade
  vazia/None = sem restrição (nunca fecha o que não conhece).
"""
from __future__ import annotations

# Grade lida da tela da Bullex (não existe API pública). Fonte única do DEFAULT;
# o app permite editar por cima via config — o texto daqui é o ponto de partida.
GRADE_BULLEX_TXT = {
    "EUR/USD": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    "GBP/USD": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    "USD/JPY": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    "EUR/JPY": "seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59",
    "AUD/USD": "seg-sex 00:00-14:00",
    "USD/CAD": "seg-sex 03:00-15:00",
    "EUR/GBP": "seg-sex 03:00-15:00",   # conferido na Bullex 21/07/2026
    # cripto negocia 24/7 na corretora: sem grade = sem restrição
}

DIAS_SIG = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5,
            "dom": 6}
DIAS_NOME = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

import re


def hhmm_min(txt):
    """'09:30' -> 570 minutos. None se não for um horário válido."""
    try:
        h, m = str(txt).strip().split(":")
        h, m = int(h), int(m)
        return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None
    except Exception:
        return None


def parse_grade(texto):
    """
    Texto -> {dia_da_semana: [[ini, fim], ...]}.

    Sintaxe (grupos por ';', faixas por ','), com prefixo de dias opcional:
        seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59
    Sem prefixo, vale a semana toda. Dia ausente = fechado naquele dia.
    Aceita acento ("sáb") — sem isso a linha era descartada em silêncio.
    """
    grade = {}
    if not texto:
        return grade
    for grupo in str(texto).split(";"):
        grupo = grupo.strip()
        if not grupo:
            continue
        dias, resto = None, grupo
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
            if hhmm_min(ini) is not None and hhmm_min(fim) is not None:
                faixas.append([ini.strip(), fim.strip()])
        if faixas:
            for dsem in dias:
                grade.setdefault(dsem, []).extend(faixas)
    return grade


def fmt_grade(grade):
    """
    {dia: faixas} -> texto compacto, agrupando dias com o mesmo horário.

    INVARIANTE (auditoria G-01): a saída DEVE ser re-parseável por parse_grade
    sem mudar de significado, porque o app reexibe o texto salvo e o re-parseia
    a cada rerun. A versão antiga juntava dias NÃO consecutivos com "/"
    ("seg/qua 09:00-12:00") — prefixo que o parser não reconhece, fazendo a
    faixa valer para a semana INTEIRA em silêncio. Agora cada bloco de dias
    vira um grupo próprio separado por ";" — sempre re-parseável.
    """
    if not grade:
        return ""
    porh = {}
    for dsem in range(7):
        chave = ", ".join(f"{a}-{b}" for a, b in grade.get(dsem, []))
        if chave:
            porh.setdefault(chave, []).append(dsem)
    partes = []
    for chave, dias in sorted(porh.items(), key=lambda kv: kv[1][0]):
        dias.sort()
        blocos, ini = [], dias[0]
        for i in range(1, len(dias) + 1):
            if i == len(dias) or dias[i] != dias[i - 1] + 1:
                fim = dias[i - 1]
                blocos.append(DIAS_NOME[ini] if ini == fim
                              else f"{DIAS_NOME[ini]}-{DIAS_NOME[fim]}")
                if i < len(dias):
                    ini = dias[i]
        # um grupo POR bloco de dias consecutivos: nunca precisa de "/"
        partes.extend(f"{bloco} {chave}" for bloco in blocos)
    return "; ".join(partes)


def aberto_em(bruto, weekday, minuto_do_dia):
    """
    O ativo está negociável dado o dicionário de grade, o dia da semana e o
    minuto do dia (AMBOS já no fuso da corretora — Brasília)?

    Regras herdadas do app (comportamento preservado e agora único):
    - sem grade -> True (nunca fecha o que não conhece);
    - dia sem faixa, mas grade tem faixas em outros dias -> fechado;
    - nenhuma faixa VÁLIDA em toda a grade -> True (grade corrompida não pode
      fechar o ativo o dia inteiro em silêncio);
    - faixa que atravessa a meia-noite (21:00-06:00) suportada.
    """
    if not bruto:
        return True
    if isinstance(bruto, dict):
        faixas = bruto.get(weekday, bruto.get(str(weekday)))
        if not faixas:
            return False if any(bruto.values()) else True
    else:
        faixas = bruto
    validas = 0
    for par in faixas:
        if not isinstance(par, (list, tuple)) or len(par) != 2:
            continue
        ini, fim = hhmm_min(par[0]), hhmm_min(par[1])
        if ini is None or fim is None:
            continue
        validas += 1
        dentro = (ini <= minuto_do_dia < fim) if ini < fim else (
            minuto_do_dia >= ini or minuto_do_dia < fim)
        if dentro:
            return True
    return False if validas else True


def horas_operaveis(grade, nomes):
    """
    Horas (0-23, Brasília) em que ALGUM dos ativos abre em dia útil.
    Ativo sem grade é IGNORADO (não libera as 24h — senão um único ativo sem
    grade mataria o filtro inteiro). Se nenhum tem grade, libera tudo.
    """
    horas = set()
    com_grade = 0
    for nome in nomes:
        bruto = (grade or {}).get(nome)
        if not bruto:
            continue
        com_grade += 1
        for dsem in range(5):             # segunda a sexta
            faixas = (bruto.get(dsem, bruto.get(str(dsem)))
                      if isinstance(bruto, dict) else bruto) or []
            for par in faixas:
                if not isinstance(par, (list, tuple)) or len(par) != 2:
                    continue
                ini, fim = hhmm_min(par[0]), hhmm_min(par[1])
                if ini is None or fim is None:
                    continue
                h_i, h_f = ini // 60, (fim - 1) // 60
                if ini < fim:
                    horas |= set(range(h_i, h_f + 1))
                else:                      # atravessa a meia-noite
                    horas |= set(range(h_i, 24)) | set(range(0, h_f + 1))
    return horas if com_grade else set(range(24))
