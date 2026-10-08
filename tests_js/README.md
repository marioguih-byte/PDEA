# Teste da camada de raios (JavaScript)

Roda o JavaScript real da camada GLM, com o Leaflet de verdade, em um DOM simulado (jsdom) e contra um servidor falso do
arquivo `glm_flashes.txt`. Verifica: primeiro desenho com o instantâneo embutido, cores por idade, atualização automática
quando o arquivo muda, que o arquivo grande não é baixado de novo sem necessidade, que um arquivo corrompido não apaga a
camada e que um arquivo sem raios esvazia o mapa.

```bash
cd tests_js
npm install leaflet jsdom
python gerar_mapa_teste.py
python gerar_canal_teste.py
python gerar_legenda_teste.py
node teste_camada_glm.js       # atualização por arquivo e pelo canal da página, só dado mais novo, anti-cache, contagem por cor
node teste_envelhecimento.js   # cores por idade e desaparecimento após 20 min (relógio adiantado)
node teste_relogio.js          # idade dos raios sem horário de envio pelo relógio do navegador
node teste_legenda.js          # legenda retrátil: abre/fecha, lembra a escolha, recolhida em tela estreita
node teste_iframes.js          # mapa e canal em iframes irmãos, ligados pela janela mais alta (sem arquivo estático)
```
