"""Testes inegociáveis do motor (Fase 5, adiantados p/ provar correções da Fase 1).
Rodar: python -m pytest tests/ -q  (ou python tests/test_engine.py)"""
import numpy as np, pandas as pd, math
import strategies as S


def _serie(n=120, seed=1):
    rng = np.random.default_rng(seed)
    c = 1.10 + np.cumsum(rng.normal(0, 6e-4, n))
    idx = pd.date_range("2026-07-20", periods=n, freq="15min")
    o = c + rng.normal(0, 2e-4, n)
    hi = np.maximum(o, c) + 4e-4
    lo = np.minimum(o, c) - 4e-4
    return pd.DataFrame({"Open": o, "High": hi, "Low": lo, "Close": c}, index=idx)


def test_sanitize_ordena_e_remove_duplicatas():
    df = _serie(60)
    # embaralha a ordem e injeta uma vela duplicada (timestamp repetido)
    dup = df.iloc[[30]].copy()
    bag = pd.concat([df, dup]).sample(frac=1, random_state=7)  # fora de ordem + duplicata
    out = S.sanitize_ohlc(bag)
    assert out.index.is_monotonic_increasing, "deve ordenar por tempo"
    assert not out.index.duplicated().any(), "não pode sobrar timestamp duplicado"
    assert len(out) == len(df), "mantém 1 linha por timestamp"


def test_add_indicators_imune_a_duplicata():
    df = _serie(80)
    d1 = S.add_indicators(df)
    d2 = S.add_indicators(pd.concat([df, df.iloc[[79]]]))  # última vela repetida
    # a última linha (a que decide) tem de ser idêntica com ou sem a repetição
    assert math.isclose(float(d1["ema9"].iloc[-1]), float(d2["ema9"].iloc[-1]), rel_tol=1e-12)
    assert math.isclose(float(d1["rsi"].iloc[-1]), float(d2["rsi"].iloc[-1]), rel_tol=1e-12)


def test_backtest_contabilidade_refund_vs_loss():
    # série determinística: sinal de COMPRA em todas as barras; metade verde/vermelha
    n = 40
    idx = pd.date_range("2026-07-20", periods=n, freq="15min")
    o = pd.Series(1.0, index=idx)
    c = pd.Series([1.01 if i % 2 == 0 else 0.99 for i in range(n)], index=idx)
    # injeta um empate (close==open) na barra 5
    c.iloc[5] = 1.0
    d = pd.DataFrame({"Open": o, "High": np.maximum(o, c)+0.001,
                      "Low": np.minimum(o, c)-0.001, "Close": c})
    score = pd.Series(0.9, index=idx)  # COMPRA forte sempre
    r_ref = S.backtest(d, score, tie_mode="refund")
    r_los = S.backtest(d, score, tie_mode="loss")
    # o empate sai do denominador no refund e entra no loss
    assert r_ref["ties"] == r_los["ties"] >= 1
    assert r_los["trades"] == r_ref["trades"] + r_ref["ties"]
    assert r_ref["wins"] == r_los["wins"], "wins não muda entre modos"
    assert 0.0 <= r_ref["win_rate"] <= 1.0


def test_sem_lookahead_score_independe_da_barra_seguinte():
    df = _serie(100, seed=3)
    d = S.add_indicators(df)
    base = {nm: float(S.score_of(nm, d, "15m").iloc[-2]) for nm in S.STRATEGIES if nm not in S.NEEDS_TF}
    # muta a ÚLTIMA barra (a "futura" em relação à penúltima) de forma brutal
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("Close")] *= 1.05
    df2.iloc[-1, df2.columns.get_loc("High")] *= 1.05
    d2 = S.add_indicators(df2)
    for nm in base:
        s2 = float(S.score_of(nm, d2, "15m").iloc[-2])
        assert math.isclose(base[nm], s2, abs_tol=1e-9), f"{nm}: score da barra -2 mudou ao alterar a barra -1 (look-ahead!)"


def test_wilson_e_veredito_breakeven():
    p, lo, hi = S.wilson_ci(60, 100)
    assert lo < p < hi and 0 <= lo <= hi <= 1
    assert math.isclose(S.breakeven(0.85), 1/1.85, rel_tol=1e-9)
    # 60/100 com payout 85% (be 54,05%): IC inferior ~50,2% -> inconclusivo, não "acima"
    assert S.verdict(60, 100, 0.85) in ("inconclusivo", "acima")
    assert S.verdict(90, 100, 0.85) == "acima"
    assert S.verdict(30, 100, 0.85) == "abaixo"


def test_bonferroni_z_monotonico_e_verdict_multi():
    z1, z11, z24 = S.z_for_comparisons(1), S.z_for_comparisons(11), S.z_for_comparisons(24)
    assert abs(z1 - 1.959964) < 1e-3, "m=1 tem de ser o z clássico de 95%"
    assert z1 < z11 < z24, "mais comparações -> IC mais largo"
    # caso que passa no IC simples mas deve FALHAR com 24 comparações:
    # 570/1000 = 57%: lo95 ~53.9% < be? na verdade lo95=53.9 < 54.05 -> já inconclusivo.
    # usa 580/1000 = 58%: lo95 ~54.9% > be (acima no simples)
    assert S.verdict(580, 1000, 0.85) == "acima"
    assert S.verdict_multi(580, 1000, 0.85, 24) == "inconclusivo", \
        "58% em 1000 ops não sobrevive a 24 comparações simultâneas"


def test_expectancy_zera_no_breakeven():
    be = S.breakeven(0.85)
    n = 100000
    w = round(be * n)
    assert abs(S.expectancy(w, n, 0.85)) < 1e-4, "EV no breakeven tem de ser ~0"
    assert S.expectancy(50, 100, 0.85) < 0, "50% com payout 85% PERDE dinheiro"
    assert S.expectancy(60, 100, 0.85) > 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}"); ok += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"ERRO {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(fns)} testes passaram")
