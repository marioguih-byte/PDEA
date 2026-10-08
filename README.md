# PDEA — Preditor de Descargas Elétricas Atmosféricas

Painel em **Streamlit** que mostra, para as **27 capitais brasileiras**, o potencial de descargas atmosféricas nas próximas 48 horas, em mapa, tabela, gráficos e alertas. A previsão usa o produto **CAPE × taxa de precipitação** previstos por modelos numéricos (Open-Meteo), o indicador de descargas de Romps et al. (2014, *Science*; 2018, *GRL*), convertido em um escore de 0 a 100 e em cinco níveis (Nenhum, Baixo, Moderado, Alto, Severo). A camada de topos de nuvem do **GOES-East** (banda 13, NASA GIBS) pode ser ligada no mapa.

> **Aviso.** O escore é uma previsão de modelo, não uma detecção. Os limiares dos níveis são **provisórios** e estão em calibração com o GLM (satélite GOES). O painel não substitui alertas oficiais (Defesa Civil, INMET) nem sistemas de detecção de descargas. Em caso de trovoada, procure abrigo em local fechado.

## Método

- **Escore (PDEA-R):** `CAPE (J/kg) × precipitação (mm/h)` na hora, convertido em 0–100 por interpolação linear em log10 entre âncoras (`ANCORAS_CAPEXP` em `risco_raio.py`). Limites dos níveis: 15 / 35 / 55 / 75.
- **Base científica:** Romps et al. (2014) mostraram que CAPE × chuva explica a maior parte da variância das descargas nos EUA continentais; Romps et al. (2018) mostraram que reproduz mapas sazonais e o ciclo diurno nos EUA e a distribuição global sobre terra, mas **não** o forte contraste terra–oceano.
- **Correção litorânea (a priori):** o CAPE × P não reproduz o menor número de descargas sobre o oceano (Romps et al., 2018), que é cerca de 5 a 10 vezes menor que sobre terra (Williams e Stanfill, 2002). As 11 capitais litorâneas (Maceió, Salvador, Fortaleza, Vitória, São Luís, João Pessoa, Recife, Rio de Janeiro, Natal, Florianópolis e Aracaju) têm o produto multiplicado por **0,4**, o ponto médio geométrico entre 1 (terra) e o oceano puro (~0,14). O fator e a lista ficam em `correcao_costeira` de `config_regioes.json` (fator 1,0 ou lista vazia desliga). A classificação litorânea é manual e o fator é **provisório**: `metodologia/estimar_correcao_costeira.py` o estima com o GLM.
- **Limitações:** convecção com pouco CAPE (sistemas frontais) tende a passar despercebida; sem precipitação do modelo o escore não é calculado. A validação para o Brasil, com o GLM, está em `metodologia/`.
- **Método legado:** a heurística por faixas de CAPE, LI e CIN (`metodo="pontos"`) continua no código para comparação.

## Funcionalidades

| Área | O que faz |
|---|---|
| **Mapa** | OpenStreetMap (padrão) ou Satélite, travado no Brasil (contorno + véu no entorno), botão ⌂ para recentralizar. Bolinhas de tamanho fixo (inclusive durante o zoom) com o score escrito dentro (opcional); a de maior risco fica por cima. A posição e o zoom são preservados ao fechar o painel. |
| **Hora da previsão** | Controle deslizante de +0 h a +48 h: o mapa, os cartões e a tabela passam a mostrar a hora escolhida. |
| **Tendência** | Seta ▲ ▼ ▬ por capital (variação do score nas próximas 6 h), pico das próximas 24 h e horário do pico. A lista lateral pode ser ordenada por *Nome* ou *Maior risco*. |
| **Detalhe da capital** | Cartões (risco, CAPE, LI, CIN, tendência e variáveis extras), gráfico de 48 h do score sobre as faixas de risco, gráficos de CAPE/LI/CIN e das variáveis extras e tabela horária. |
| **Regiões e variáveis extras** | O score usa CAPE × chuva. Fatores de CAPE por UF podem ser definidos em `config_regioes.json` (todos 1,0 por padrão). Lifted Index, CIN, rajada, gradiente 850–500 hPa e nível de 0 °C aparecem como cartões e gráficos no detalhe e no histórico, mas **não** entram no score. |
| **Como o score foi calculado** | No detalhe de cada capital, uma tabela mostra o CAPE, a chuva prevista, o produto CAPE × chuva e o score final. |
| **Camadas do mapa** | Divisas estaduais (ligadas por padrão) e topos de nuvem do GOES-East, infravermelho banda 13, via NASA GIBS (opcional, com controle de opacidade). |
| **Histórico** | Grava em SQLite, uma vez por hora e por modelo, CAPE, LI, CIN e variáveis extras das próximas 48 h. O painel mostra o score realizado e os gráficos de CAPE, LI, CIN e variáveis extras no período, como a previsão para um horário mudou entre execuções, e exporta CSV com score recalculado. |
| **Exportação** | Tabela atual e séries horárias de 48 h em CSV (abre no Excel em português); mapa estático em PNG e relatório em PDF (mapa + ranking). |
| **Acessibilidade** | Score escrito dentro da bolinha e barra lateral recolhida em telas pequenas. |
| **Robustez** | 3 tentativas com espera crescente na API; se ela falhar, usa o último dado válido e avisa quantos minutos ele tem. Indicador "Dados · há N min" no cabeçalho. |
| **Alertas** | `alertas.py` envia e-mail e/ou webhook (Teams, Slack, Discord, gateway de WhatsApp) quando uma capital atinge, ou deve atingir em poucas horas, o nível configurado. |

## Estrutura

| Arquivo | Finalidade |
|---|---|
| `app.py` | Interface Streamlit. |
| `analise.py` | Séries de score, tendência, extras e fatores por UF (sem dependência do Streamlit). |
| `historico.py` | Histórico das previsões em SQLite (módulo e linha de comando). |
| `app_camadas.py` | Endereço e atribuição da camada GOES. |
| `config_regioes.json` | Fatores de CAPE e LI por UF (neutros por padrão). |
| `risco_raio.py` | Heurística de risco, parâmetros de calibração e cores dos níveis. |
| `modelos.py` | Consulta dos modelos (com tentativas) e normalização da resposta. |
| `unidades.py` | As 27 capitais, coordenadas e UF. |
| `relatorio.py` | Geração do mapa PNG e do relatório PDF. |
| `alertas.py` | Verificação e envio de alertas (linha de comando). |
| `dados/` | Contorno, máscara e divisas estaduais simplificados (Natural Earth, domínio público). |
| `.streamlit/config.toml` | Tema escuro fixo. |
| `.github/workflows/alertas.yml` | Agendamento dos alertas a cada 30 min. |
| `validar.py` | Testes (offline por padrão; `--online` consulta a API). |
| `metodologia/` | Protocolo e scripts para verificar o escore (e alternativas probabilísticas) contra o GLM. Ver `metodologia/README.md`. |

## Como executar

```bash
pip install -r requirements.txt
streamlit run app.py
```

A aplicação abre normalmente em `http://localhost:8501`. Para validar:

```bash
python validar.py                    # testes offline
python validar.py --online           # relatório de sanidade com dados reais (cobertura, faixas, horizonte, extras, GOES)
python validar.py --online --modelos # idem para os 12 modelos: mostra quais devolvem CAPE, LI e CIN
```

## Sobre o score

- Escore = função monótona de `CAPE × chuva` (ver **Método**). Sem chuva prevista ou sem CAPE, o escore é zero; sem dado de precipitação do modelo, a capital aparece como "Sem dados".
- O escore mede **potencial**, e não detecta descargas. Os níveis são provisórios até a calibração com o GLM (`metodologia/`).
- O Open-Meteo combina modelos globais; use o seletor de modelo para comparar e avaliar a incerteza.

## Histórico

O painel grava automaticamente uma execução por modelo e por hora cheia quando os dados são buscados com sucesso. O arquivo fica em `historico/pdea.sqlite` (mude com a variável `PDEA_HISTORICO`; use `desligado` para não gravar).

```bash
python historico.py registrar                      # busca o modelo e grava (agende a cada hora)
python historico.py status
python historico.py exportar --saida historico.csv --dias 30
python historico.py limpar --manter-dias 90
```

> No Streamlit Community Cloud o disco é apagado quando o app reinicia, então o histórico lá não é permanente. Para um histórico de verdade, agende `python historico.py registrar` em uma máquina sua (cron/Agendador de Tarefas) ou use um volume persistente.

O histórico guarda as variáveis **brutas** (não o score), então dá para recalcular o score com outra calibração e compará-lo depois com observações.

## Alertas

```bash
python alertas.py --dry-run                                  # só imprime
python alertas.py --nivel Alto --antecedencia 3              # avisa em Alto+ ou previsto em até 3 h
python alertas.py --heuristica-ampliada                      # inclui precipitação, rajada etc. no score
```

Cada capital é avisada **uma vez**; ela só é avisada de novo depois de voltar a ficar abaixo do nível. O estado fica em `estado_alertas.json`. Os canais são configurados por variáveis de ambiente (nenhuma é obrigatória, mas sem elas o script apenas imprime):

| Variável | Uso |
|---|---|
| `ALERTA_WEBHOOK_URL` | Webhook (recebe JSON com `text` e `content`). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Servidor de e-mail. |
| `ALERTA_EMAIL_DE`, `ALERTA_EMAIL_PARA` | Remetente e destinatários (separados por vírgula). |

Para rodar a cada 30 minutos sem servidor próprio, use o workflow `.github/workflows/alertas.yml` (cadastre as variáveis como *secrets* do repositório). O GitHub pode atrasar execuções agendadas em alguns minutos.

## Publicação no Streamlit Community Cloud

Envie esta pasta para um repositório GitHub. Na criação do aplicativo no [Streamlit Community Cloud](https://share.streamlit.io/), indique o repositório, a branch e o arquivo principal `app.py`. As dependências são instaladas a partir de `requirements.txt`.

## Fonte de dados

Previsões da API [Open-Meteo](https://open-meteo.com/), com as variáveis horárias `cape`, `lifted_index` e `convective_inhibition` (3 dias a partir de 00:00 de hoje) e, para a heurística ampliada, `precipitation`, `wind_gusts_10m`, `freezing_level_height`, `temperature_850hPa` e `temperature_500hPa`. Imagem de satélite: GOES-East via [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/). Confira os termos de uso da Open-Meteo para o seu caso (o plano gratuito é voltado a uso não comercial).
