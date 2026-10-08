const http = require("http"), fs = require("fs"), { JSDOM } = require("jsdom");
let estadoArquivo = null;           // conteúdo servido em /app/static/glm_flashes.txt (null = 404)
let contagem = 0, contagemMeta = 0, urlsVistas = [];
const srv = http.createServer((req, res) => {
  urlsVistas.push(req.url);
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

  // 2b) o cartão mostra a hora da coleta e todas as consultas levam parâmetro anti-cache
  ok(/coleta \d\d:\d\d/.test(chip()), "cartão sem a hora da coleta: " + chip());
  ok(urlsVistas.filter(u => u.startsWith("/app/static/glm_")).every(u => /[?&]_=\d+/.test(u)), "consultas sem anti-cache: " + urlsVistas.slice(0, 4));

  // 2c) dado MAIS VELHO (arquivo antigo/em cache) nunca sobrescreve um dado recente
  const antes120 = cont();
  estadoArquivo = JSON.stringify({gerado: agora + 100, janela_min: 30, ultimo_arquivo: agora, arquivos: 10, raios: [[-5, -40, 10], [-6, -41, 20]]});
  await dorme(1200);
  ok(cont() === antes120, "arquivo velho sobrescreveu o dado novo: " + cont());
  estadoArquivo = JSON.stringify({gerado: agora + 300, janela_min: 20, ultimo_arquivo: agora + 280, arquivos: 90,
    raios: Array.from({length: 120}, (_, i) => [-10 + i * 0.05, -45, 30 + (i % 19) * 60])});  // volta ao arquivo bom (mesmo gerado)

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
  // 2d) canal da página (window.top.__pdeaGlm): dado novo entregue pelo Streamlit, sem nenhum arquivo estático
  estadoArquivo = null;   // o servidor passa a responder 404 (como se o arquivo estático não funcionasse)
  w.__pdeaGlm = {versao: "teste", gerado: agora + 900, janela_min: 20, ultimo_arquivo: agora + 880, arquivos: 60, agora: agora + 960,
                 raios: [[-1, -50, 10], [-2, -51, 400], [-3, -52, 700], [-4, -53, 1000], [-5, -54, 1130]]};
  await dorme(1200);
  ok(cont() === 5, "o canal deveria entregar 5 raios, veio " + cont());
  ok(/5 nos/.test(chip()) && /coleta/.test(chip()), "cartão após o canal: " + chip());
  ok(w.document.querySelector(".pdea-glm-chip").title.includes("teste"), "tooltip com a versão do coletor");
  // a hora do servidor no envio corrige as idades: gerado = +900, agora = +960 => cada raio chega 60 s mais velho
  const coresCanal = Object.values(mapa._layers).filter(l => l instanceof w.L.CircleMarker).map(l => l.options.fillColor).sort();
  ok(JSON.stringify(coresCanal) === JSON.stringify(["#e11d1d", "#ff8a00", "#ffe000", "#2ecc40", "#2ecc40"].sort()), "cores com a correção de 60 s: " + coresCanal);
  w.__pdeaGlm = {...w.__pdeaGlm, gerado: agora + 100};   // canal com dado velho: ignorado
  await dorme(800);
  ok(cont() === 5, "canal velho não pode sobrescrever");

  console.log("JS da camada GLM (Leaflet real em jsdom): OK  | arquivo grande:", contagem, "| meta:", contagemMeta);
  srv.close(); process.exit(0);
})();
