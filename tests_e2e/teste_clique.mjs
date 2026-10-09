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
    // só vale um mapa NOVO (sem cliques anteriores): depois de fechar o painel ou trocar o método o mapa é recriado, e clicar no antigo se perde
    for (const f of p.frames()) { try { if ((await f.$(".pdea-pin")) && await f.evaluate(() => !window.__GLOBAL_DATA__ || window.__GLOBAL_DATA__.last_object_clicked_count === 0)) return f; } catch (e) {} }
    await dorme(1000);
  }
  return null;
}
const dialogoAberto = async (titulo) => p.evaluate(t => document.body.innerText.includes("Como o score foi calculado") && document.body.innerText.includes(t), titulo);
async function esperarDialogo(titulo, ate = 25) { for (let i = 0; i < ate; i++) { if (await dialogoAberto(titulo)) return true; await dorme(1000); } return false; }
async function clicarBolinha(frame, k) {   // clica na bolinha k, se ela estiver bem visível na janela e livre (nada por cima)
  const pins = await frame.$$(".pdea-pin");
  if (k >= pins.length) return false;
  await pins[k].evaluate(e => e.scrollIntoView({block: "center"}));
  await dorme(600);
  const livre = await pins[k].evaluate(e => { const r = e.getBoundingClientRect(); const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); return !!t && (t === e || e.contains(t)); });
  const caixa = await pins[k].boundingBox();   // posição na JANELA (a barra do Streamlit, no topo, fica fora do iframe do mapa)
  if (!(livre && caixa && caixa.y > 120 && caixa.y < 760 && caixa.x > 340)) return false;
  await pins[k].click();
  return true;
}
// Tenta bolinhas em sequência até uma abrir o painel (algumas posições do mapa não recebem o clique do automatizador). Se o mapa
// estivesse perdendo cliques de verdade, NENHUMA abriria e o teste falharia do mesmo jeito.
async function abrirPainelPorBolinha(frame, inicio, tentativas = 8) {
  for (let k = inicio; k < inicio + tentativas; k++) {
    if (!(await clicarBolinha(frame, k))) continue;
    if (await esperarDialogo("", 5)) return true;
  }
  return false;
}
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
if (!(await abrirPainelPorBolinha(mapa, 0))) { await p.screenshot({path: "falha_abrir.png"}); falha("o painel da capital não abriu ao clicar na bolinha"); }
console.log("1) clique na bolinha: painel da capital abriu");
// 3) o painel resiste às atualizações do canal (o fragmento roda a cada 30 s e o servidor gera dado novo a cada 20 s)
await dorme(65000);
if (!(await dialogoAberto(""))) { await p.screenshot({path: "falha_fechou.png"}); falha("o painel fechou sozinho durante as atualizações do canal"); }
console.log("2) painel continuou aberto depois de 65 s (duas atualizações do canal)");
// 4) fechar e abrir outra capital
await p.evaluate(() => { const b = [...document.querySelectorAll("button")].find(x => x.innerText.trim() === "Fechar"); if (b) b.click(); });
for (let i = 0; i < 15 && await dialogoAberto(""); i++) await dorme(1000);
if (await dialogoAberto("")) falha("o botão Fechar não fechou o painel");
console.log("3) botão Fechar fechou o painel");
mapa = await acharMapa();
await dorme(2000);
const segunda = await abrirPainelPorBolinha(mapa, 3);
let outra = false;
for (let i = 0; i < 25 && !outra; i++) { outra = await p.evaluate(() => document.body.innerText.includes("Como o score foi calculado")); if (!outra) await dorme(1000); }
if (!outra) { await p.screenshot({path: "falha_outra.png"}); falha("o painel não abriu na segunda capital"); }
console.log("4) segunda capital: painel abriu");

// O método padrão é o HEURÍSTICO: o painel já abre com o cartão "Método" e o aviso do heurístico.
async function fecharPainel() {
  await p.evaluate(() => { const b = [...document.querySelectorAll("button")].find(x => x.innerText.trim() === "Fechar"); if (b) b.click(); });
  for (let i = 0; i < 15 && await p.evaluate(() => document.body.innerText.includes("Como o score foi calculado")); i++) await dorme(1000);
}
async function escolherMetodo(trecho) {
  await p.evaluate(t => { const l = [...document.querySelectorAll('[data-testid="stSidebar"] label')].find(x => x.innerText.includes(t)); l.click(); }, trecho);
}
const metodoNaTela = (nome) => p.evaluate(n => new RegExp("M[ÉE]TODO\\s*\\n?\\s*" + n, "i").test(document.body.innerText), nome);
const avisoHeuristico = () => p.evaluate(() => document.body.innerText.includes("Método heurístico:"));
async function esperar(condicao, tentativas = 40) { for (let i = 0; i < tentativas; i++) { if (await condicao()) return true; await dorme(1000); } return false; }

if (!(await metodoNaTela("Heurístico \\(CAPE, LI e CIN\\)")) || !(await avisoHeuristico())) falha("o painel não abriu no método heurístico (padrão)");
console.log("5) o painel abre no método heurístico (padrão): cartão Método e aviso");

// 5b) ajuste por chuva no Nordeste: o painel de Fortaleza (CE), aberto pela lista da barra lateral, mostra os pontos e a linha "Chuva prevista"
await fecharPainel();
await p.evaluate(c => { const b = [...document.querySelectorAll('[data-testid="stSidebar"] button')].find(x => x.innerText.includes(c)); b.click(); }, "Fortaleza");
const ajusteNE = await esperar(() => p.evaluate(() => { const t = document.body.innerText; return t.includes("Soma") && t.includes("Chuva prevista") && (t.includes("score reduzido nesta região") || t.includes("score mantido")); }), 30);
if (!ajusteNE) { await p.screenshot({path: "falha_ajuste_ne.png"}); falha("o painel de Fortaleza não mostrou o ajuste por chuva do Nordeste"); }
console.log("5b) painel de Fortaleza (CE): pontos de CAPE, LI e CIN e o ajuste por chuva do Nordeste");
await fecharPainel();
const assentar = async () => { mapa = await acharMapa(); await dorme(4000); };   // depois de fechar o painel o mapa é recriado: espera antes de clicar na lista
// Abre o painel de uma capital pela lista da barra lateral. Se o clique se perder porque o mapa estava sendo recriado, tenta de novo (até 3
// vezes); se o app NUNCA abrir o painel, o teste falha do mesmo jeito.
async function abrirPelaLista(capital) {
  for (let tentativa = 0; tentativa < 3; tentativa++) {
    await assentar();
    await p.evaluate(c => { const b = [...document.querySelectorAll('[data-testid="stSidebar"] button')].find(x => x.innerText.includes(c)); b.click(); }, capital);
    // o painel está aberto quando aparece o seu título (o texto "Soma" também existe na barra lateral, então não serve de prova)
    if (await esperar(() => p.evaluate(() => document.body.innerText.includes("Como o score foi calculado")), 14)) { await dorme(1000); return true; }
  }
  return false;
}
const linhaDoAjuste = () => p.evaluate(() => { const t = document.body.innerText; return t.includes("score reduzido nesta região") || t.includes("score mantido"); });
// Maceió (AL) foi acrescentada à lista do documento: tem a linha do ajuste; Rio de Janeiro (litoral fora do Nordeste) não tem
if (!(await abrirPelaLista("Maceió")) || !(await linhaDoAjuste())) { await p.screenshot({path: "falha_maceio.png"}); falha("o painel de Maceió não mostrou o ajuste por chuva"); }
console.log("5c) painel de Maceió (AL): ajuste por chuva presente (capital acrescentada à lista do documento)");
await fecharPainel();
if (!(await abrirPelaLista("Rio de Janeiro")) || (await linhaDoAjuste())) { await p.screenshot({path: "falha_rio.png"}); falha("o painel do Rio de Janeiro não deveria ter a linha do ajuste por chuva"); }
console.log("5d) painel do Rio de Janeiro: sem ajuste por chuva (fora da lista)");
await fecharPainel();
// costa norte (foz do Amazonas): a capital de maior risco sem raios por perto no caso que motivou a extensão
if (!(await abrirPelaLista("Macapá")) || !(await linhaDoAjuste())) { await p.screenshot({path: "falha_macapa.png"}); falha("o painel de Macapá não mostrou o ajuste por chuva"); }
console.log("5e) painel de Macapá (AP): ajuste por chuva presente");
await fecharPainel();

// 6) trocar para CAPE × chuva: o aviso some, o cartão muda e o painel da capital mostra a tabela do produto (sem a soma de pontos)
await escolherMetodo("CAPE × chuva");
if (!(await esperar(async () => !(await avisoHeuristico()) && await metodoNaTela("CAPE × chuva")))) { await p.screenshot({path: "falha_metodo.png"}); falha("a troca para CAPE × chuva não apareceu no cabeçalho e no aviso"); }
mapa = await acharMapa();
await dorme(2500);
await abrirPainelPorBolinha(mapa, 0);
const tabelaProduto = await esperar(() => p.evaluate(() => { const t = document.body.innerText; return t.includes("Como o score foi calculado") && t.includes("escala logarítmica") && !t.includes("Soma"); }), 30);
if (!tabelaProduto) { await p.screenshot({path: "falha_tabela_produto.png"}); falha("o painel da capital não mostrou a tabela do CAPE × chuva"); }
console.log("6) CAPE × chuva: aviso some, cartão muda e o painel mostra o produto CAPE × chuva");
await fecharPainel();

// 7) voltar ao heurístico restaura o aviso e o cartão
await escolherMetodo("Heurístico");
if (!(await esperar(async () => (await avisoHeuristico()) && await metodoNaTela("Heurístico \\(CAPE, LI e CIN\\)")))) falha("a volta ao método heurístico não apareceu");
console.log("7) volta ao método heurístico: aviso e cartão restaurados");
if (erros.length) console.log("avisos de JS (informativo):", erros.slice(0, 3));
console.log("TESTE DE NAVEGADOR: OK");
await b.close();
