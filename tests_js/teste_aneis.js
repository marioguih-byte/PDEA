// Anéis de alcance: painel acima das bolinhas (620 > 600), sem capturar o mouse, e visíveis só a partir do zoom 6.
const fs = require("fs"), { JSDOM } = require("jsdom");
(async () => {
  const dom = new JSDOM(fs.readFileSync("mapa_aneis.html", "utf8"), {url: "http://localhost:1/", runScripts: "dangerously", pretendToBeVisual: true});
  const w = dom.window, dorme = (ms) => new Promise(r => setTimeout(r, ms));
  await dorme(300);
  const mapa = Object.keys(w).filter(k => k.startsWith("map_")).map(k => w[k])[0];
  const ok = (c, m) => { if (!c) { console.error("FALHOU:", m); process.exit(1); } };
  const aneis = () => Object.values(mapa._layers).filter(l => l instanceof w.L.Circle);
  const painel = mapa.getPane("aneis");
  ok(painel && painel.style.zIndex === "620" && painel.style.pointerEvents === "none", "painel dos anéis");
  ok(parseInt(painel.style.zIndex, 10) > parseInt(mapa.getPane("markerPane").style.zIndex || "600", 10), "os anéis devem ficar acima dos marcadores");
  // zoom 4 (abertura do app): sem anéis; a partir do zoom 6: 2 capitais x 3 anéis
  ok(aneis().length === 0, "no zoom 4 não deve haver anéis, veio " + aneis().length);
  mapa.setView([-14, -52], 5, {animate: false}); await dorme(50);  // (no jsdom o zoom é inteiro: sem transformações 3D, o Leaflet ignora o zoomSnap 0,25)
  ok(aneis().length === 0, "no zoom 5 ainda não deve haver anéis: " + aneis().length);
  mapa.setView([-14, -52], 6, {animate: false}); await dorme(50);
  ok(aneis().length === 6, "no zoom 6 deveria haver 6 anéis, veio " + aneis().length);
  const porRaio = {}; aneis().forEach(l => { porRaio[l.getRadius()] = l.options; });
  ok(porRaio[30000].color === "#2ecc40" && porRaio[50000].color === "#ffd400" && porRaio[100000].color === "#e11d1d", "cores: " + JSON.stringify(Object.fromEntries(Object.entries(porRaio).map(([k, v]) => [k, v.color]))));
  ok(aneis().every(l => l.options.pane === "aneis" && l.options.interactive === false && l.options.weight === 4 && l.options.dashArray === "12 8"), "opções dos anéis");
  mapa.setView([-14, -52], 9, {animate: false}); await dorme(50);
  ok(aneis().length === 6, "no zoom 9 os anéis continuam: " + aneis().length);
  mapa.setView([-14, -52], 4, {animate: false}); await dorme(50);
  ok(aneis().length === 0, "ao afastar, os anéis somem: " + aneis().length);
  console.log("Anéis de alcance (painel acima das bolinhas, zoom mínimo 6, cores verde/amarelo/vermelho, tracejados): OK");
  process.exit(0);
})();
