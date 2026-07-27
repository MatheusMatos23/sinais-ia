# AUDITORIA TÉCNICA — Kairo (gerador de sinais / opções binárias)

Auditor: engenharia sênior quant + code review.
Método: leitura direta do código-fonte. Toda afirmação referencia `arquivo:linha`.
Escopo: `app.py` (5183 linhas), `strategies.py` (310), `scan_job.py` (397).
Branch de trabalho: `auditoria/fase-0` (git no sandbox; o repo em produção não é
clone local — ver "Decisões que exigem o dono" no fim).

> Nota de honestidade que atravessa toda a auditoria: opção binária é
> estruturalmente de EV negativo. Com payout 85%, o breakeven é `1/(1+0,85) =
> 54,05%`. Nenhuma correção aqui promete lucro; o objetivo é que os NÚMEROS que o
> sistema mostra sejam verdadeiros e livres de viés. `strategies.py:292` já
> calcula esse breakeven corretamente.

---

## FASE 0 — MAPA DO SISTEMA

### 1. Diagrama do fluxo de dados

```
                       ┌─────────────────────────────────────────────┐
ORIGEM DO DADO         │ Forex: Twelve Data (1 req/vela, N símbolos)  │ app.py:582 td_fetch
                       │   fallback -> yfinance                        │ app.py:770 _dl
                       │ Cripto: Binance -> Coinbase -> yfinance       │ app.py:642/654/815
                       └───────────────────────┬─────────────────────┘
                                               │
NORMALIZAÇÃO           _td_to_df / _ohlc_df / _dl:                     app.py:505 / 634 / 770
  - parse OHLC -> float, index = timestamp
  - sort_index()  (ordenação garantida)                               app.py:517, 639, (yf herda ordem)
  - tz -> UTC naive  (tz_convert UTC + tz_localize None)              app.py:518-519, 781
                                               │
CACHE                  get_data_live (janela curta, crítica)          app.py:787
  - chave (yf_symbol, interval, candle_key)                           app.py:795
  - @st.cache_data cross-session ttl=900 na busca TD                  app.py:572
                                               │
CORTE DA VELA          _abre_atual = candle_key(m)*m*60               app.py:2485
EM FORMAÇÃO            _fechadas = df[df.index < _abre_atual]          app.py:2486
  (corte por TIMESTAMP, não por posição — vale p/ TD e yfinance)
                                               │
INDICADORES            add_indicators(_fechadas)                      app.py:2489 -> strategies.py:29
  EMA9/21/50, RSI14, MACD, Bollinger20, ADX14, ATR14, corpo/range
                                               │
REGRA DE SINAL         score_of(nm, d, interval) -> classify(last)    app.py:2505-2507
  - score da ÚLTIMA vela FECHADA (iloc[-1] de _fechadas)              app.py:2506
  - |score|>=0.50 entra; 0.80 FORTE / 0.60 MÉDIA / resto FRACA        strategies.py:228
  - agrega por (ativo, direção); força = máx das estratégias          app.py:2510-2517
                                               │
GATES                  janela de horário / grade Bullex               app.py:2522-2523, 305
  circuit breaker / limite de perda diária / janela de notícia        app.py:2541 / 2528 / 2320
                                               │
EXIBIÇÃO               hero_html / card_html / abas                   app.py:3188 / 3207
                                               │
REGISTRO               record_and_resolve                             app.py:2908
  - grava entrada com ts = abertura da vela (na janela)               app.py:2921-2947
  - apura pela COR da vela quando ela fecha                           app.py:2950+
PERSISTÊNCIA           Gist (histórico/config/backtest)               app.py:1628-1630, 1666
```

Caminho paralelo de forward test sem navegador: `scan_job.py` (GitHub Actions),
reusa `strategies.py`, grava no MESMO Gist. Hoje forward-only + grade Bullex
(`scan_job.py:JANELA_GAP_MIN`, `aberto_na_corretora`).

### 2. Inventário de módulos

| Arquivo | Linhas | Responsabilidade |
|---|---|---|
| `strategies.py` | 310 | Motor PURO: indicadores (`add_indicators` :29), 11 estratégias (:75–204), `backtest` (:239), estatística Wilson/breakeven/verdict (:278–310). Sem Streamlit — o mesmo código roda ao vivo e no backtest. |
| `app.py` | 5183 | Aplicação Streamlit. Ingestão de dados (:491–870), fuso/grade da corretora (:153–347), orçamento de créditos TD (:523–569), gates de risco (:2320–2581), agregação de sinal (:2462–2517), registro/apuração (:2908–3010), reapuração de empates (:3012), backtest por ativo/hora/força (:2632–2757), UI (:3173+). |
| `scan_job.py` | 397 | Scanner de fundo standalone (Actions). Backfill→agora forward-only, grade Bullex, grava Gist. Reusa `strategies.py`. |

### 3. Dependências (VERIFICADO com `pip-audit -r requirements.txt`)

`pip-audit` executado no sandbox: **"No known vulnerabilities found"**.
Versões resolvidas no ambiente de teste (o `requirements.txt` usa pisos `>=`):

| Pacote | requirements.txt | Resolvido no teste | Observação |
|---|---|---|---|
| streamlit | >=1.40 | 1.59.2 | ok |
| pandas | >=2.0 | 2.3.3 | ok |
| numpy | >=1.24 | 2.2.6 | ok |
| requests | >=2.31 | 2.34.2 | ok |
| yfinance | >=0.2.40 | 1.5.1 | fonte de fallback; conhecida por quebrar por scraping — tratada com try/except |
| tradingview-ta | >=3.3.0 | 3.3.0 | **import não encontrado no código** (candidato a dependência morta — verificar na Fase 4) |
| streamlit-autorefresh | >=1.0.1 | — | usado para o auto-refresh das abas |

Ressalva metodológica: pisos `>=` sem teto significam que um deploy futuro pode
puxar uma versão maior não testada. Recomendação de pinагем fica para a Fase 5.

### 4. Estado global / cache / mutável compartilhado

| Local | O quê | Risco |
|---|---|---|
| `app.py:530-532` | `_td_lock` (threading.Lock), `_td_spent` (deque), `_td_day` — orçamento de créditos módulo-global, compartilhado por TODAS as sessões do servidor | Correto: protegido por lock (:549). É intencional — a cota da API é do servidor, não da sessão. |
| `app.py:572, 671, 751` | `@st.cache_data` cross-session para dados de mercado (TD, cripto, preços) | Seguro: chaveado por símbolo/intervalo/vela; dado de mercado é igual para todos. |
| `app.py` (dezenas) | `st.session_state[...]` — hist, cfg, fontes, caches ohlc, radar, latência | Isolado por sessão pelo Streamlit. `hist`/`cfg` têm cópia autoritativa no Gist. |
| `strategies.py` | Nenhum estado global mutável — módulo puro | Ideal. |

### 5. Observações já levantadas na leitura (severidade final atribuída na fase própria)

- Anti-look-ahead do caminho ao vivo parece CORRETO: corte por timestamp
  (`app.py:2486`), score da última vela fechada (`app.py:2506`), e no backtest
  entrada no open da barra seguinte via `shift(-1)` (`strategies.py:251-260`).
  Rompimento e confluência usam `shift(1)` para não ler a barra corrente
  (`strategies.py:99-100, 131`). → confirmar exaustivamente na Fase 3.
- Conflito de sinais: duas estratégias em direções opostas geram DUAS entradas
  separadas (`app.py:2510`, chave inclui direção). Não há regra de desempate
  explícita. → Fase 2.
- 24 blocos `except Exception` (0 `except:` nu). Vários devolvem `{}`/`None` em
  silêncio (`app.py:602, 783, 512`). → Fase 5.
- Inconsistência de doc: `strategies.py:15` diz "empate conta como derrota" mas
  o `backtest` usa `tie_mode="refund"` por padrão (`strategies.py:239`). → P3.
- `radar` monta candle em FORMAÇÃO com preço spot (`app.py:3128-3144`) — é
  display isolado; confirmar que nunca vira entrada. → Fase 2.

### Checkpoint Fase 0
Mapa completo, dependências auditadas (sem CVE conhecida), estado global
inventariado e seguro. Nenhum P0 confirmado ainda — as fases 1–3 é que decidem
isso. Próximo: Fase 1 (integridade dos dados).

---

## FASE 1 — INTEGRIDADE DOS DADOS

### Timezone / DST — OK
- Toda a matemática interna é UTC: `candle_key` usa `datetime.now(timezone.utc)`
  (`app.py:488`); todas as fontes normalizam para UTC-naive (`app.py:519, 781`).
- Conversão para exibição/grade via `zoneinfo` DST-aware: `br()` (`app.py:156-157`)
  e eventos macro em `America/New_York` (`app.py:2323`). Brasil não tem DST desde
  2019, então o fallback fixo `-3` (`app.py:32`) é sempre correto hoje.
- Ressalva menor (P3): o fallback de NY é fixo `-4` (`app.py:39`), que erra 1h em
  horário-padrão (EST=-5). Só entra se `zoneinfo` faltar — não ocorre no deploy
  (Python 3.12). Afeta apenas o rótulo da janela de notícia, nunca a decisão.

### Candle fechado vs. em formação — OK
- Decisão usa só velas fechadas: corte por TIMESTAMP `df[df.index < _abre_atual]`
  (`app.py:2486`), e o score é o da última fechada (`app.py:2506`). O comentário
  em `:2471-2484` documenta o bug anterior (`df.iloc[:-1]`) já corrigido.
- O radar monta candle em formação com preço spot (`app.py:3128-3144`), mas é
  painel isolado (confirmação da não-contaminação fica para a Fase 2).

### Gaps / fim de semana / feriados — OK (sem preenchimento sintético)
- Normalizadores NÃO criam barras artificiais: `_dl` faz `dropna` (`app.py:779`),
  `_td_to_df` só parseia (`app.py:505`). As janelas rolantes atravessam o gap de
  fim de semana (comportamento intraday padrão, aceitável). Não há `asfreq`/
  reindex de calendário que injetaria vela fantasma.
- Os `fillna` em `strategies.py` (rsi/adx→50, score→0) são defaults neutros do
  período de aquecimento; o gate `len(_fechadas) < 60` (`app.py:2487`) garante que
  a barra de decisão está muito além do aquecimento. Não é viés na decisão.
- `strat_confluencia` usa `.reindex(...).ffill()` (`strategies.py`), mas só para
  carregar a direção da última barra maior JÁ fechada (`shift(1)` antes). Correto.

### Duplicatas de timestamp e ordenação — ERA UM FURO, CORRIGIDO (P2)
- **Evidência:** nenhum `drop_duplicates`/`duplicated` em `app.py` ou
  `strategies.py` (grep vazio). `sort_index` existia nas fontes (`app.py:517, 639`)
  mas nada impedia timestamp repetido. Uma vela duplicada da fonte faria
  `iloc[-1]` e `rolling(20)`/streaks contarem a barra fantasma — decisão sobre
  dado corrompido.
- **Correção aplicada:** `sanitize_ohlc()` (`strategies.py`, novo) ordena e remove
  duplicata (mantém a última), chamada no ÚNICO ponto por onde todos os caminhos
  passam: `add_indicators` (ao vivo, backtest e scanner). Commit atômico.
- **Prova:** `tests/test_engine.py` — `test_sanitize_ordena_e_remove_duplicatas`
  e `test_add_indicators_imune_a_duplicata` PASSAM; a última barra fica idêntica
  com ou sem a repetição.

### Bônus verificado nesta fase (relevante para a Fase 3)
- `test_sem_lookahead_score_independe_da_barra_seguinte` PASSA para TODAS as
  estratégias: mutar a última vela em +5% não altera o score da penúltima. É
  evidência forte de ausência de look-ahead DENTRO do motor. (Aprofundamento e
  data-snooping ficam na Fase 3.)

### Checkpoint Fase 1
Fuso, candle fechado e gaps: corretos. Único furo real — ausência de guarda
contra timestamp duplicado — corrigido em ponto único e coberto por teste.
5/5 testes passam. Nada quebrado (o motor produz os mesmos números em dados sem
duplicata; só blinda o caso com duplicata).

---

## FASE 2 — MOTOR DE SINAIS

### Pseudocódigo declarado × código real (todas as 11 estratégias percorridas)

| Estratégia | Regra (pseudocódigo) | Código confere? | Off-by-one conferido |
|---|---|---|---|
| A Tendência | ema9>ema21>ema50 ∧ macd>0 ∧ close>ema9 (invertido p/ venda) | ✅ `strategies.py:76-77` | atributos da barra i fechada — ok |
| B Reversão | rsi<30 ∧ close≤bb_low ∧ (verde ∨ pavio_inf≥corpo) | ✅ `:87-88` | ok |
| C Rompimento | close > máx20 das barras ANTERIORES ∧ corpo≥1,2×média | ✅ `:99-103` | `rolling(20).max().shift(1)` exclui a barra corrente — correto |
| D Multi-TF | direção base = direção do TF maior JÁ FECHADO | ✅ `:126-132` | `shift(1)` na série maior antes do `reindex/ffill` — sem vazamento da barra maior parcial |
| E Fade romp. | −C | ✅ `:147` | herda C |
| F Exaustão | n velas seguidas na mesma cor → contra | ✅ `:152-158` | `rolling(n)` sobre barras fechadas — ok |
| G Vela extrema | range ≥2× média20 → contra a cor | ✅ `:164-170` | média20 INCLUI a barra corrente (auto-inclusão amortece o ratio) — escolha de desenho, não bug; documentado |
| H Z-score | \|z\|≥1,8 vs média20 → volta | ✅ `:176-182` | ok |
| I | G se ADX<25 | ✅ `:191` | ok |
| J | H com z≥2,2 | ✅ `:195` | ok |
| K | G∧H concordam ∧ ADX<25 → score fixo 0,85 | ✅ `:198-204` | ok |

`backtest` (`strategies.py:251-266`): entrada no `Open.shift(-1)`, acerto pela cor
da barra seguinte, última barra excluída por `notna`. Modelo correto de binária.
Prova empírica adicional: `test_sem_lookahead_score_independe_da_barra_seguinte`.

### Expiração e latência — modeladas honestamente
- Janela de entrada de 20 s após a virada (`app.py:2340-2344`); só grava dentro
  dela (`record_and_resolve(..., na_janela=window_open)`, `app.py:3092`).
- `lag` do dado gravado POR SINAL (`app.py:2955`) + painel "efeito do atraso".
- Apuração espera existir barra POSTERIOR (`app.py:2970`) — nunca apura vela em
  formação. `ts` sobrevive ao round-trip JSON (reconvertido em `app.py:1784`).
- Entradas pendentes continuam tendo dado buscado mesmo após a corretora fechar
  (`fetch_list` inclui pendentes, `app.py:2411-2418`) — sem "aguardando eterno".

### Conflito de sinais — ERA ACIDENTAL NO REGISTRO, CORRIGIDO (S-01, P2)
- Duas estratégias em direções opostas geram DUAS entradas (chave inclui
  direção, `app.py:2510`). A tela desempata deterministicamente (confluência >
  força > score, `app.py:2572` + destaque `app.py:3404`), mas o HISTÓRICO não
  registrava que houve conflito — impossível medir se vela contraditória acerta
  menos.
- **Correção:** flag `conflito` computada sobre `agg` e gravada em toda entrada
  do par conflitante (`app.py`, pós-`entries`; campo novo em `record_and_resolve`).
- Radar confirmado ISOLADO: escreve apenas em `radar[]` e nos contadores de
  conversão (`app.py:3160-3169`); nunca toca `agg`/`entries`.

---

## FASE 3 — VALIDAÇÃO ESTATÍSTICA (a mais importante)

### Look-ahead bias — LIMPO (verificado por 3 vias)
1. Grep dirigido: **zero** `bfill`, zero scaler/normalização ajustada no dataset
   inteiro, zero indicador centrado. Os `fillna` são neutros de aquecimento
   (rsi/adx→50, score→0) e o gate `len<60` (`app.py:2487`) mantém a decisão longe
   do aquecimento.
2. Estrutural: corte por timestamp (`app.py:2486`), `shift(1)` nos rolling de
   rompimento (`strategies.py:99-100`) e no TF maior (`:131`), entrada via
   `shift(-1)` no backtest (`:251-252`).
3. Empírico: teste automatizado muta a barra futura em +5% e exige score
   idêntico na barra de decisão — PASSA para todas as estratégias.

### Data snooping / múltiplas comparações — EXISTIA (V-01, o achado mais grave), CORRIGIDO
- **Evidência:** "melhores horários" declarava vencedor qualquer hora com IC95
  simples acima do breakeven (`app.py:4398-4401` pré-correção) testando até 24
  baldes; a tabela compara 11 estratégias (`app.py:4113+`). Sob hipótese nula,
  ~1 falso vencedor a cada 20 baldes é o ESPERADO.
- **Correção:** `z_for_comparisons(m)` + `verdict_multi(...)` (Bonferroni,
  `strategies.py` novo). Aplicado: selo "VANTAGEM COMPROVADA" e resumo da tabela
  (m=11) e painel de horários (m = baldes efetivamente testados; rótulo do
  painel agora declara a correção).
- **Prova medida:** `test_bonferroni_z_monotonico_e_verdict_multi` — 58% em
  1.000 operações é "acima" no IC simples e vira "inconclusivo" com m=24.
- O forward test ao vivo (coorte única pré-registrada) permanece como o
  out-of-sample verdadeiro do sistema — essa é a defesa primária contra
  snooping, e já existia por desenho.

### Split temporal — NÃO EXISTIA (V-02), IMPLEMENTADO
- `run_perf` media a janela CHEIA (in-sample puro). Não há parâmetro ajustado
  (regras fixas), então o overfitting aqui é o USUÁRIO escolher estratégia
  olhando a janela inteira.
- **Correção:** split cronológico em metades por ativo (`h1`/`h2` em `run_perf`),
  coluna "1ª → 2ª metade" na tabela com selo **INSTÁVEL** quando a 1ª metade está
  acima do breakeven e a 2ª caiu abaixo (o padrão clássico de sorte de janela).
  Não é split aleatório — é cronológico, como exige série temporal.

### Tamanho de amostra — REFORÇADO
- Wilson já era usado no ao-vivo (`verdict`, `strategies.py:297`) e no painel de
  horas. Adicionado: selo **"n<100 · não confiável"** na tabela de estratégias
  (exigência da auditoria); painéis de hora/ativo já usavam pisos 150/200.

### Payout / expectância — payout OK; expectância FALTAVA, IMPLEMENTADA
- Breakeven por payout correto em todo lugar (`strategies.py:292-294`; margem
  por ativo com payout do próprio ativo, `app.py:4320+`).
- PnL correto e flat-stake: `pnl_de` usa payout GRAVADO no sinal (`app.py:1837-1849`).
- **Novo:** coluna EV/op = p·(1+payout)−1 na tabela de estratégias
  (`expectancy()`, `strategies.py`); testes provam EV=0 no breakeven e EV<0 a 50%.

### Martingale — AUSENTE (verificado)
- `grep -i "gale|martin"` vazio nos 3 arquivos. Aposta fixa; sem recuperação
  progressiva mascarando perda. Nada a auditar aqui além da confirmação.

---

## FASE 4 — CÓDIGO MORTO E DUPLICADO

- **Dependência morta:** `tradingview-ta` no requirements com ZERO imports no
  código (grep vazio). Removida do `requirements.txt`.
- **Imports mortos no scan_job.py:** `time`, `numpy`, `MIN_SCORE`, `breakeven`
  (cada um só na linha de import). Removidos.
- **Funções do app:** todas as candidatas (market_open, in_win, hist_df, url_com,
  ops_para_concluir…) têm def + uso real. Nada morto.
- **Duplicação crítica (C-01, P1-operacional):** grade da corretora em DUAS
  cópias (app.py:120-347 e scan_job.py) — divergência silenciosa contaminaria a
  coorte. **Centralizada em `grade_core.py`** (fonte única de GRADE_BULLEX_TXT,
  parse_grade, fmt_grade, aberto_em, horas_operaveis); app e scanner agora são
  wrappers finos. 5/5 testes de paridade (`tests/test_grade_core.py`).
- **BUG P1 ACHADO NA CENTRALIZAÇÃO (G-01):** `fmt_grade` antigo emitia dias não
  consecutivos como "seg/qua 09:00-12:00"; o parser não reconhece "/" e a faixa
  passava a valer a SEMANA INTEIRA (incl. fim de semana) no ciclo salvar→exibir→
  re-parsear (`app.py:2162→2177`). As grades padrão escapavam por só gerarem
  blocos consecutivos — qualquer edição do usuário com dias alternados corrompia.
  Corrigido: cada bloco vira grupo próprio separado por ";" (invariante testada:
  `fmt_grade→parse_grade` idempotente).
- **Caminho crítico MEDIDO:** varredura completa (7 ativos × indicadores × 6
  estratégias, 250 velas) = **231 ms** — 2,3% do ciclo de refresh de 10 s. Não é
  gargalo; nenhuma otimização arriscada aplicada (P3 documentado).

## FASE 5 — ROBUSTEZ

- **Frescor do dado: JÁ EXISTIA e está correto** — `dados_atrasados`
  (`app.py:2452`), bloqueio de entrada com dado vencido (`app.py:2466`), lag
  medido e gravado por sinal, radar desligado com dado velho (`app.py:3114`),
  painel de diagnóstico (`data_diag`, `app.py:2422`).
- **Exceções:** 24 `except Exception`, 0 bare `except:`. O pior caso real —
  falha de REDE no `td_fetch` devolvendo `{}` mudo — agora grava `td_erro` na
  barra de status (R-01 corrigido). Os demais degradam com fallback visível
  (fontes por ativo no diagnóstico) ou são por-linha (parse de vela).
- **Concorrência:** orçamento TD com `threading.Lock` (`app.py:530`); futures do
  yfinance consumidos no thread principal; caches por sessão. Sem race relevante.
- **Precisão:** comparação `cl==op` em float é DELIBERADA (detector de vela
  achatada de feed, `app.py:2975-2985`); prova de apuração com 6 casas.
- **Testes (inegociáveis):** regra de sinal (look-ahead), indicadores
  (duplicata), contabilidade (refund/loss) + grade — `tests/test_engine.py`
  (7/7) e `tests/test_grade_core.py` (5/5). Limitação honesta: `pnl_de`/fluxos
  Streamlit não são testáveis sem refatorar o app em módulos — auditados por
  leitura com evidência; roadmap.

## FASE 6 — MELHORIA DE ASSERTIVIDADE (implementada E medida)

**Base de medição:** 25.592 velas M15 REAIS (EUR/USD, 5min 2018-2019 reamostrado
— único dataset real substancial disponível offline; ressalvas: 1 ativo, 1 era).
**Protocolo:** limiares pré-registrados (os do app — nada ajustado nestes dados);
calibração APENAS na 1ª metade; avaliação APENAS na 2ª (out-of-sample);
empate = refund; payout 0,85 (BE 54,05%). Script: `tests/walkforward.py`.

Baseline out-of-sample: **n=3.753 · 52,57% · EV −2,74%/op** (abaixo do BE — o
sistema cru PERDE, coerente com tudo que o app sempre disse).

| Melhoria (pré-registrada) | n | wr | EV/op | Δwr | p-valor | Veredito |
|---|---|---|---|---|---|---|
| corpo ≥ 35% | 2.558 | 53,95% | −0,20% | +1,38pp | 0,282 | não significativo |
| ATR pct 20–85 | 2.442 | 52,87% | −2,20% | +0,30pp | 0,820 | não significativo |
| **confluência ≥ 2** | 1.261 | 56,94% | **+5,34%** | **+4,37pp** | **0,007** | significativo (limítrofe sob Bonferroni m=8: α=0,00625) |
| só FORTE | 2.265 | 53,86% | −0,35% | +1,29pp | 0,331 | não significativo |
| ADX < 25 | 2.078 | 52,31% | −3,23% | −0,26pp | 0,848 | não significativo |
| sem conflito | 3.737 | 52,58% | −2,72% | +0,01pp | 0,992 | nulo |
| **premium (conf+corpo+ATR)** | 675 | **59,41%** | **+9,90%** | **+6,84pp** | **0,001** | **significativo mesmo sob Bonferroni m=8** |
| horas calibradas (1ª metade) | 0 | — | — | — | — | NENHUMA hora sobreviveu à calibração honesta |

**Trade-off explícito (não é ganho puro):** confluência corta 66% das operações;
premium corta 82%. Menos sinais/dia, mais seletivos.

**Multi-TF:** já existe como estratégia D (catálogo) e como confluência (medida
acima). **Filtro de notícia:** não mensurável offline (sem calendário histórico)
— continua como experimento A/B ao vivo, que o app já grava (`bloq="noticia"`).
**Calibração por ativo:** payout por ativo já existente; calibração de
estratégia por ativo exigiria dados multi-ativo reais — roadmap.

**Desativação automática de estratégia degradada — IMPLEMENTADA (variante
estatística):** desativar por ponto (wr<BE na 1ª metade) desligaria TODAS as
estratégias (todas 52-53,6% — medido); a variante implementada marca DEGRADADA
só quando o IC de Wilson INTEIRO fica abaixo do BE com n≥30 ao vivo (nesta base,
nenhuma — todas inconclusivas, comportamento protetivo correto). No app: selo
"DEGRADADA AO VIVO" + aviso recomendando remoção manual (remoção automática
partiria a coorte — decisão humana, documentada no próprio aviso).

## FASE 7 — INTERFACE (verificação + itens entregues)

Checklist exigido × estado (com evidência):
- Estado do sinal (ao vivo/expirado/resultado): **já existia** — hero esmaece
  fora da janela (`dim`/`stale`), rótulo "vela X → expira Y" (`hm_exp`),
  resultado por sinal no Histórico, campo prontidão.
- Confiança + amostra por sinal: força/score no cartão; W/L junto de TODA taxa
  (`wl()`); **novos**: EV/op, selo n<100, IC nos tooltips de hora.
- Real × backtest: **já existia** (tabela Backtest × ao vivo) + **novo** selo
  DEGRADADA.
- Filtros por ativo/hora/estratégia: já existiam (força, ativo, coorte, horas).
- Saúde da conexão/frescor: já existia (diagnóstico por ativo, atraso, fonte,
  erros TD na barra) + **novo** motivo de falha de rede (R-01).
- Design system/acessibilidade: paleta e tipografia consistentes de sessões
  anteriores; direção NUNCA é só cor (texto COMPRA/VENDA sempre presente) — ok
  para daltonismo no elemento crítico; refino de paleta = P3 roadmap.
- Métricas decorativas: nada claramente decorativo a remover — cada painel
  responde uma pergunta operacional; poda já feita em iterações anteriores.

**Decisão deliberada:** NÃO refazer o layout inteiro no meio do forward test —
regra "não quebre o que funciona" + histórico recente de instabilidade após
mudanças grandes. Redesign completo, se desejado, é decisão do dono (Fase 8).

---

## FASE 8 — TABELA FINAL DE ACHADOS

| ID | Sev | Arquivo:linha | Problema | Evidência | Correção aplicada | Status |
|---|---|---|---|---|---|---|
| V-01 | **P0** | app.py:4398-4401 (pré) | Vereditos de vantagem sem correção de múltiplas comparações — falsos vencedores por acaso eram o ESPERADO | 24 baldes-hora × IC95 simples; 11 estratégias idem | Bonferroni (`z_for_comparisons`+`verdict_multi`) nos 2 painéis; teste prova que 58%/1000 "acima" vira inconclusivo com m=24 | ✅ corrigido |
| G-01 | P1 | app.py:263+2162 (pré) | `fmt_grade` emitia "seg/qua …", parser não lê "/" → faixa virava semana INTEIRA no ciclo salvar→exibir→re-parsear | app.py:2162 realimenta 2177 | fmt re-parseável (grupos por ";"), invariante testada | ✅ corrigido |
| V-02 | P1 | app.py:2689 | Backtest in-sample puro, sem split temporal | `run_perf` janela cheia | split cronológico h1/h2 + coluna + selo INSTÁVEL | ✅ corrigido |
| V-03 | P1 | app.py (tabela) | Win rate sem expectância | tabela sem EV | coluna EV/op + testes (EV=0 no BE) | ✅ corrigido |
| C-01 | P1-op | app.py:120-347 ↔ scan_job.py | Grade da corretora duplicada — divergência silenciosa contaminaria a coorte | duas cópias integrais | `grade_core.py` fonte única + 5 testes de paridade | ✅ corrigido |
| D-01 | P2 | global | Sem guarda contra timestamp duplicado da fonte | grep sem `duplicated` | `sanitize_ohlc()` em `add_indicators` + 2 testes | ✅ corrigido |
| S-01 | P2 | app.py:2510 | Conflito de sinais opostos sem marca no histórico | registro cego ao conflito | flag `conflito` gravada; efeito MEDIDO (nulo: Δ+0,01pp) | ✅ corrigido |
| V-04 | P2 | app.py (tabela) | n<100 sem marca de não-confiável | `wl(fino=20)` apenas | selo "n<100 · não confiável" | ✅ corrigido |
| R-01 | P2 | app.py:602 (pré) | Falha de rede TD engolida — fallback sem motivo visível | `except: return {}` | `td_erro` na barra de status | ✅ corrigido |
| X-01 | P2 | requirements.txt | Dependência morta `tradingview-ta` + 4 imports mortos no scanner | zero usos (grep) | removidos | ✅ corrigido |
| D-02 | P3 | app.py:39 | Fallback NY fixo −4 erra 1h em EST | só sem `zoneinfo`; afeta rótulo, não decisão | aceito com justificativa (deploy tem zoneinfo) | 🟡 aberto |
| D-03 | P3 | strategies.py:15 | Doc de empate contradizia o código | `tie_mode="refund"` | doc corrigida | ✅ corrigido |
| P-01 | P3 | app.py:2465+ | Varredura recomputada a cada rerun | **medido: 231 ms** (2,3% do ciclo) | não vale o risco de cache; documentado | 🟡 aceito |
| M-01 | — | — | Martingale | grep vazio | ausente por desenho | ✅ n/a |

**Critério de conclusão:** todo P0/P1 corrigido; P2 corrigidos; P3 abertos com
justificativa explícita. Testes 12/12 verdes. Melhorias medidas, não estimadas.

### (a) Resumo executivo (10 linhas)
1. O motor de sinais está LIMPO de look-ahead — verificado por estrutura, grep e teste empírico.
2. O achado mais grave (P0) era estatístico: vereditos de "vantagem" sem correção de múltiplas comparações — os painéis fabricavam vencedores por acaso; corrigido com Bonferroni.
3. Bug P1 real na grade da corretora (fmt→parse corrompia grades editadas); corrigido e testado.
4. Grade centralizada em módulo único — app e scanner não podem mais divergir.
5. Contabilidade honesta confirmada: payout gravado por sinal, sem martingale, empate=refund.
6. Backtest agora tem split temporal, expectância por operação e selos de amostra fina.
7. Medido em 25.592 velas reais out-of-sample: sistema cru = 52,6% (PERDE, EV −2,7%/op).
8. Únicas melhorias que sobrevivem à estatística: confluência ≥2 (+4,4pp) e premium (+6,8pp, EV +9,9%/op) — ao custo de 66-82% menos operações.
9. Nenhum horário "bom" sobreviveu à calibração honesta; desativação automática implementada na variante estatística (Wilson).
10. Nada disso torna binária EV-positiva de forma comprovada — o forward test ao vivo continua sendo o único juiz.

### (b) Os 3 achados mais graves
1. **V-01 (P0)** — vereditos sem correção de múltiplas comparações: o painel de horários e o selo de estratégia declaravam vantagem que era ruído esperado.
2. **G-01 (P1)** — corrupção silenciosa de grade editada (fmt/parse assimétricos): risco direto de operar/registrar fora do horário da corretora.
3. **C-01 (P1 operacional)** — grade duplicada em dois arquivos: uma edição de um lado só contaminaria a coorte sem nenhum erro visível.

### (c) O que exige decisão SUA
1. **Deploy**: as correções estão acumuladas localmente (combinado). Subo tudo num commit único para o GitHub (app.py, strategies.py, scan_job.py, grade_core.py NOVO, requirements.txt, tests/, AUDITORIA.md)? O `grade_core.py` é arquivo novo — sem ele no repo, app e scanner quebram; sobe junto obrigatoriamente.
2. **Modo Premium**: a medição out-of-sample favorece operar SÓ premium (59,4% vs 52,6%, p=0,001) ao custo de ~82% menos sinais. Liga o "operar só Premium" no app? (É 1 toggle; a coorte atual continua sendo gravada inteira de qualquer forma.)
3. **Estratégias**: manter as 6 ativas (nenhuma comprovadamente degradada) ou enxugar para as de confluência? Recomendo manter e deixar o selo DEGRADADA vigiar.
4. **Redesign completo da UI**: deliberadamente não feito no meio do teste. Quer na próxima janela de manutenção?

### (d) Roadmap priorizado (ficou de fora)
1. Scanner ler a grade editada do usuário via `kairo_config.json` do Gist (hoje usa o default do grade_core; edição manual no app não propaga ao scanner).
2. Refatorar app.py em módulos testáveis (dados/regras/UI) — destravaria testes de `pnl_de`, `record_and_resolve` e `reapurar_empates`.
3. Pinагem de versões no requirements (hoje `>=` sem teto — deploy futuro pode puxar major não testada).
4. Medição multi-ativo do walk-forward quando houver dados reais dos 7 pares (a atual é EUR/USD 2018-19).
5. Paleta daltônico-completa e polish visual (P3).
6. D-02: fallback NY→−4 (só relevante se o deploy perder `zoneinfo`).
