"""Paridade da grade centralizada com o comportamento validado em produção."""
import pandas as pd
import grade_core as G

GRADE = {n: G.parse_grade(t) for n, t in G.GRADE_BULLEX_TXT.items()}
BRT = "America/Sao_Paulo"


def _aberto(nome, iso_utc):
    t = pd.Timestamp(iso_utc).tz_localize("UTC").tz_convert(BRT)
    return G.aberto_em(GRADE.get(nome), t.weekday(), t.hour * 60 + t.minute)


def test_tabela_verdade_bullex():
    # a mesma matriz validada na correção do scanner (27/07/2026 = segunda)
    casos = [
        ("USD/CAD", "2026-07-27T12:00:00", True),    # seg 09:00 BRT
        ("USD/CAD", "2026-07-27T19:00:00", False),   # seg 16:00 BRT (>15:00)
        ("USD/CAD", "2026-07-25T12:00:00", False),   # sábado
        ("EUR/USD", "2026-07-26T23:30:00", False),   # dom 20:30 (só 22-23:59)
        ("EUR/USD", "2026-07-27T02:00:00", True),    # dom 23:00 BRT
        ("EUR/USD", "2026-07-25T10:00:00", False),   # sábado
        ("AUD/USD", "2026-07-27T10:00:00", True),    # seg 07:00 (00-14)
        ("BTC/USD", "2026-07-25T03:00:00", True),    # sem grade = sem restrição
    ]
    for nome, iso, esperado in casos:
        assert _aberto(nome, iso) == esperado, f"{nome} @ {iso}"


def test_parse_roundtrip_e_acento():
    g = G.parse_grade("seg-qui 00:00-15:30, 22:00-23:59; sex 00:00-15:30; dom 22:00-23:59")
    assert set(g) == {0, 1, 2, 3, 4, 6}, "sábado fechado (ausente)"
    assert g[0] == [["00:00", "15:30"], ["22:00", "23:59"]]
    assert G.parse_grade("sáb 09:00-12:00") == {5: [["09:00", "12:00"]]}
    txt = G.fmt_grade(g)
    assert G.parse_grade(txt) == g, "fmt_grade -> parse_grade tem de ser idempotente"


def test_grade_corrompida_nao_fecha_em_silencio():
    assert G.aberto_em({0: [["xx", "yy"]]}, 0, 600) is True
    assert G.aberto_em(None, 3, 600) is True


def test_meia_noite_atravessada():
    assert G.aberto_em({0: [["21:00", "06:00"]]}, 0, 22 * 60) is True
    assert G.aberto_em({0: [["21:00", "06:00"]]}, 0, 3 * 60) is True
    assert G.aberto_em({0: [["21:00", "06:00"]]}, 0, 12 * 60) is False


def test_horas_operaveis_ignora_sem_grade():
    grade = {"USD/CAD": GRADE["USD/CAD"]}
    hs = G.horas_operaveis(grade, ["USD/CAD", "BTC/USD"])
    assert 17 not in hs, "17h fechada não pode ser liberada por ativo sem grade"
    assert {3, 10, 14}.issubset(hs)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}"); ok += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{ok}/{len(fns)}")
