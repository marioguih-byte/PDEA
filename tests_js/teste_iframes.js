// O mapa roda dentro de iframes e quem entrega os dados é OUTRO iframe (um fragmento do Streamlit): os dois precisam se achar pela
// janela mais alta acessível (window.top.__pdeaGlm). Aqui: página principal com dois iframes irmãos (mapa e canal).
const fs = require("fs"), http = require("http"), { JSDOM, ResourceLoader } = require("jsdom");
(async () => {
  // os iframes são carregados de um servidor local (mesma origem que a página principal, como no Streamlit)
  const srv = http.createServer((req, res) => {
    const arq = req.url.split("?")[0].replace("/", "");
    if (["mapa.html", "canal.html"].includes(arq)) { res.writeHead(200, {"Content-Type": "text/html"}); res.end(fs.readFileSync(arq)); }
    else { res.writeHead(404); res.end("nao"); }
  });
  await new Promise(r => srv.listen(0, r));
  const porta = srv.address().port;
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {url: `http://localhost:${porta}/`, runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = () => Promise.reject(new Error("sem rede")); } });
  const w = dom.window, doc = w.document, dorme = (ms) => new Promise(r => setTimeout(r, ms));
  const mapaFrame = doc.createElement("iframe");
  mapaFrame.src = "/mapa.html";
  doc.body.appendChild(mapaFrame);
  await dorme(1500);
  const jm = mapaFrame.contentWindow;
  ok = (c, m) => { if (!c) { console.error("FALHOU:", m); process.exit(1); } };
  ok(jm && jm.document, "o iframe do mapa não carregou");
  const mapa = Object.keys(jm).filter(k => k.startsWith("map_")).map(k => jm[k])[0];
  ok(mapa, "o mapa não foi criado dentro do iframe");
  const cont = () => Object.values(mapa._layers).filter(l => l instanceof jm.L.CircleMarker).length;
  const antes = cont();   // instantâneo embutido: 4 raios visíveis
  ok(antes === 4, "instantâneo embutido: " + antes);
  // segundo iframe (irmão) entrega o canal com 3 raios MAIS NOVOS
  const canalFrame = doc.createElement("iframe");
  canalFrame.src = "/canal.html";
  doc.body.appendChild(canalFrame);
  await dorme(1500);
  ok(w.__pdeaGlm && w.__pdeaGlm.versao === "teste-iframes", "o canal deveria gravar em window.top.__pdeaGlm (a janela principal)");
  ok(cont() === 3, "o mapa deveria passar a mostrar os 3 raios do canal, veio " + cont());
  console.log("Canal entre iframes irmãos (mapa e fragmento) pela janela mais alta: OK");
  srv.close(); process.exit(0);
})();
