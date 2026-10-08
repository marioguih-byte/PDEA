// Dado sem "agora" (instantâneo embutido no mapa ou arquivo estático): a idade já decorrida desde a coleta vem do relógio do navegador.
// Assim, um mapa recriado minutos depois da coleta não mostra raios velhos como se fossem novos.
const fs = require("fs"), { JSDOM } = require("jsdom");
const html = fs.readFileSync("mapa.html", "utf8");
const gerado = Number(/"gerado":(\d+)/.exec(html)[1]);   // hora da coleta embutida no mapa
const dorme = (ms) => new Promise(r => setTimeout(r, ms));
const ok = (c, m) => { if (!c) { console.error("FALHOU:", m); process.exit(1); } };
async function abrir(segundosDepois) {
  const dom = new JSDOM(html, {url: "http://localhost:1/", runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = () => Promise.reject(new Error("sem rede"));
      w.Date.now = () => (gerado + segundosDepois) * 1000;   // o navegador "está" X segundos depois da coleta
    } });
  await dorme(300);
  const w = dom.window;
  const mapa = Object.keys(w).filter(k => k.startsWith("map_")).map(k => w[k])[0];
  return Object.values(mapa._layers).filter(l => l instanceof w.L.CircleMarker).map(l => l.options.fillColor).sort();
}
(async () => {
  // o instantâneo embutido tem raios de 60, 400, 700, 1100 e 1300 s de idade no momento da coleta
  const V = "#e11d1d", L = "#ff8a00", A = "#ffe000", G = "#2ecc40";
  let c = await abrir(0);
  ok(JSON.stringify(c) === JSON.stringify([V, L, A, G].sort()), "logo após a coleta: " + c);          // 60, 400, 700, 1100 (o de 1300 já está fora)
  c = await abrir(600);
  ok(JSON.stringify(c) === JSON.stringify([A, G].sort()), "10 min depois: " + c);                      // 660 (amarelo) e 1000 (verde); 1300+ somem
  c = await abrir(1300);
  ok(c.length === 0, "tudo velho demais: " + c);
  c = await abrir(5000);   // relógio muito adiantado (mais de 1 h): ignorado, como se fosse erro do relógio
  ok(c.length === 4, "relógio absurdo deve ser ignorado: " + c);
  console.log("Idade pelo relógio do navegador (dado sem horário de envio): OK");
  process.exit(0);
})();
