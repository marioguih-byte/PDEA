# Teste de navegador (clique na bolinha, cores dos raios, painel de detalhes)

Abre o painel em um Chromium de verdade (sem tela), com previsão e raios **simulados** (sem rede), e confere:
- as quatro cores de raios (vermelho, laranja, amarelo e verde) têm contagem maior que zero;
- clicar na bolinha de uma capital **abre o painel de detalhes**;
- o painel **não fecha sozinho** durante as atualizações do canal dos raios (65 s, duas rodadas);
- o botão *Fechar* fecha, e outra capital abre;
- trocar para o **método heurístico** muda o cartão *Método* do cabeçalho e mostra o aviso, o painel da capital passa a mostrar a tabela de pontos (CAPE, LI e CIN, com a soma), e voltar ao método padrão restaura tudo.

```bash
cd tests_e2e && npm install @sparticuz/chromium puppeteer-core leaflet@1.9.4 jquery && cd ..
streamlit run tests_e2e/launcher_teste.py --server.headless true --server.port 8765 &   # na RAIZ do projeto (lê o tema de .streamlit/config.toml)
cd tests_e2e && node teste_clique.mjs
```

Foi este teste que mostrou, em 08/10/2026, por que o clique deixou de abrir o painel: o HTML do mapa mudava a cada execução (havia um
horário embutido), o `streamlit-folium` recriava o mapa e perdia o clique.
