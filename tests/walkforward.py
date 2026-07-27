"""
Fase 6 — medição em walk-forward de melhorias de assertividade.
Dados REAIS: EUR/USD 5min 2018-2019 (Dukascopy-style), reamostrado para M15.
Regra de honestidade: limiares PRÉ-REGISTRADOS (os do app: corpo>=35, ATR 20-85,
confluência>=2, FORTE, ADX<25). Nada é ajustado nestes dados. A calibração de
horas usa SÓ a 1ª metade; TODA avaliação é na 2ª metade (out-of-sample).
"""
import numpy as np, pandas as pd, math, sys
sys.path.insert(0, "/tmp/audit")
import strategies as S

PAYOUT = 0.85
BE = S.breakeven(PAYOUT)
ESTR = ["E · Fade de rompimento", "F · Exaustão", "G · Fade vela extrema",
        "I · Fade extremo lateral", "J · Z-score forte", "K · Reversão dupla"]

# ---------- carga e reamostragem ----------
raw = pd.read_csv("/sessions/affectionate-dreamy-turing/mnt/outputs/binary_signals_tool/data/EURUSD_5min_2018-2019.csv")
raw["dt"] = pd.to_datetime(raw["Gmt time"], format="%d.%m.%Y %H:%M:%S.%f")
raw = raw.set_index("dt")[["Open", "High", "Low", "Close"]].sort_index()
m15 = raw.resample("15min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
print(f"velas M15 reais: {len(m15)}  ({m15.index[0]} -> {m15.index[-1]})")

d = S.add_indicators(m15)

# ---------- sinais agregados (regra do app) ----------
scores = {nm: S.score_of(nm, d, "15m") for nm in ESTR}
n = len(d)
o_next = d["Open"].shift(-1).values
c_next = d["Close"].shift(-1).values

# qualidade da vela de sinal (idêntico ao app: corpo% e percentil do ATR em 200)
rng_ = d["rng"].values
corpo = np.where(rng_ > 0, d["body"].values / np.where(rng_ == 0, np.nan, rng_) * 100, 0.0)
atr = d["atr"]
atrp = atr.rolling(200).apply(lambda w: (w <= w[-1]).mean() * 100.0, raw=True).values
adx = d["adx"].values
hora = d.index.hour  # GMT — consistente entre as metades; o rótulo BRT não muda o efeito

entries = []   # uma por (barra, direção): app agrega estratégias da mesma direção
for i in range(60, n - 1):
    if math.isnan(o_next[i]) or math.isnan(c_next[i]):
        continue
    por_dir = {}
    for nm in ESTR:
        v = float(scores[nm].iloc[i])
        r = S.classify(v)
        if not r:
            continue
        dd, ff = r
        e = por_dir.setdefault(dd, {"strats": [], "force": ff})
        e["strats"].append(nm)
        if {"FRACA": 1, "MEDIA": 2, "FORTE": 3}[ff] > {"FRACA": 1, "MEDIA": 2, "FORTE": 3}[e["force"]]:
            e["force"] = ff
    for dd, e in por_dir.items():
        if c_next[i] == o_next[i]:
            res = None                       # empate: refund, fora do denominador
        else:
            res = (c_next[i] > o_next[i]) == (dd == "COMPRA")
        entries.append({"i": i, "dir": dd, "nstr": len(e["strats"]),
                        "force": e["force"], "corpo": corpo[i], "atrp": atrp[i],
                        "adx": adx[i], "hora": int(hora[i]), "res": res,
                        "conflito": len(por_dir) > 1,
                        "strats": tuple(e["strats"])})
E = pd.DataFrame(entries)
meio = n // 2
E1, E2 = E[E["i"] < meio], E[E["i"] >= meio]      # 1ª metade = calibração; 2ª = avaliação

def medir(df):
    df = df[df["res"].notna()]
    nn = len(df)
    ww = int(sum(1 for v in df["res"] if bool(v)))
    if not nn:
        return (0, 0, float("nan"), float("nan"))
    wr = ww / nn
    return (nn, ww, wr * 100, (wr * (1 + PAYOUT) - 1) * 100)

def ztest(w1, n1, w2, n2):
    """z bicaudal para diferença de proporções (baseline vs filtro)."""
    if not n1 or not n2:
        return float("nan")
    p1, p2 = w1 / n1, w2 / n2
    p = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return float("nan")
    from statistics import NormalDist
    z = (p2 - p1) / se
    return 2 * (1 - NormalDist().cdf(abs(z)))

# calibração de horas na 1ª METADE apenas (Wilson-Bonferroni sobre as horas testadas)
h1 = E1[E1["res"].notna()].groupby("hora")["res"].agg(["count", "sum"])
h_testadas = h1[h1["count"] >= 100]
zb = S.z_for_comparisons(len(h_testadas))
boas_horas = [h for h, r in h_testadas.iterrows()
              if S.wilson_ci(int(r["sum"]), int(r["count"]), z=zb)[1] > BE]

base = medir(E2)
print(f"\nBASELINE 2ª metade (out-of-sample): n={base[0]}  wr={base[2]:.2f}%  EV={base[3]:+.2f}%/op   (BE={BE*100:.2f}%)")
print(f"horas calibradas na 1ª metade (Bonferroni m={len(h_testadas)}): {boas_horas or 'NENHUMA'}\n")

filtros = [
    ("corpo ≥ 35%",            E2[E2["corpo"] >= 35]),
    ("ATR pct 20–85",          E2[(E2["atrp"] >= 20) & (E2["atrp"] <= 85)]),
    ("confluência ≥ 2",        E2[E2["nstr"] >= 2]),
    ("só FORTE",               E2[E2["force"] == "FORTE"]),
    ("ADX < 25 (lateral)",     E2[E2["adx"] < 25]),
    ("sem conflito",           E2[~E2["conflito"]]),
    ("premium (conf+corpo+ATR)", E2[(E2["nstr"] >= 2) & (E2["corpo"] >= 35)
                                    & (E2["atrp"] >= 20) & (E2["atrp"] <= 85)]),
    ("horas calibradas (1ª metade)", E2[E2["hora"].isin(boas_horas)] if boas_horas else E2.iloc[0:0]),
]
print(f"{'filtro':32} {'n':>6} {'wr%':>7} {'EV%/op':>8} {'Δwr pp':>8} {'p-valor':>8}")
for nome, sub in filtros:
    m = medir(sub)
    if not m[0]:
        print(f"{nome:32} {0:>6}      —        —        —        —")
        continue
    dwr = m[2] - base[2]
    p = ztest(base[1], base[0], m[1], m[0])
    print(f"{nome:32} {m[0]:>6} {m[2]:>7.2f} {m[3]:>+8.2f} {dwr:>+8.2f} {p:>8.3f}")

# desativação automática: estratégias com wr < BE na 1ª metade saem na 2ª
por_estr_1 = {}
for nm in ESTR:
    sub = E1[E1["strats"].apply(lambda s: nm in s) & E1["res"].notna()]
    nn = len(sub)
    ww = int(sum(1 for v in sub["res"] if bool(v))) if nn else 0
    por_estr_1[nm] = (nn, ww, ww / nn * 100 if nn else float("nan"))
ativas = [nm for nm, (nn, ww, wr) in por_estr_1.items() if nn >= 100 and wr >= BE * 100]
E2_at = E2[E2["strats"].apply(lambda s: any(x in ativas for x in s))]
m = medir(E2_at)
print(f"\nDESATIVAÇÃO AUTOMÁTICA (1ª metade decide, 2ª avalia):")
for nm, (nn, ww, wr) in por_estr_1.items():
    print(f"  {nm:28} 1ªm: n={nn:5} wr={wr:6.2f}%  -> {'MANTIDA' if nm in ativas else 'DESATIVADA'}")
dwr = m[2] - base[2] if m[0] else float('nan')
p = ztest(base[1], base[0], m[1], m[0]) if m[0] else float('nan')
print(f"  resultado 2ª metade só com as mantidas: n={m[0]} wr={m[2]:.2f}% EV={m[3]:+.2f}%  Δ={dwr:+.2f}pp p={p:.3f}")
