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
| **Cabeçalho** | Logo do PDEA em um cartão branco (54 px de altura) e, à direita, três cartões com rótulo e valor: *Modelo*, *Hora exibida* e *Dados* (com um ponto verde pulsante quando a previsão tem até 15 min; laranja e parado se for mais velha). Uma faixa de cor fina no topo reúne a paleta do painel. |
| **Mapa** | OpenStreetMap (padrão) ou Satélite, travado no Brasil (contorno + véu no entorno), botão ⌂ para recentralizar. Bolinhas de tamanho fixo (inclusive durante o zoom) com o score escrito dentro (opcional); a de maior risco fica por cima. A posição e o zoom são preservados ao fechar o painel. |
| **Hora da previsão** | Controle deslizante de +0 h a +48 h: o mapa, os cartões e a tabela passam a mostrar a hora escolhida. |
| **Tendência** | Seta ▲ ▼ ▬ por capital (variação do score nas próximas 6 h), pico das próximas 24 h e horário do pico. A lista lateral pode ser ordenada por *Nome* ou *Maior risco*. |
| **Detalhe da capital** | Cartões (risco, CAPE, LI, CIN, tendência e variáveis extras), gráfico de 48 h do score sobre as faixas de risco, gráficos de CAPE/LI/CIN e das variáveis extras e tabela horária. |
| **Regiões e variáveis extras** | O score usa CAPE × chuva. Fatores de CAPE por UF podem ser definidos em `config_regioes.json` (todos 1,0 por padrão). Lifted Index, CIN, rajada, gradiente 850–500 hPa e nível de 0 °C aparecem como cartões e gráficos no detalhe e no histórico, mas **não** entram no score. |
| **Como o score foi calculado** | No detalhe de cada capital, uma tabela mostra o CAPE, a chuva prevista, o produto CAPE × chuva e o score final. |
| **Camadas do mapa** | Divisas estaduais (ligadas por padrão), topos de nuvem do GOES-East (infravermelho, banda 13, via NASA GIBS, com controle de opacidade) e **raios observados pelo GLM** (ver abaixo). |
| **Alcance ao redor das capitais** | Anéis tracejados e grossos de **30 km (verde)**, **50 km (amarelo)** e **100 km (vermelho)** em volta de cada capital, os mesmos raios usados na verificação contra o GLM, em **qualquer zoom** (com o mapa afastado, os anéis menores ficam por baixo da bolinha da capital; aproxime para vê-los). Botão na barra lateral (*Mapa*); só contorno, sem capturar o mouse. |
| **Legenda retrátil** | A legenda do mapa (níveis de risco, nuvens, alcance e raios por idade) recolhe e expande ao clicar no título "Risco de raios" (seta à direita). O mapa lembra a escolha quando é redesenhado; em telas estreitas (menos de 700 px) ela começa recolhida, para não cobrir o mapa. |
| **Raios em tempo real (GLM)** | Flashes do GLM do GOES-East nos últimos 20 min, coloridos pela idade: **vermelho** (até 5 min), **laranja** (5 a 10), **amarelo** (10 a 15) e **verde** (15 a 20); depois de 20 min somem. Uma thread no servidor coleta os arquivos mais recentes do repositório público da NOAA a cada 5 min (minutos 0, 5, 10…) e grava `static/glm_flashes.txt`; o mapa busca o arquivo no navegador e redesenha **só a camada de raios**, sem recarregar a página nem acionar o Streamlit. Um cartão no mapa mostra o número de raios e a hora do dado mais recente. |
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
| `glm_ao_vivo.py` | Coleta dos raios do GLM em segundo plano (a cada 5 min) e gravação de `static/glm_flashes.txt`. |
| `assets/` | Logo do PDEA (`pdea_logo.png`) e ícone da aba (`pdea_icone.png`). |
| `static/` | Pasta servida pelo Streamlit em `/app/static/` (`enableStaticServing`); recebe os arquivos dos raios. |
| `tests_js/` | Teste em Node do JavaScript da camada de raios (Leaflet real em jsdom). |
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

## Manter o app acordado (Streamlit Community Cloud)

O Community Cloud hiberna apps sem tráfego por **12 horas**; segundo a documentação, para mantê-lo acordado basta **visitar** o app (commits não acordam). Não há como impedir isso dentro do código. O workflow `.github/workflows/manter_app_acordado.yml` visita o app a cada 6 horas com um navegador (Playwright), clica em "Yes, get this app back up!" se ele estiver dormindo e espera o app carregar.

1. No GitHub: *Settings > Secrets and variables > Actions > aba Variables > New repository variable*: nome `APP_URL`, valor `https://SEU-APP.streamlit.app`.
2. Rode uma vez em *Actions > Manter o app acordado > Run workflow* para testar; o log deve terminar em "Visita concluída".

Limites: é um **contorno** de uma regra do plano gratuito, não um recurso garantido, e o Streamlit pode mudar a regra; o agendamento do GitHub pode atrasar alguns minutos; em repositório público o GitHub desativa agendamentos após 60 dias sem atividade no repositório; em repositório privado cada visita consome minutos da cota do Actions (cerca de 2 min por execução, 4 execuções por dia). Para operação sempre ligada, a saída segura é hospedar o app em um servidor próprio ou em outro provedor sem hibernação.

## Raios em tempo real (GLM): detalhes

- **Fonte:** arquivos GLM-L2-LCFA do bucket público `noaa-goes19` (GOES-East desde 07/04/2025), um arquivo a cada 20 s; a variável de ambiente `PDEA_GLM_BUCKET` troca o bucket.
- **Raios de antes da abertura:** na primeira coleta (servidor recém-ligado ou acordado) o coletor busca no S3 todos os arquivos dos 20 min anteriores, e o mapa espera até 12 s por essa coleta. Quem abre o painel já vê os raios recentes, e não só os que ocorrem depois de abrir. Se ninguém abrir o app, nada é coletado, mas isso não perde dados dentro da janela: o GLM guarda os arquivos no S3 e a janela é reconstruída na abertura.
- **Todas as cores na abertura:** quando há muitos raios, o limite de quantidade é dividido por **faixa de idade** (5 em 5 min), e não corta só os antigos; flashes repetidos na mesma célula e no mesmo minuto são juntados. Assim, o amarelo e o verde (mais antigos) continuam aparecendo em tempestades fortes. O cartão do mapa mostra a contagem por cor (vermelho, laranja, amarelo e verde).
- **O que se vê:** o GLM mede a atividade elétrica **total** (intranuvem e nuvem-solo), com eficiência de detecção que varia com a posição e o horário; os arquivos chegam ao S3 com atraso de alguns minutos. É observação por satélite, não previsão nem alerta.
- **Duas vias de atualização, vale a mais nova:** (1) o **canal da página**: um fragmento do Streamlit (`canal_glm`, a cada 30 s) entrega ao navegador, em `window.top.__pdeaGlm`, cada coleta nova do servidor (de 5 em 5 min); não depende do arquivo estático nem de gravação em disco; (2) o **arquivo estático** `glm_flashes.txt`, consultado pelo `glm_meta.txt` (poucos bytes) a cada 30 s, com parâmetro anti-cache. O mapa só aceita dado **mais novo** que o que já mostra: um arquivo velho ou em cache nunca sobrescreve um dado recente. Só a camada de raios é redesenhada, sem recarregar a página nem o mapa. Nos primeiros minutos de um servidor recém-ligado o navegador tenta a cada 5 s até a primeira coleta terminar.
- **Arquivos velhos:** ao ligar, o coletor apaga `static/glm_flashes.txt` e `static/glm_meta.txt` que sobraram de execuções antigas (não envie esses arquivos ao GitHub) e só aceita como "atual" um arquivo de menos de 15 min. Arquivos do GLM que falham são baixados de novo nas rodadas seguintes (até 3 vezes), e, se a janela ficar incompleta logo depois de ligar, há rodadas de reforço a cada 30 s.
- **Diagnóstico no mapa:** o cartão mostra o número de raios em cada cor, "dados até HH:MM (há N min)" e "coleta HH:MM" (a hora em que o servidor coletou; deve avançar de 5 em 5 min). Passe o mouse no cartão: aparece a versão do coletor, a janela e o número de arquivos lidos. Se aparecer "falha na coleta", o servidor não conseguiu alcançar o repositório da NOAA.
- **Requisitos:** `netCDF4` (em `requirements.txt`) e `server.enableStaticServing = true` (em `.streamlit/config.toml`). Sem eles o painel funciona, só sem a camada.
- **Streamlit Community Cloud:** a coleta roda enquanto o app está ativo; quando o app hiberna por inatividade, a thread para, e a camada mostra "aguardando dados" até alguém acordar o app e a primeira coleta terminar (cerca de um minuto). Veja a seção sobre manter o app acordado.
- **Barra de rolagem:** a barra lateral e a da página são mais grossas (16 px no Chrome/Edge; no Firefox usa a espessura padrão, `scrollbar-width: auto`, em vez da fina).
- **Leitura em processo separado (importante):** a biblioteca NetCDF/HDF5 é nativa e **não é segura para várias threads**: leituras simultâneas derrubam o processo inteiro com `Segmentation fault`, e o Streamlit Cloud mostra "Oh no. Error running app" (isso aconteceu em 08/10/2026 depois de várias atualizações de código seguidas, quando o app antigo deixava coletores velhos rodando junto com o novo). Por isso o coletor lê os arquivos em um **processo filho** (`extrair_isolado`): se a biblioteca falhar, só o filho cai; o lote é dividido ao meio até achar o arquivo problemático, que vira uma falha isolada. Além disso, ao recarregar o módulo, a versão nova **para** o coletor da versão anterior (`parar()`), o lock do NetCDF é um só por processo e só existe um coletor por processo.
- **`st.iframe`:** o canal da página usa `st.iframe` (o `st.components.v1.html` está obsoleto e anunciado para remoção) e só cai para o antigo em versões do Streamlit que ainda não têm o novo.

## Como confirmar a versão no ar

A barra lateral mostra os créditos (Elaborado por: Mário Henrique e Mayara Christine). O número da versão fica só no texto de ajuda: passe o mouse sobre os créditos e aparece "Versão 2026-10-08g". Se não for esse, o que está rodando é uma cópia antiga: envie **todos** os arquivos ao GitHub (inclusive `glm_ao_vivo.py`, `assets/`, `static/`, `requirements.txt` e `.streamlit/config.toml`), com o `app.py` na **raiz** do repositório, e use *Manage app > Reboot app* no Streamlit.

## Raios em tempo real (GLM): detalhes

- **Fonte:** arquivos GLM-L2-LCFA do bucket público `noaa-goes19` (GOES-East desde 07/04/2025), um arquivo a cada 20 s; a variável de ambiente `PDEA_GLM_BUCKET` troca o bucket.
- **Raios de antes da abertura:** na primeira coleta (servidor recém-ligado ou acordado) o coletor busca no S3 todos os arquivos dos 20 min anteriores, e o mapa espera até 12 s por essa coleta. Quem abre o painel já vê os raios recentes, e não só os que ocorrem depois de abrir. Se ninguém abrir o app, nada é coletado, mas isso não perde dados dentro da janela: o GLM guarda os arquivos no S3 e a janela é reconstruída na abertura.
- **Todas as cores na abertura:** quando há muitos raios, o limite de quantidade é dividido por **faixa de idade** (5 em 5 min), e não corta só os antigos; flashes repetidos na mesma célula e no mesmo minuto são juntados. Assim, o amarelo e o verde (mais antigos) continuam aparecendo em tempestades fortes. O cartão do mapa mostra a contagem por cor (vermelho, laranja, amarelo e verde).
- **O que se vê:** o GLM mede a atividade elétrica **total** (intranuvem e nuvem-solo), com eficiência de detecção que varia com a posição e o horário; os arquivos chegam ao S3 com atraso de alguns minutos. É observação por satélite, não previsão nem alerta.
- **Sem recarregar:** nos primeiros minutos de um servidor recém-ligado o navegador tenta a cada 5 s até a primeira coleta terminar; depois consulta `glm_meta.txt` (poucos bytes) a cada 30 s e só baixa `glm_flashes.txt` quando há dado novo. Se o arquivo não puder ser lido, o mapa mantém o último desenho e o cartão mostra o horário do dado.
- **Requisitos:** `netCDF4` (em `requirements.txt`) e `server.enableStaticServing = true` (em `.streamlit/config.toml`). Sem eles o painel funciona, só sem a camada.
- **Streamlit Community Cloud:** a coleta roda enquanto o app está ativo; quando o app hiberna por inatividade, a thread para, e a camada mostra "aguardando dados" até alguém acordar o app e a primeira coleta terminar (cerca de um minuto). Veja a seção sobre manter o app acordado.
- **Cuidado técnico:** a leitura NetCDF/HDF5 é feita um arquivo por vez, porque a biblioteca não é segura para várias threads.

## Como confirmar a versão no ar

A barra lateral mostra "Versão 2026-10-08b · raios GLM 20 min ... (sem cadastro de e-mail)". Se esse texto não aparece, o que está rodando é uma cópia antiga do app: envie **todos** os arquivos ao GitHub (inclusive `glm_ao_vivo.py`, `assets/`, `static/`, `requirements.txt` e `.streamlit/config.toml`), com o `app.py` na **raiz** do repositório e use *Manage app > Reboot app* no Streamlit.

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
