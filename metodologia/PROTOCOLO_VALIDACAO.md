# Protocolo de validação do PDEA (pré-registro)

Preencha os campos marcados com ▢ **antes** de olhar qualquer resultado e guarde este arquivo com data (por exemplo, em um commit).

## 1. Hipóteses
- **H1.** O escore PDEA-R discrimina as horas com raio melhor do que a climatologia (local × hora do dia), o CAPE sozinho e a heurística PDEA-H.
- **H2.** A correção litorânea melhora o PDEA-R nas capitais litorâneas (comparar “PDEA-R” e “PDEA-R com correção litorânea”).
- **H3.** Os limiares dos níveis, calibrados no treino, continuam úteis fora da amostra.
- **H4.** A habilidade cai de modo gradual com a antecedência (faixas 0–6, 6–12, 12–24 e 24–48 h).

## 2. Dados
- Modelo: ▢ (padrão: `ecmwf_ifs`) · Execuções UTC: ▢ (padrão: 0 e 12) · Antecedência máxima: ▢ h
- Período de calibração: ▢ · **Período de teste prospectivo (congelado, não usado na calibração): ▢**
- GLM: raio principal ▢ (30 ou 50 km) · evento ▢ (≥ 1 ou ≥ 10 flashes/h) · filtro de qualidade ▢ (sim/não)
- Capitais: as 27 · Horas do GLM incompletas descartadas: sim (< 160 arquivos/h)

## 3. Análise (fixada)
- Validação cruzada: deixar um mês de fora e, depois, uma cidade de fora (`--esquema mes` e `--esquema cidade`).
- Referências: climatologia (local × hora do dia), CAPE × chuva bruto, só LI, PDEA-H.
- Métrica principal: **ΔAUC do PDEA-R sobre a melhor referência**, com IC 95% por bootstrap de dias. Secundárias: AP, CSI, BSS, escala diária, confiabilidade.
- Estratos obrigatórios (relatados mesmo se desfavoráveis): região, estação do ano, faixa de CAPE, litoral/interior, faixa de antecedência.

## 4. Critérios de sucesso (decidir com o orientador)
- H1: ΔAUC com IC acima de zero em ▢ de 5 regiões e na análise geral.
- H2: ΔAUC do PDEA-R com correção sobre o PDEA-R, com IC acima de zero nas litorâneas; se o IC do fator estimado incluir 1,0, a correção não é mantida.
- H3: queda de CSI ou BSS entre calibração e teste menor que ▢.
- H4: relatar a curva de habilidade por antecedência (sem critério de aprovação).

## 5. Congelamento
Depois da calibração, congelar âncoras, limiares dos níveis e fator litorâneo (`risco_raio.py`, `config_regioes.json`) e rodar **uma vez** no período de teste. Qualquer mudança posterior vira uma nova versão do método, com novo teste.
