# Teste da camada de raios (JavaScript)

Roda o JavaScript real da camada GLM, com o Leaflet de verdade, em um DOM simulado (jsdom) e contra um servidor falso do
arquivo `glm_flashes.txt`. Verifica: primeiro desenho com o instantâneo embutido, cores por idade, atualização automática
quando o arquivo muda, que o arquivo grande não é baixado de novo sem necessidade, que um arquivo corrompido não apaga a
camada e que um arquivo sem raios esvazia o mapa.

```bash
cd tests_js
npm install leaflet jsdom
python gerar_mapa_teste.py
python gerar_mapa_aneis.py
node teste_camada_glm.js
node teste_envelhecimento.js   # cores por idade e desaparecimento após 20 min (relógio adiantado)
node teste_aneis.js            # anéis de alcance: painel acima das bolinhas, zoom mínimo 6, cores e tracejado
```
