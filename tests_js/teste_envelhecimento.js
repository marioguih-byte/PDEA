// Verifica que os raios mudam de cor com o tempo (vermelho, laranja, amarelo, verde) e SOMEM depois de 20 min,
// sem nenhuma requisição nova: o relógio do navegador é adiantado e os temporizadores são acelerados.
const fs = require("fs"), { JSDOM } = require("jsdom");
let adianta = 0;  // ms adiantados no relógio do navegador
(async () => {
  const dom = new JSDOM(fs.readFileSync("mapa.html", "utf8"), {url: "http://localhost:1/", runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = () => Promise.reject(new Error("sem rede"));
      const pn = w.performance.now.bind(w.performance); w.performance.now = () => pn() + adianta;
      const si = w.setInterval.bind(w); w.setInterval = (f, ms) => si(f, Math.max(5, ms / 100));  // 30 s vira 300 ms
    } });
  const w = dom.window, dorme = (ms) => new Promise(r => setTimeout(r, ms));
  await dorme(300);
  const mapa = Object.keys(w).filter(k => k.startsWith("map_")).map(k => w[k])[0];
  const cores = () => Object.values(mapa._layers).filter(l => l instanceof w.L.CircleMarker).map(l => l.options.fillColor);
  const ok = (c, m) => { if (!c) { console.error("FALHOU:", m); process.exit(1); } };
  // instantâneo: idades A=60, B=400, C=700, D=1100 s (+ uma de 1300 s, já fora da janela)
  const vermelho = "#e11d1d", laranja = "#ff8a00", amarelo = "#ffe000", verde = "#2ecc40";
  const igual = (a, b) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
  ok(igual(cores(), [vermelho, laranja, amarelo, verde]), "início: " + cores());
  adianta = 3 * 60 * 1000; await dorme(700);    // A=4 min (vermelho), B=9,7 (laranja), C=14,7 (amarelo), D=21,3 (some)
  ok(igual(cores(), [vermelho, laranja, amarelo]), "+3 min: " + cores());
  adianta = 8 * 60 * 1000; await dorme(700);    // A=9 (laranja), B=14,7 (amarelo), C=19,7 (verde), D some
  ok(igual(cores(), [laranja, amarelo, verde]), "+8 min: " + cores());
  adianta = 12 * 60 * 1000; await dorme(700);   // A=13 (amarelo), B=18,7 (verde), C some
  ok(igual(cores(), [amarelo, verde]), "+12 min: " + cores());
  adianta = 17 * 60 * 1000; await dorme(700);   // A=18 (verde), B some
  ok(igual(cores(), [verde]), "+17 min: " + cores());
  adianta = 20 * 60 * 1000; await dorme(700);   // tudo some
  ok(cores().length === 0, "+20 min: " + cores());
  console.log("Envelhecimento (vermelho > laranja > amarelo > verde > some em 20 min): OK");
  process.exit(0);
})();
