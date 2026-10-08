// Teste de navegador de verdade (Chromium sem tela): abre o painel, confere os raios por cor, clica na bolinha de uma capital e
// verifica que o painel de detalhes abre, fecha, abre em outra capital e SOBREVIVE às atualizações do canal dos raios.
// O sandbox/CI pode não alcançar CDNs: o Leaflet e o jQuery vêm do node_modules e o resto é esvaziado.
import chromium from "@sparticuz/chromium";
import puppeteer from "puppeteer-core";
import fs from "fs";
const URL = process.env.URL || "http://localhost:8765";
const dorme = ms => new Promise(r => setTimeout(r, ms));
const falha = m => { console.error("FALHOU:", m); process.exit(1); };
const b = await puppeteer.launch({executablePath: await chromium.executablePath(), args: [...chromium.args, "--no-sandbox"], headless: "shell"});
const p = await b.newPage();
await p.setViewport({width: 1500, height: 900});
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");
await p.setRequestInterception(true);
p.on("request", req => {
  const u = req.url();
  if (u.startsWith("http://localhost") || u.startsWith("data:") || u.startsWith("blob:")) return req.continue();
  if (/leaflet(\.min)?\.js/.test(u) && !/awesome|rotate|fullscreen/i.test(u)) return req.respond({status: 200, contentType: "application/javascript", body: fs.readFileSync("node_modules/leaflet/dist/leaflet.js")});
  if (/leaflet(\.min)?\.css/.test(u) && !/awesome|rotate/i.test(u)) return req.respond({status: 200, contentType: "text/css", body: fs.readFileSync("node_modules/leaflet/dist/leaflet.css")});
  if (/jquery/.test(u) && u.endsWith(".js")) return req.respond({status: 200, contentType: "application/javascript", body: fs.readFileSync("node_modules/jquery/dist/jquery.min.js")});
  if (/\.(png|jpg|jpeg|gif)(\?|$)/.test(u)) return req.respond({status: 200, contentType: "image/png", body: PNG});
  if (/\.js(\?|$)/.test(u)) return req.respond({status: 200, contentType: "application/javascript", body: ""});
  if (/\.css(\?|$)/.test(u)) return req.respond({status: 200, contentType: "text/css", body: ""});
  return req.respond({status: 200, contentType: "text/plain", body: ""});
});
const erros = [];
p.on("pageerror", e => erros.push(e.message));

async function acharMapa() {
  for (let i = 0; i < 90; i++) {
    for (const f of p.frames()) { try { if (await f.$(".pdea-pin")) return f; } catch (e) {} }
    await dorme(1000);
  }
  return null;
}
const dialogoAberto = async (titulo) => p.evaluate(t => document.body.innerText.includes("Como o score foi calculado") && document.body.innerText.includes(t), titulo);
async function esperarDialogo(titulo, ate = 25) { for (let i = 0; i < ate; i++) { if (await dialogoAberto(titulo)) return true; await dorme(1000); } return false; }
async function clicarCapital(frame, texto) {
  for (const pin of await frame.$$(".pdea-pin")) {
    if ((await pin.evaluate(e => e.textContent)) === texto) {
      // rola a bolinha para o centro da tela e espera a rolagem terminar ANTES de clicar: o clique automático do Puppeteer calcula a
      // posição durante a rolagem e pode errar o alvo (o que, num teste, parece "o painel não abriu")
      await pin.evaluate(e => e.scrollIntoView({block: "center"}));
      await dorme(800);
      await pin.click();
      return true;
    }
  }
  return false;
}

await p.goto(URL, {waitUntil: "domcontentloaded"});
let mapa = await acharMapa();
if (!mapa) falha("o mapa não apareceu");
await dorme(3000);
// 1) raios: o cartão tem contagem em TODAS as quatro cores, inclusive o verde
const cores = await mapa.$$eval(".pdea-glm-chip .g-cls", els => els.map(e => parseInt(e.textContent.replace(/\D/g, ""), 10)));
console.log("raios por cor (vermelho, laranja, amarelo, verde):", cores);
if (cores.length !== 4 || !cores.every(n => n > 0)) falha("alguma cor de raio está zerada: " + cores);
// 2) clicar na bolinha abre o painel de detalhes
if (!(await clicarCapital(mapa, "81"))) falha("bolinha 81 não encontrada");
if (!(await esperarDialogo("Porto Alegre"))) { await p.screenshot({path: "falha_abrir.png"}); falha("o painel de Porto Alegre não abriu ao clicar na bolinha"); }
console.log("1) clique na bolinha: painel de Porto Alegre abriu");
// 3) o painel resiste às atualizações do canal (o fragmento roda a cada 30 s e o servidor gera dado novo a cada 20 s)
await dorme(65000);
if (!(await dialogoAberto("Porto Alegre"))) { await p.screenshot({path: "falha_fechou.png"}); falha("o painel fechou sozinho durante as atualizações do canal"); }
console.log("2) painel continuou aberto depois de 65 s (duas atualizações do canal)");
// 4) fechar e abrir outra capital
await p.evaluate(() => { const b = [...document.querySelectorAll("button")].find(x => x.innerText.trim() === "Fechar"); if (b) b.click(); });
for (let i = 0; i < 15 && await dialogoAberto("Porto Alegre"); i++) await dorme(1000);
if (await dialogoAberto("Porto Alegre")) falha("o botão Fechar não fechou o painel");
console.log("3) botão Fechar fechou o painel");
mapa = await acharMapa();
await dorme(2000);
if (!(await clicarCapital(mapa, "77"))) falha("bolinha 77 não encontrada");
let outra = false;
for (let i = 0; i < 25 && !outra; i++) { outra = await p.evaluate(() => document.body.innerText.includes("Como o score foi calculado")); if (!outra) await dorme(1000); }
if (!outra) { await p.screenshot({path: "falha_outra.png"}); falha("o painel não abriu na segunda capital"); }
console.log("4) segunda capital: painel abriu");
if (erros.length) console.log("avisos de JS (informativo):", erros.slice(0, 3));
console.log("TESTE DE NAVEGADOR: OK");
await b.close();
