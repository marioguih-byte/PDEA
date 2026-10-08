# Metodologia de verificação do PDEA contra o GLM

Este diretório não altera o painel. Ele reúne o protocolo e os scripts para **medir** se o escore do painel (PDEA-R =
CAPE × chuva) tem acurácia e correlação úteis com o GLM, de forma reprodutível, **com previsões reais com antecedência**.

## 0. Fluxo recomendado (previsões reais, 27 capitais)

```bash
pip install -r metodologia/requirements.txt
# 1) confira o acesso (cada comando abaixo tem --testar)
python metodologia/baixar_previsoes_runs.py --testar
python metodologia/baixar_glm.py --testar
# 2) previsões arquivadas por execução (Single Runs API): 00 e 12 UTC, antecedência de 0 a 72 h
python metodologia/baixar_previsoes_runs.py --inicio 2024-03-14 --fim 2025-03-13 --saida runs/ --modelo ecmwf_ifs
# 3) flashes do GLM por capital e hora, nos raios de 30, 50 e 100 km
python metodologia/baixar_glm.py --inicio 2024-03-14 --fim 2025-03-13 --saida glm/
# 4) tabela capital x hora x execução
python metodologia/montar_tabela.py --runs runs/ --glm glm/ --raio 30 --saida tabela_30km.csv
# 5) verificação por faixa de antecedência (uma execução por capital e hora em cada faixa)
python metodologia/verificar_glm.py --csv tabela_30km.csv --saida res_12-24h --antecedencia 12-24 --esquema mes
python metodologia/estimar_correcao_costeira.py --csv tabela_30km.csv   # use uma faixa de antecedência por vez
```

**Por que a Single Runs API e não a Previous Runs API:** a página da Previous Runs API (consultada em 8/10/2026) lista só
variáveis de superfície, sem CAPE, LI nem CIN. A Single Runs API lista CAPE, LI, CIN, precipitação, rajada, nível de 0 °C e
temperaturas em 850 e 500 hPa, e entrega qualquer execução pelo horário de inicialização (`run=`). O arquivo vai de
**14/03/2024** (ECMWF IFS HRES 9 km, `ecmwf_ifs`) e de **02/04/2026** (a maioria dos demais modelos). Cada execução só fica
disponível 4 a 6 h após a inicialização.

**Satélite do GLM:** o GOES-16 era o GOES-East até 06/04/2025 e o GOES-19 passou a ser em 07/04/2025; o `baixar_glm.py`
escolhe o bucket pela data. Um período que cruza essa data mistura dois instrumentos: considere analisar os dois trechos
separadamente.

**Cuidados de qualidade:**
- Horas do GLM com poucos arquivos (< 160 de ~180) são descartadas em `montar_tabela.py`: ausência de arquivo não é ausência de raio.
- O `baixar_glm.py` conta todos os flashes por padrão; `--filtro-qualidade` usa só `flash_quality_flag == 0`. Escolha uma opção e
  mantenha-a em todo o estudo.
- As horas locais usam um deslocamento fixo por UF (AC −5; AM, RO, RR, MT, MS −4; demais −3).
- O plano gratuito da Open-Meteo é não comercial e tem limites de chamadas; o downloader pausa entre execuções e retoma de onde parou.

**Status dos testes:** `python metodologia/testar_kit.py` testa offline os metadados, a contagem do GLM (com arquivos NetCDF
sintéticos), o download simulado, a junção e a CLI. Os scripts de download \textbf{não foram executados contra a rede real}
(o ambiente de desenvolvimento não tinha acesso): rode os dois `--testar` antes de começar.


Este diretório não altera o painel. Ele reúne o protocolo para **medir** se um preditor de raios
(o escore do painel, PDEA-R = CAPE × chuva, ou alternativas) tem acurácia e correlação úteis com o GLM, de forma reprodutível.

## 1. Tabela de entrada (CSV, uma linha por local × hora)

| coluna | conteúdo |
|---|---|
| `local` | nome da cidade/unidade |
| `hora` | horário local (ex.: 2026-09-01 14:00) |
| `cape`, `li`, `cin` | Open-Meteo (J/kg, °C, J/kg) |
| `precip`, `rajada`, `nivel0`, `t850`, `t500` | Open-Meteo (mm/h, km/h, m, °C, °C) |
| `flashes` | flashes do GLM naquela hora, dentro do raio escolhido (30, 50 ou 100 km) |
| `costeira` | (opcional) 1 = capital litorânea, 0 = interior; ativa a referência com correção litorânea e o estimador do fator |

Gere uma tabela por raio (ou um CSV por raio) e rode o script uma vez para cada.

## 2. Execução

```bash
pip install -r metodologia/requirements.txt
python metodologia/verificar_glm.py --csv tabela_30km.csv --saida resultados_30km --esquema mes
python metodologia/verificar_glm.py --csv tabela_30km.csv --saida resultados_30km_10fl --limiar-flashes 10
python metodologia/verificar_glm.py --demo        # dados SINTÉTICOS, só para testar o código
```

Esquemas de validação (`--esquema`):
- `mes`: deixa um mês de fora por vez (precisa de 2 ou mais meses). **É o recomendado.**
- `cidade`: deixa uma cidade de fora por vez (mede generalização espacial).
- `dia`: blocos de dias. Com um único mês é o único possível, mas tende a ser otimista.

## 2b. Correção litorânea

```bash
python metodologia/estimar_correcao_costeira.py --csv tabela_30km.csv --saida fator_costeiro.json
```

Ajusta `logit P(raio) = b0 + bx·log10(1 + CAPE×chuva) + bc·costeira` e converte o efeito do litoral em fator sobre o produto
(`10^(bc/bx)`), com IC 95% por bootstrap de dias. O painel usa hoje 0,4 (a priori, da literatura). Se o IC incluir 1,0,
os dados não sustentam a correção; se o IC excluir 0,4, atualize `correcao_costeira.fator` em `config_regioes.json`.
Cuidado: o efeito do litoral se confunde com o da cidade, então use várias capitais litorâneas e várias do interior.

## 3. O que o script faz

- Compara a **PDEA-R** (escore do painel), a heurística legada **PDEA-H**, o proxy **CAPE × chuva** bruto (Romps et al., 2014), o **LI sozinho** (Kunz, 2007) e a
  **PDEA-P** (regressão logística e gradient boosting em conjuntos de atributos: `nucleo`, `nucleo_cin`,
  `completo`, `defasagem`, `diurno`).
- Todos os preditores passam pelo mesmo procedimento: calibração de Platt e limiar que maximiza o CSI,
  escolhidos em escores fora da amostra dentro do treino.
- Métricas com IC 95% por bootstrap de dias inteiros: AUC, AP, POD, FAR, CSI, viés, HSS, PSS e BSS.
- Ganho (ΔAUC, ΔAP, ΔCSI) de cada preditor sobre a PDEA-R, também com IC.
- Escala diária (pico do escore × dia com raio) e tabela de confiabilidade do melhor modelo.
- Exporta `modelo_pdea_p.json` (regressão logística legível) e `prever_probabilidade()` em `pdea_prob.py`
  para uso futuro no painel sem depender do scikit-learn.

Saídas: `metricas.csv`, `ganho_sobre_pdea_r.csv`, `escala_diaria.csv`, `confiabilidade.csv`, `modelo_pdea_p.json`
(CSV com `;` e vírgula decimal, para abrir no Excel em português).

## 4. Cuidados para o artigo

1. Um único mês não caracteriza a habilidade (no artigo atual o AUC variou de 0,67 a 0,74 entre 2025 e 2026). Use o ano todo e capitais de todas as regiões (as 27 capitais do painel).
2. A Historical Forecast API é próxima de uma análise. Para falar em previsão com antecedência, use previsões de
   execuções anteriores (Previous Runs API) e confirme se CAPE e LI estão disponíveis nela.
3. Reporte sempre a taxa de acerto de quem nunca prevê raio e o ganho sobre a referência, não só o acerto bruto.
4. Os coeficientes exportados valem para o conjunto em que foram ajustados. Verifique em período e local independentes.
5. O modo `--demo` serve apenas para testar o código; seus números não são resultados.
