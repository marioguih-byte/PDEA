// Legenda retrátil: abre por padrão, recolhe e expande ao clicar no título, lembra a escolha (janela mais alta acessível)
// e, sem escolha anterior, começa recolhida em telas estreitas.
const fs = require("fs"), { JSDOM } = require("jsdom");
const html = fs.readFileSync("legenda.html", "utf8");
const dorme = (ms) => new Promise(r => setTimeout(r, ms));
const ok = (c, m) => { if (!c) { console.error("FALHOU:", m); process.exit(1); } };
function abrir({largura = 1200, guardado} = {}) {
  return new JSDOM(html, {url: "http://localhost:1/", runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(w) { Object.defineProperty(w, "innerWidth", {value: largura, configurable: true}); if (guardado !== undefined) w.__pdeaLegendaAberta = guardado; }
  }).window;
}
(async () => {
  let w = abrir(); await dorme(50);
  let el = w.document.querySelector("details.pdea-legenda");
  ok(el && el.open, "a legenda deve começar aberta em tela larga");
  ok(w.document.querySelector("details.pdea-legenda summary .rl-titulo").textContent.trim() === "Risco de raios", "título no summary");
  const corpo = w.document.querySelector(".pdea-legenda .rl-corpo").textContent;
  ok(["Severo", "Alcance ao redor da capital", "30 km", "Raios observados (GLM)", "15 a 20 min", "Nuvens"].every(t => corpo.includes(t)), "conteúdo da legenda: " + corpo.slice(0, 80));
  // recolher e expandir pelo título
  w.document.querySelector("details.pdea-legenda summary").click(); await dorme(60);
  ok(!el.open && w.__pdeaLegendaAberta === false, "clicar no título deve recolher e guardar a escolha");
  w.document.querySelector("details.pdea-legenda summary").click(); await dorme(60);
  ok(el.open && w.__pdeaLegendaAberta === true, "clicar de novo deve expandir");
  // a escolha anterior vale ao redesenhar o mapa
  w = abrir({guardado: false}); await dorme(50);
  ok(!w.document.querySelector("details.pdea-legenda").open, "escolha 'recolhida' deve ser restaurada");
  w = abrir({largura: 1200, guardado: true}); await dorme(50);
  ok(w.document.querySelector("details.pdea-legenda").open, "escolha 'aberta' deve ser restaurada");
  // tela estreita sem escolha anterior: começa recolhida; com escolha 'aberta': respeita
  w = abrir({largura: 480}); await dorme(50);
  ok(!w.document.querySelector("details.pdea-legenda").open, "em tela estreita deve começar recolhida");
  w = abrir({largura: 480, guardado: true}); await dorme(50);
  ok(w.document.querySelector("details.pdea-legenda").open, "em tela estreita, a escolha 'aberta' vale");
  console.log("Legenda retrátil (abre/fecha, lembra a escolha, recolhida em tela estreita): OK");
  process.exit(0);
})();
