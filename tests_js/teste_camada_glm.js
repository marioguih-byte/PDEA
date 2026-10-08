const http = require("http"), fs = require("fs"), { JSDOM } = require("jsdom");
let estadoArquivo = null;           // conteúdo servido em /app/static/glm_flashes.txt (null = 404)
let contagem = 0, contagemMeta = 0;
const srv = http.createServer((req, res) => {
  if (req.url.startsWith("/app/static/glm_meta.txt") && estadoArquivo !== null) {
    contagemMeta++; let g = null; try { g = JSON.parse(estadoArquivo).gerado; } catch (e) {}
    if (g === null) { res.writeHead(404); res.end("x"); } else { res.writeHead(200, {"Content-Type": "text/plain"}); res.end(JSON.stringify({gerado: g})); }
  } else if (req.url.startsWith("/app/static/glm_flashes.txt") && estadoArquivo !== null) {
    contagem++; res.writeHead(200, {"Content-Type": "text/plain"}); res.end(estadoArquivo);
  } else { res.writeHead(404); res.end("nao"); }
});
const dorme = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  await new Promise(r => srv.listen(0, r));
  const porta = srv.address().port;
  const agora = Math.floor(Date.now() / 1000);
  const dom = new JSDOM(fs.readFileSync("mapa.html", "utf8"), {url: `http://localhost:${porta}/`, runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(u, o); } });
  const w = dom.window;
  await dorme(300);
  const mapa = Object.keys(w).filter(k => k.startsWith("map_")).map(k => w[k])[0];
  const cont = () => Object.values(mapa._layers).filter(l => l instanceof w.L.CircleMarker).length;
  const cores = () => Object.values(mapa._layers).filter(l => l instanceof w.L.CircleMarker).map(l => l.options.fillColor).sort();
  const chip = () => w.document.querySelector(".pdea-glm-chip").textContent;
  const ok = (c, m) => { if (!c) { console.error("FALHOU:", m); process.exit(1); } };

  // 1) servidor sem arquivo (404): usa o instantâneo embutido; o raio com mais de 20 min fica de fora; sem erros
  ok(cont() === 4, "esperava 4 raios do instantâneo, veio " + cont());
  ok(JSON.stringify(cores()) === JSON.stringify(["#e11d1d", "#ff8a00", "#ffe000", "#2ecc40"].sort()), "vermelho, laranja, amarelo e verde por idade: " + cores());
  ok(/4 nos/.test(chip()) && /20 min/.test(chip()), "chip: " + chip());

  // 2) o servidor passa a servir um arquivo novo (como se a coleta de 5 min tivesse rodado): o mapa atualiza sozinho
  estadoArquivo = JSON.stringify({gerado: agora + 300, janela_min: 20, ultimo_arquivo: agora + 280, arquivos: 90,
    raios: Array.from({length: 120}, (_, i) => [-10 + i * 0.05, -45, 30 + (i % 19) * 60])});
  await dorme(1200);
  ok(cont() === 120, "após a atualização, esperava 120, veio " + cont());
  ok(/120 nos/.test(chip()), "chip após atualização: " + chip());
  // contagem por cor no cartão (vermelho, laranja, amarelo, verde) = contagem das idades do arquivo
  const idades = Array.from({length: 120}, (_, i) => 30 + (i % 19) * 60);
  const esperado = [0, 0, 0, 0]; idades.forEach(a => { esperado[a <= 300 ? 0 : (a <= 600 ? 1 : (a <= 900 ? 2 : 3))]++; });
  const lido = [...w.document.querySelectorAll(".pdea-glm-chip .g-cls")].map(e => parseInt(e.textContent, 10));
  ok(JSON.stringify(lido) === JSON.stringify(esperado) && lido.every(n => n > 0), "contagem por cor: " + lido + " esperado " + esperado);
  const n1 = contagem;

  // 3) arquivo igual: não redesenha e NÃO baixa o arquivo grande de novo (só consulta o meta)
  const m0 = contagemMeta;
  await dorme(1500);
  ok(cont() === 120, "não deveria mudar");
  ok(contagem === n1, "o arquivo grande foi baixado de novo sem necessidade: " + (contagem - n1));
  ok(contagemMeta > m0, "deveria consultar o meta");

  // 4) arquivo corrompido: mantém o último desenho bom
  estadoArquivo = "{ isto nao e json";
  await dorme(1000);
  ok(cont() === 120, "arquivo ruim não pode apagar a camada, veio " + cont());

  // 5) arquivo novo com 0 raios: camada esvazia e o chip mostra 0
  estadoArquivo = JSON.stringify({gerado: agora + 600, janela_min: 20, ultimo_arquivo: agora + 580, arquivos: 90, raios: []});
  await dorme(1000);
  ok(cont() === 0 && /\b0 nos/.test(chip()), "esvaziar: " + cont() + " / " + chip());
  console.log("JS da camada GLM (Leaflet real em jsdom): OK  | arquivo grande:", contagem, "| meta:", contagemMeta);
  srv.close(); process.exit(0);
})();
