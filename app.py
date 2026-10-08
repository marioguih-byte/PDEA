"""PDEA em Streamlit.

Painel operacional com barra lateral escura, mapa claro (OpenStreetMap) travado
no Brasil, seletor de hora da previsão, tendência por capital, calibração da
heurística, exportação e detalhe horário com gráficos para cada capital.
"""

from __future__ import annotations

import base64
import io
import json
import time
from datetime import datetime
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Optional

import altair as alt
import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from branca.element import Element, MacroElement
from jinja2 import Template
from streamlit_folium import st_folium


# Carimbo de versão: fica no texto de ajuda (passar o mouse) dos créditos da barra lateral, para conferir qual cópia está no ar.
# Alcance ao redor das capitais (km, cor): os mesmos raios usados na verificação contra o GLM. Linhas grossas e tracejadas.
ALCANCES_KM = ((30, "#2ecc40"), (50, "#ffd400"), (100, "#e11d1d"))  # verde, amarelo, vermelho
# Responsáveis pela elaboração (aparecem em destaque na barra lateral).
RESPONSAVEIS = (
    ("Mário Henrique", "mario.vanderlei@icat.ufal.br"),
    ("Mayara Christine", "mayara.lins@icat.ufal.br"),
)
VERSAO_APP = "2026-10-08i"  # aparece só ao passar o mouse nos créditos da barra lateral

try:  # raios do GLM em tempo real: precisa de requests, numpy e netCDF4
    import glm_ao_vivo
except Exception:  # noqa: BLE001 - sem o módulo/dependência, o painel só esconde a opção
    glm_ao_vivo = None

st.set_page_config(
    page_title="PDEA | Painel Meteorológico",
    page_icon=str(Path(__file__).resolve().parent / "assets" / "pdea_icone.png"),
    layout="wide",
    initial_sidebar_state="auto",  # recolhida automaticamente em telas pequenas
)

# Os módulos do projeto precisam ser todos da mesma versão. Se o app foi publicado com algum arquivo
# desatualizado (por exemplo, só o app.py foi trocado no GitHub), o Streamlit Cloud esconde o erro real;
# aqui mostramos qual módulo está desatualizado e o que fazer.
try:
    import historico
    from analise import (
        carregar_gate,
        carregar_regioes,
        consolidar,
        explicar_hora,
        horas_a_frente,
        rotulo_horario,
        series_por_unidade,
    )
    from app_camadas import GOES_ATRIBUICAO, GOES_ZOOM_NATIVO, url_goes
    from modelos import MODELOS, TZ_BRASILIA, ErroBuscaModelo, buscar_modelo, horario_local
    from relatorio import gerar_pdf, gerar_png
    from risco_raio import (
        CORES_NIVEL,
        ICONES,
        NIVEIS_RISCO,
        ROTULOS,
        ParametrosRisco,
    )
    from unidades import ESTACOES
except ImportError as _erro_import:
    st.error(
        "**Arquivos do projeto de versões diferentes.** Algum módulo está desatualizado ou ausente no repositório "
        "publicado. Envie para o GitHub **todos** os arquivos do `.zip` (em especial `analise.py`, `risco_raio.py`, "
        "`modelos.py`, `historico.py`, `app_camadas.py`, `relatorio.py`, `unidades.py`, `config_regioes.json` e a pasta "
        "`dados/`), confirme o commit e reinicie o app (*Manage app → Reboot*)."
    )
    st.code(f"{type(_erro_import).__name__}: {_erro_import}")
    st.stop()

RAIZ = Path(__file__).resolve().parent

# OpenStreetMap é o padrão (primeiro item). O mapa escuro foi removido.
TILES = {
    "OpenStreetMap": {"tiles": "OpenStreetMap", "attr": "© OpenStreetMap contributors"},
    "Satélite": {
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles © Esri",
    },
}

# Enquadramento fixo no Brasil: sudoeste e nordeste do território continental.
CENTRO_BRASIL = [-14.2, -53.4]
ZOOM_BRASIL = 4
LIMITES_BRASIL = [[-33.75, -74.0], [5.3, -34.8]]
# Limite de navegação (folga pequena em volta do Brasil).
LIMITES_NAVEGACAO = {"min_lat": -36.0, "min_lon": -77.0, "max_lat": 8.0, "max_lon": -31.0}

HORIZONTE_MAX_H = 48
ALTURA_MAPA = 680

CACHE_HORARIO_LOCAL = "america_sao_paulo_v3"
ESPERA_APOS_FALHA_S = 60  # após uma falha, usa o último dado válido por este tempo sem tentar de novo


# ----------------------------------------------------------------------------
# Dados: cache, tentativas e último dado válido
# ----------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def _buscar_modelo_cache(modelo_id: str, versao_cache: str) -> dict[str, Any]:
    """Mantém a previsão consultada por dez minutos, separada por versão temporal."""
    return buscar_modelo(modelo_id)


@st.cache_resource(show_spinner=False)
def _estado_dados() -> dict[str, dict[str, Any]]:
    """Último dado válido e instante da última falha, por modelo (compartilhado entre sessões)."""
    return {"valido": {}, "falha": {}}


def limpar_cache_dados() -> None:
    _buscar_modelo_cache.clear()
    _estado_dados()["falha"].clear()


def carregar_dados(modelo_id: str) -> tuple[dict[str, Any], Optional[str]]:
    """Devolve (dados, aviso). Se a API falhar, usa o último dado válido e avisa."""
    estado = _estado_dados()
    ultima_falha = estado["falha"].get(modelo_id)
    valido = estado["valido"].get(modelo_id)
    if ultima_falha and valido and time.time() - ultima_falha < ESPERA_APOS_FALHA_S:
        return valido, _aviso_defasado(valido, "serviço indisponível; nova tentativa em instantes")
    try:
        dados = _buscar_modelo_cache(modelo_id, CACHE_HORARIO_LOCAL)
    except ErroBuscaModelo as erro:
        estado["falha"][modelo_id] = time.time()
        if valido:
            return valido, _aviso_defasado(valido, str(erro))
        raise
    estado["valido"][modelo_id] = dados
    estado["falha"].pop(modelo_id, None)
    return dados, None


def _minutos_desde(dados: dict[str, Any]) -> Optional[int]:
    try:
        obtido = datetime.fromisoformat(dados["_obtido_em"])
    except (KeyError, ValueError):
        return None
    return max(0, int((datetime.now(TZ_BRASILIA) - obtido).total_seconds() // 60))


def _aviso_defasado(dados: dict[str, Any], motivo: str) -> str:
    minutos = _minutos_desde(dados)
    quando = f"de {minutos} min atrás" if minutos is not None else "anteriores"
    return f"Exibindo dados {quando} — {motivo}."


def _inicializar_estado() -> None:
    valores_iniciais = {
        "modelo_anterior": None,
        "estacao_popup": None,
        "abrir_popup": False,
        "ultimo_clique_mapa": None,
        "versao_mapa": 0,
        "relatorio": None,
    }
    for chave, valor in valores_iniciais.items():
        st.session_state.setdefault(chave, valor)


@lru_cache(maxsize=1)
def _logo_base64() -> str:
    """Logo do PDEA embutida (data URI), para não depender de arquivos estáticos."""
    arq = Path(__file__).resolve().parent / "assets" / "pdea_logo.png"
    try:
        return "data:image/png;base64," + base64.b64encode(arq.read_bytes()).decode("ascii")
    except OSError:
        return ""


def _html_logo(classe: str) -> str:
    uri = _logo_base64()
    if not uri:
        return "<b>PDEA</b>"
    return f"<img class='{classe}' src='{uri}' alt='PDEA: Preditor de Descargas Elétricas Atmosféricas'>"


def recriar_mapa() -> None:
    """Descarta o clique antigo do componente (a posição é preservada no navegador)."""
    st.session_state["versao_mapa"] += 1


def selecionar_estacao(nome: str) -> None:
    """Seleciona a capital e solicita a abertura do detalhamento (o mapa não se move)."""
    st.session_state["estacao_popup"] = nome
    st.session_state["abrir_popup"] = True


def _numero(valor: Optional[float], casas: int = 0) -> str:
    if valor is None or pd.isna(valor):
        return "—"
    return f"{valor:.{casas}f}"


def _cor_texto(cor_hex: str) -> str:
    """Preto ou branco, conforme o contraste com a cor de fundo."""
    h = cor_hex.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return "#111827" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


def _rgba(cor_hex: str, alfa: float) -> str:
    """Converte #rrggbb em rgba(r, g, b, alfa)."""
    h = cor_hex.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alfa})"


@st.cache_resource(show_spinner=False)
def carregar_geojson(arquivo: str) -> dict[str, Any]:
    """Carrega o contorno/máscara do Brasil empacotado em dados/."""
    return json.loads((RAIZ / "dados" / arquivo).read_text(encoding="utf-8"))


def adicionar_alcance(mapa: folium.Map, tabela: pd.DataFrame) -> None:
    """Anéis de 30, 50 e 100 km (verde, amarelo e vermelho; grossos e tracejados) ao redor de cada capital, em qualquer zoom."""
    grupo = folium.FeatureGroup(name="Alcance (30, 50 e 100 km)", control=False)
    for _, linha in tabela.iterrows():
        for km, cor_anel in reversed(ALCANCES_KM):  # do maior para o menor: o anel de 30 km fica por cima
            anel = folium.Circle(
                location=[linha["Latitude"], linha["Longitude"]], radius=km * 1000, color=cor_anel, weight=4,
                opacity=0.95, dash_array="12 8", fill=True, fill_color=cor_anel, fill_opacity=0.04,
            )
            anel.options["interactive"] = False  # o folium descarta esse argumento no construtor; sem isso o anel captura o mouse
            anel.add_to(grupo)
    grupo.add_to(mapa)


class RaiosGLM(MacroElement):
    """Camada de raios do GLM que se atualiza sozinha no navegador, sem recarregar o mapa nem a página.

    Há duas fontes de dados, e vale sempre a MAIS NOVA (um arquivo velho ou em cache nunca sobrescreve um dado recente):
      1. Canal da página: um fragmento do Streamlit (``canal_glm``) entrega a coleta mais recente do servidor em
         ``window.top.__pdeaGlm`` a cada 5 min. Não depende do arquivo estático nem de gravação em disco.
      2. Arquivo ``static/glm_flashes.txt`` (consulta o ``glm_meta.txt`` a cada 30 s, com parâmetro anti-cache).
    O instantâneo ``inicial`` (embutido no mapa) cobre o primeiro desenho.
    """

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        (function () {
            var mapa = {{ this._parent.get_name() }};
            function origem() {
                var o = window.origin;
                return (o && o !== 'null') ? o : '';
            }
            function topo() {   // a janela mais alta que este mapa consegue acessar (o mapa roda dentro de iframes)
                var w = window;
                try { while (w.parent && w.parent !== w) { void w.parent.document; w = w.parent; } } catch (e) {}
                return w;
            }
            function bust(u) { return u + (u.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now(); }
            var URLS = {{ this.urls }}.map(function (u) { return origem() + u; });
            var JANELA_S = {{ this.janela_s }};
            var INTERVALO_MS = {{ this.intervalo_ms }};
            var CANAL_MS = {{ this.canal_ms }};
            var inicial = {{ this.inicial }};
            var grupo = L.layerGroup().addTo(mapa);
            var estado = {gerado: null, recebido: 0, desloc: 0, dados: null, url: null};

            var chip = L.control({position: 'topright'});
            chip.onAdd = function () {
                var d = L.DomUtil.create('div', 'pdea-glm-chip');
                d.innerHTML = '<b>&#9889; Raios GLM</b><br><span class="g-n">carregando...</span>';
                L.DomEvent.disableClickPropagation(d);
                return d;
            };
            chip.addTo(mapa);

            function hora(ms) {
                try {
                    return new Date(ms).toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit', timeZone: 'America/Sao_Paulo'});
                } catch (e) { return ''; }
            }
            function agoraServidor() {
                return estado.gerado + estado.desloc + (performance.now() - estado.recebido) / 1000;
            }
            function estilo(idadeS) {
                var m = idadeS / 60;
                if (m <= 5) { return {r: 4.5, f: '#e11d1d', b: '#5c0a0a'}; }    // vermelho: os mais recentes
                if (m <= 10) { return {r: 4, f: '#ff8a00', b: '#6b3500'}; }     // laranja
                if (m <= 15) { return {r: 3.5, f: '#ffe000', b: '#6b5a00'}; }   // amarelo
                return {r: 3.6, f: '#2ecc40', b: '#08330f'};                     // verde (15 a 20 min); depois some
            }
            function texto(n, d, cls) {
                var el = chip.getContainer();
                if (!el) { return; }
                if (!d) { el.innerHTML = '<b>&#9889; Raios GLM</b><br><span class="g-n">aguardando dados</span>'; return; }
                var ult = d.ultimo_arquivo ? d.ultimo_arquivo * 1000 : null;
                var atraso = ult ? Math.round((agoraServidor() * 1000 - ult) / 60000) : null;
                var obs = ult ? ('dados at&eacute; ' + hora(ult) + ' (h&aacute; ' + atraso + ' min)') : 'sem arquivos recentes';
                obs += ' &middot; coleta ' + hora(d.gerado * 1000);
                var aviso = (atraso !== null && atraso > 15) ? ' &middot; <span class="g-aviso">atrasado</span>' : '';
                if (d.erro) { aviso += ' &middot; <span class="g-aviso">falha na coleta, tentando de novo</span>'; }
                var cores = ['#e11d1d', '#ff8a00', '#ffe000', '#2ecc40'], rot = ['at&eacute; 5 min', '5 a 10 min', '10 a 15 min', '15 a 20 min'];
                var linha = '';
                if (cls) {
                    for (var c = 0; c < 4; c++) {
                        linha += '<span title="' + rot[c] + '"><span class="g-pt" style="background:' + cores[c] + '"></span><span class="g-cls">' +
                                 cls[c] + '</span></span> ';
                    }
                }
                el.title = 'Coletor ' + (d.versao || 'antigo') + ' · janela ' + d.janela_min + ' min · ' + (d.arquivos || 0) + ' arquivos do GLM';
                el.innerHTML = '<b>&#9889; Raios GLM</b> &middot; ' + n.toLocaleString('pt-BR') + ' nos &uacute;ltimos ' +
                    Math.round(JANELA_S / 60) + ' min<br><span class="g-linha">' + linha + '</span><br><span class="g-n">' + obs + aviso + '</span>';
            }
            function desenhar() {
                var d = estado.dados;
                grupo.clearLayers();
                if (!d || !d.raios) { texto(0, d); return; }
                var agora = agoraServidor();
                var idadeExtra = agora - d.gerado;
                var visiveis = [];
                var cls = [0, 0, 0, 0];
                for (var i = 0; i < d.raios.length; i++) {
                    var r = d.raios[i];
                    var idade = r[2] + idadeExtra;
                    if (idade <= JANELA_S) {
                        visiveis.push([r[0], r[1], idade]);
                        cls[idade <= 300 ? 0 : (idade <= 600 ? 1 : (idade <= 900 ? 2 : 3))] += 1;
                    }
                }
                visiveis.sort(function (a, b) { return b[2] - a[2]; });  // mais antigos primeiro; os novos ficam por cima
                for (var j = 0; j < visiveis.length; j++) {
                    var v = visiveis[j], e = estilo(v[2]);
                    L.circleMarker([v[0], v[1]], {radius: e.r, color: e.b, weight: 0.6, fillColor: e.f, fillOpacity: 0.92,
                                                  opacity: 0.9, interactive: false}).addTo(grupo);
                }
                texto(visiveis.length, d, cls);
            }
            function aplicar(d) {
                if (!d || !d.raios) { return; }
                if (estado.gerado !== null && d.gerado <= estado.gerado) { return; }  // só aceita dado MAIS NOVO
                estado.dados = d; estado.gerado = d.gerado; estado.recebido = performance.now();
                // idade já decorrida desde a coleta: as cores ficam corretas mesmo que o dado tenha chegado minutos depois (mapa recriado, aba antiga)
                var base = (typeof d.agora === 'number') ? d.agora : (Date.now() / 1000);   // hora do servidor (canal) ou, na falta dela, a do navegador
                var dif = base - d.gerado;
                estado.desloc = (dif >= 0 && dif < 3600) ? dif : 0;   // mais de 1 h (ou negativo): relógio do navegador errado, ignora
                desenhar();
            }
            function lerCanal() {
                try { var d = topo().__pdeaGlm; if (d && d.raios) { aplicar(d); } } catch (e) {}
            }
            function tentar(ordem, i) {
                if (i >= ordem.length) { return Promise.reject(new Error('sem arquivo')); }
                var url = ordem[i];
                return fetch(bust(url), {cache: 'no-store'}).then(function (r) {
                    if (!r.ok) { throw new Error('http ' + r.status); }
                    return r.text();
                }).then(function (txt) {
                    var d = JSON.parse(txt);
                    if (!d || !d.raios) { throw new Error('formato'); }
                    estado.url = url;
                    return d;
                }).catch(function () {
                    return tentar(ordem, i + 1);
                });
            }
            function meta(url) {
                return fetch(bust(url.replace('glm_flashes.txt', 'glm_meta.txt')), {cache: 'no-store'}).then(function (r) {
                    if (!r.ok) { throw new Error('http ' + r.status); }
                    return r.text();
                }).then(function (txt) { return JSON.parse(txt); });
            }
            function buscar() {
                try {   // qualquer falha aqui (sem fetch, rede fora...) não pode impedir o canal da página nem os temporizadores
                    // começa pelo endereço que funcionou da última vez; se falhar, testa os demais
                    var ordem = estado.url ? [estado.url].concat(URLS.filter(function (u) { return u !== estado.url; })) : URLS;
                    var passo = estado.url
                        ? meta(estado.url).then(function (m) {
                            return (m && m.gerado <= estado.gerado) ? null : tentar(ordem, 0);  // só baixa o arquivo grande se há dado mais novo
                        }).catch(function () { return tentar(ordem, 0); })
                        : tentar(ordem, 0);
                    passo.then(function (d) { if (d) { aplicar(d); } }).catch(function () {
                        estado.url = null;
                        if (!estado.dados) { texto(0, null); }
                    });
                } catch (e) {
                    estado.url = null;
                    if (!estado.dados) { texto(0, null); }
                }
            }

            if (inicial && inicial.raios) { aplicar(inicial); }
            lerCanal();
            buscar();
            setInterval(lerCanal, CANAL_MS);         // canal da página (memória do navegador): barato, a cada poucos segundos
            setInterval(buscar, INTERVALO_MS);       // arquivo estático: consulta o meta (poucos bytes); só baixa e redesenha se há dado mais novo
            setInterval(desenhar, 30000);            // as cores acompanham a idade dos raios (e os de mais de 20 min somem)
            var tentativas = 0;
            var rapido = setInterval(function () {   // servidor recém-ligado: tenta a cada 5 s até a 1ª coleta terminar
                tentativas += 1;
                if (estado.dados || tentativas > 60) { clearInterval(rapido); } else { buscar(); }
            }, 5000);
        })();
        {% endmacro %}
        """
    )

    def __init__(self, inicial: Optional[dict], caminho_base: str = "", janela_s: int = 1200, intervalo_ms: int = 30000,
                 canal_ms: int = 5000) -> None:
        super().__init__()
        self._name = "RaiosGLM"
        base = (caminho_base or "").rstrip("/")
        arq = "app/static/glm_flashes.txt"
        # O mapa roda dentro de um iframe: usa a origem herdada (window.origin) e, no Streamlit Cloud, também o prefixo /~/+/.
        self.urls = json.dumps([f"{base}/{arq}", f"/~/+/{arq}"])
        self.janela_s = int(janela_s)
        self.intervalo_ms = int(intervalo_ms)
        self.canal_ms = int(canal_ms)
        if inicial and inicial.get("raios"):
            # Só o necessário ao 1º desenho, com a mesma cota por faixa de idade: o amarelo e o verde (mais antigos) não ficam de fora.
            raios_ini = glm_ao_vivo.amostrar_por_faixa(inicial["raios"], 6000) if glm_ao_vivo else sorted(inicial["raios"], key=lambda r: r[2])[:6000]
            recorte = {**inicial, "raios": raios_ini}  # sem horário "de agora": o HTML do mapa tem de ser igual entre execuções
            self.inicial = json.dumps(recorte, separators=(",", ":"))
        else:
            self.inicial = "null"


def _html_canal_glm(dados: dict, erro: Optional[str] = None) -> str:
    """HTML (só um script) que deixa a coleta mais recente em ``window.top.__pdeaGlm`` para os mapas lerem."""
    raios = glm_ao_vivo.amostrar_por_faixa(dados["raios"], 15000) if glm_ao_vivo else dados["raios"]
    carga = {**dados, "raios": raios, "agora": int(time.time()), "erro": bool(erro)}
    js = json.dumps(carga, separators=(",", ":")).replace("</", "<\\/")
    return ("<script>(function(){var w=window;try{while(w.parent&&w.parent!==w){void w.parent.document;w=w.parent;}}catch(e){}"
            "w.__pdeaGlm=" + js + ";})();</script>")


def _incorporar_script(html: str) -> None:
    """Insere um iframe mínimo que só executa o script. ``st.components.v1.html`` está obsoleto (aviso no log do Streamlit e
    remoção anunciada), então usa ``st.iframe`` quando existe e cai para o antigo só em versões que ainda não o têm."""
    if hasattr(st, "iframe"):
        st.iframe(html, height=1)
    else:
        components.html(html, height=0)


@st.fragment(run_every=30)
def canal_glm() -> None:
    """Entrega ao navegador, a cada coleta nova (de 5 em 5 min), os raios do servidor: não depende do arquivo estático.

    Só o fragmento é reexecutado (a cada 30 s); o mapa e o resto da página não são recarregados. Também reinicia a coleta se a
    thread tiver morrido.
    """
    if glm_ao_vivo is None:
        return
    coletor = glm_ao_vivo.obter_atualizador(iniciar=True)
    dados = coletor.ultimo
    if not dados or st.session_state.get("glm_enviado") == dados.get("gerado"):
        return
    st.session_state["glm_enviado"] = dados["gerado"]
    _incorporar_script(_html_canal_glm(dados, coletor.ultimo_erro))


class AjusteBrasil(MacroElement):
    """Mantém o mapa limitado ao Brasil e preserva a posição entre recriações.

    - O zoom mínimo é calculado no navegador a partir do tamanho do mapa, então
      não é possível afastar a visão além do território brasileiro.
    - A posição/zoom atuais são guardados no próprio navegador (sem acionar o
      servidor). Se o mapa for recriado (ao fechar o painel, por exemplo), ele
      reabre exatamente onde estava. Na primeira abertura, enquadra o Brasil.
    """

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        (function () {
            var mapa = {{ this._parent.get_name() }};
            var limites = L.latLngBounds({{ this.limites }});
            var CHAVE = '__pdeaVista';

            function lerVista() {
                try { if (window.parent[CHAVE]) { return window.parent[CHAVE]; } } catch (e) {}
                return null;
            }
            function gravarVista() {
                var c = mapa.getCenter();
                try { window.parent[CHAVE] = {lat: c.lat, lng: c.lng, zoom: mapa.getZoom()}; } catch (e) {}
            }
            function limitarZoom() {
                mapa.invalidateSize();
                mapa.setMinZoom(mapa.getBoundsZoom(limites, false, L.point(8, 8)));
            }
            function enquadrar() {
                mapa.invalidateSize();
                mapa.fitBounds(limites, {padding: [8, 8], animate: false});
                limitarZoom();
            }

            mapa.whenReady(function () {
                var vista = lerVista();
                if (vista) {
                    // Primeiro a posição, depois o zoom mínimo: o inverso dispara uma
                    // animação de zoom que terminaria por cima da posição restaurada.
                    mapa.invalidateSize();
                    mapa.setView([vista.lat, vista.lng], vista.zoom, {animate: false});
                    limitarZoom();
                } else {
                    enquadrar();
                }
                gravarVista();
                mapa.on('moveend', gravarVista);
            });
            window.addEventListener('resize', limitarZoom);

            var controle = L.control({position: 'topleft'});
            controle.onAdd = function () {
                var caixa = L.DomUtil.create('div', 'leaflet-bar pdea-home');
                var botao = L.DomUtil.create('a', '', caixa);
                botao.href = '#';
                botao.title = 'Centralizar no Brasil';
                botao.setAttribute('role', 'button');
                botao.innerHTML = '&#8962;';
                L.DomEvent.disableClickPropagation(caixa);
                L.DomEvent.on(botao, 'click', function (e) {
                    L.DomEvent.preventDefault(e);
                    enquadrar();
                });
                return caixa;
            };
            controle.addTo(mapa);
        })();
        {% endmacro %}
        """
    )

    def __init__(self, limites: list[list[float]]):
        super().__init__()
        self._name = "AjusteBrasil"
        self.limites = json.dumps(limites)


# Lembra se a legenda estava aberta ou fechada, mesmo quando o mapa é redesenhado (o estado fica na janela mais alta acessível).
# Sem escolha anterior, começa recolhida em telas estreitas.
_JS_LEGENDA = """
    <script>
    (function () {
        var el = document.querySelector('details.pdea-legenda');
        if (!el) { return; }
        var CHAVE = '__pdeaLegendaAberta';
        function topo() {
            var w = window;
            try { while (w.parent && w.parent !== w) { void w.parent.document; w = w.parent; } } catch (e) {}
            return w;
        }
        try {
            var guardado = topo()[CHAVE];
            if (guardado === false || (guardado === undefined && window.innerWidth < 700)) { el.removeAttribute('open'); }
        } catch (e) {}
        el.addEventListener('toggle', function () { try { topo()[CHAVE] = el.open; } catch (e) {} });
    })();
    </script>
"""


def _legenda_mapa(cores: dict[str, str], rotulo_hora: str, fonte: str, goes: bool = False, glm: bool = False, alcance: bool = False) -> str:
    itens = "".join(
        f"<div class='rl-item'><span class='rl-dot' style='background:{cores[nivel]}'></span>{nivel}</div>"
        for _, nivel, _ in NIVEIS_RISCO[::-1]
    )
    if goes:
        itens += "<div class='rl-sub' style='margin:4px 0 2px'>☁ Nuvens: GOES-East IR (cores quentes/frias = topos mais altos)</div>"
    if alcance:
        itens += "<div class='rl-sub' style='margin:6px 0 2px'>&#9711; Alcance ao redor da capital:</div>" + "".join(
            f"<div class='rl-item'><span class='rl-anel' style='border-color:{cor_anel}'></span>{km} km</div>" for km, cor_anel in ALCANCES_KM
        )
    if glm:
        itens += (
            "<div class='rl-sub' style='margin:6px 0 2px'>&#9889; Raios observados (GLM), idade:</div>"
            "<div class='rl-item'><span class='rl-raio' style='background:#e11d1d'></span>até 5 min</div>"
            "<div class='rl-item'><span class='rl-raio' style='background:#ff8a00'></span>5 a 10 min</div>"
            "<div class='rl-item'><span class='rl-raio' style='background:#ffe000'></span>10 a 15 min</div>"
            "<div class='rl-item'><span class='rl-raio' style='background:#2ecc40'></span>15 a 20 min</div>"
        )
    return f"""
    <style>
      .leaflet-tooltip {{ font: 600 12px 'Segoe UI', Arial, sans-serif; color:#1f2933; border:0;
        border-radius:6px; padding:4px 8px; box-shadow:0 2px 8px rgba(15,23,42,.25); }}
      .leaflet-popup-content-wrapper {{ border-radius:12px; box-shadow:0 8px 24px rgba(15,23,42,.28); }}
      .leaflet-popup-content {{ margin:0; }}
      .pdea-pin-wrap {{ background:transparent; border:0; }}
      .pdea-pin {{ width:var(--d); height:var(--d); box-sizing:border-box; border-radius:50%; background:var(--c);
        border:2.5px solid #fff; box-shadow:0 0 0 5px var(--h), 0 2px 5px rgba(15,23,42,.35); cursor:pointer;
        display:flex; align-items:center; justify-content:center; color:var(--t);
        font:700 10px/1 'Segoe UI', Arial, sans-serif; letter-spacing:-.02em; transition:box-shadow .15s ease; }}
      .pdea-pin-wrap:hover .pdea-pin {{ box-shadow:0 0 0 8px var(--h), 0 2px 6px rgba(15,23,42,.45); }}
      .pdea-home a {{ font-size:20px; line-height:30px; text-align:center; color:#1f2933; }}
      .pdea-legenda {{ position:absolute; left:12px; bottom:64px; z-index:1000; background:rgba(255,255,255,.94);
        border:1px solid rgba(15,23,42,.12); border-radius:10px; padding:0;
        font:12px 'Segoe UI', Arial, sans-serif; color:#1f2933; box-shadow:0 4px 14px rgba(15,23,42,.18);
        max-height:calc(100vh - 84px); overflow-y:auto; }}
      .pdea-legenda summary {{ display:flex; align-items:center; justify-content:space-between; gap:14px; cursor:pointer; list-style:none;
        padding:8px 12px; user-select:none; }}
      .pdea-legenda summary::-webkit-details-marker {{ display:none; }}
      .pdea-legenda summary::after {{ content:""; width:7px; height:7px; border-right:2px solid #52606d; border-bottom:2px solid #52606d;
        transform:rotate(45deg); margin:-3px 2px 0 0; transition:transform .15s ease; }}
      .pdea-legenda:not([open]) summary::after {{ transform:rotate(-135deg); margin-top:3px; }}
      .pdea-legenda summary:hover .rl-titulo {{ color:#1f2933; }}
      .pdea-legenda summary:focus-visible {{ outline:2px solid #2f7bff; outline-offset:-2px; border-radius:10px; }}
      .pdea-legenda .rl-corpo {{ padding:0 12px 8px; }}
      .pdea-legenda .rl-titulo {{ font-weight:700; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:#52606d; }}
      .pdea-legenda .rl-sub {{ color:#7b8794; font-size:10.5px; margin:-2px 0 4px; }}
      .rl-anel {{ width:12px; height:12px; border-radius:50%; border:3px dashed; box-sizing:border-box; margin:0 0; }}
      .rl-raio {{ width:9px; height:9px; border-radius:50%; border:1px solid rgba(15,23,42,.55); margin:0 1px; }}
      .pdea-glm-chip {{ background:rgba(255,255,255,.94); border:1px solid rgba(15,23,42,.12); border-radius:10px; padding:6px 10px;
        font:12px 'Segoe UI', Arial, sans-serif; color:#1f2933; box-shadow:0 4px 14px rgba(15,23,42,.18); line-height:1.35; }}
      .pdea-glm-chip .g-pt {{ display:inline-block; width:9px; height:9px; border-radius:50%; border:1px solid rgba(15,23,42,.55); margin:0 3px 0 2px; }}
      .pdea-glm-chip .g-linha {{ font-size:11px; }}
      .pdea-glm-chip .g-n {{ color:#52606d; font-size:11px; }}
      .pdea-glm-chip .g-aviso {{ color:#b45309; font-weight:700; }}
      .rl-item {{ display:flex; align-items:center; gap:7px; margin:3px 0; }}
      .rl-dot {{ width:11px; height:11px; border-radius:50%; border:2px solid #fff; box-shadow:0 0 0 1px rgba(15,23,42,.25); }}
    </style>
    <details class="pdea-legenda" open><summary><span class="rl-titulo">Risco de raios</span></summary>
      <div class="rl-corpo"><div class="rl-sub">{escape(rotulo_hora)} · {escape(fonte)}</div>{itens}</div></details>
    """ + _JS_LEGENDA


def _html_popup(linha: pd.Series, cor: str) -> str:
    score = linha["Score"]
    score_txt = "—" if pd.isna(score) else f"{score:.1f}/100"
    seta = f" {linha['Tendência']}" if linha["Tendência"] else ""
    extras = ""
    if not pd.isna(linha["Pico 24 h"]):
        extras += (
            f"<div style='display:flex;justify-content:space-between;'><span>Pico 24 h</span>"
            f"<b>{linha['Pico 24 h']:.0f} · {escape(str(linha['Hora do pico']))}</b></div>"
        )
    if linha.get("Ajuste chuva") == "reduzido":
        extras += "<div style='color:#7b8794;'>☂ Sem chuva prevista nas próximas horas: score reduzido</div>"
    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif; min-width:240px; overflow:hidden; border-radius:12px;">
        <div style="background:{cor}; color:{_cor_texto(cor)}; padding:9px 12px;">
            <div style="font-weight:700; font-size:13px; line-height:1.3;">{escape(str(linha['Capital']))}</div>
            <div style="font-size:12px; opacity:.95; margin-top:2px;">{escape(str(linha['Risco']))} · {score_txt}{seta}</div>
        </div>
        <div style="padding:9px 12px 10px; font-size:12.5px; line-height:1.7; color:#1f2933;">
            <div style="display:flex; justify-content:space-between;"><span>CAPE</span><b>{_numero(linha['CAPE (J/kg)'])} J/kg</b></div>
            <div style="display:flex; justify-content:space-between;"><span>Lifted Index</span><b>{_numero(linha['Lifted Index (°C)'], 1)} °C</b></div>
            <div style="display:flex; justify-content:space-between;"><span>CIN</span><b>{_numero(linha['CIN (J/kg)'])} J/kg</b></div>
            {extras}
            <div style="margin-top:6px; color:#7b8794; font-size:11px;">Clique para abrir a previsão horária.</div>
        </div>
    </div>
    """


def _instantaneo_glm() -> Optional[dict]:
    """Instantâneo dos raios embutido no mapa, CONGELADO enquanto o mapa não é recriado de propósito.

    Se ele mudasse a cada coleta (5 min), o HTML do mapa mudaria entre execuções, o ``streamlit-folium`` recriaria o mapa e um clique
    em andamento (por exemplo, numa bolinha) seria perdido: o painel de detalhes não abriria. Os raios novos chegam ao mapa pelo canal
    da página e pelo arquivo estático, que não mexem no HTML. O instantâneo só é renovado quando o mapa é recriado (``versao_mapa``)
    ou quando já tem mais de 10 min e há um mais novo.
    """
    if glm_ao_vivo is None:
        return None
    try:
        guardado = st.session_state.get("glm_instantaneo")  # (versao_mapa, dados)
        versao = st.session_state.get("versao_mapa")
    except Exception:  # noqa: BLE001 - fora do Streamlit (testes): sem congelar
        return glm_ao_vivo.dados_atuais(aguardar_s=12)
    if guardado is not None and guardado[0] == versao and time.time() - guardado[1]["gerado"] < 600:
        return guardado[1]
    novo = glm_ao_vivo.dados_atuais(aguardar_s=12)
    if novo is None:
        return guardado[1] if guardado is not None and guardado[0] == versao else None
    if guardado is not None and guardado[0] == versao and novo["gerado"] <= guardado[1]["gerado"]:
        return guardado[1]
    try:
        st.session_state["glm_instantaneo"] = (versao, novo)
    except Exception:  # noqa: BLE001
        pass
    return novo


def criar_mapa(
    tabela: pd.DataFrame,
    estilo: str,
    cores: dict[str, str],
    mostrar_score: bool,
    rotulo_hora: str,
    fonte: str,
    divisas: bool = False,
    goes: bool = False,
    goes_opacidade: float = 0.6,
    glm: bool = False,
    alcance: bool = False,
) -> folium.Map:
    """Cria o mapa Folium travado no Brasil, com marcadores clicáveis."""
    configuracao = TILES[estilo]
    mapa = folium.Map(
        location=CENTRO_BRASIL,
        zoom_start=ZOOM_BRASIL,
        tiles=configuracao["tiles"],
        attr=configuracao["attr"],
        control_scale=True,
        prefer_canvas=True,
        zoom_snap=0.25,
        zoom_delta=0.5,
        min_zoom=3,
        max_zoom=13,
        max_bounds=True,
        max_bounds_viscosity=1.0,
        **LIMITES_NAVEGACAO,
    )

    if goes:
        # Abaixo do véu, do contorno e dos marcadores. Se a imagem não carregar, o mapa segue normal.
        folium.TileLayer(
            tiles=url_goes(),
            attr=GOES_ATRIBUICAO,
            name="Topos de nuvem (GOES-East)",
            overlay=True,
            control=False,
            opacity=goes_opacidade,
            max_native_zoom=GOES_ZOOM_NATIVO,
            max_zoom=13,
        ).add_to(mapa)

    # Véu suave fora do Brasil + contorno do país.
    veu = {"fillColor": "#f4f6f8", "fillOpacity": 0.62} if estilo == "OpenStreetMap" else {"fillColor": "#0b1220", "fillOpacity": 0.5}
    folium.GeoJson(
        carregar_geojson("brasil_mascara.geojson"),
        name="Fora do Brasil",
        style_function=lambda _f, veu=veu: {**veu, "weight": 0, "color": "transparent"},
        interactive=False,
    ).add_to(mapa)
    folium.GeoJson(
        carregar_geojson("brasil_contorno.geojson"),
        name="Brasil",
        style_function=lambda _f: {"fill": False, "color": "#1d4e89", "weight": 2, "opacity": 0.85},
        interactive=False,
    ).add_to(mapa)

    if divisas:
        folium.GeoJson(
            carregar_geojson("brasil_estados.geojson"),
            name="Divisas estaduais",
            style_function=lambda _f, g=goes: {
                "fill": False,
                "color": "#ffffff" if g else "#6b7a8c",
                "weight": 1,
                "opacity": 0.85 if g else 0.6,
            },
            interactive=False,
        ).add_to(mapa)

    for _, linha in tabela.iterrows():
        score = linha["Score"]
        cor = cores.get(linha["Risco"], cores["Sem dados"])
        # Marcador HTML (DivIcon): o tamanho é fixo em pixels, inclusive durante a
        # animação de zoom (camadas de canvas/SVG são escaladas nesse momento).
        diametro = 22 if pd.isna(score) else round(22 + min(float(score), 100) / 12)
        texto = "" if not mostrar_score else ("–" if pd.isna(score) else f"{score:.0f}")
        icone = folium.DivIcon(
            html=(
                f'<div class="pdea-pin" style="--c:{cor};--h:{_rgba(cor, 0.30)};--t:{_cor_texto(cor)};--d:{diametro}px">'
                f"{texto}</div>"
            ),
            icon_size=(diametro, diametro),
            icon_anchor=(diametro // 2, diametro // 2),
            popup_anchor=(0, -(diametro // 2) - 2),
            class_name="pdea-pin-wrap",
        )
        folium.Marker(
            location=[linha["Latitude"], linha["Longitude"]],
            icon=icone,
            tooltip=linha["Capital"],
            popup=folium.Popup(_html_popup(linha, cor), max_width=320, auto_pan=False),
            # Risco mais alto sempre por cima quando há sobreposição.
            z_index_offset=0 if pd.isna(score) else int(score * 10),
        ).add_to(mapa)

    if alcance:
        adicionar_alcance(mapa, tabela)
    if glm:
        # Raios do GLM: desenhados no navegador e atualizados sozinhos (sem recarregar a página nem acionar o Streamlit).
        inicial = _instantaneo_glm()  # no 1º acesso, espera a 1ª coleta (raios de antes da abertura)
        mapa.add_child(RaiosGLM(inicial, caminho_base=st.get_option("server.baseUrlPath") or ""))
    mapa.add_child(AjusteBrasil(LIMITES_BRASIL))
    mapa.get_root().html.add_child(Element(_legenda_mapa(cores, rotulo_hora, fonte, goes, glm, alcance)))
    return mapa


# ----------------------------------------------------------------------------
# Detalhamento por unidade (janela): cartões, gráficos e tabela horária
# ----------------------------------------------------------------------------
def _tema_grafico(grafico: alt.Chart) -> alt.Chart:
    return (
        grafico.configure(background="transparent")
        .configure_axis(labelColor="#c9d1db", titleColor="#c9d1db", gridColor="#2b323c", domainColor="#3b4350", tickColor="#3b4350")
        .configure_view(strokeWidth=0)
        .configure_legend(labelColor="#c9d1db", titleColor="#c9d1db", orient="bottom", title=None)
    )


def _dados_grafico(nome: str, ctx: dict[str, Any]) -> pd.DataFrame:
    """Séries horárias da unidade (da hora atual até +48 h): CAPE, LI, CIN e score."""
    serie = ctx["dados"].get(nome, {})
    inicio, tempos = serie.get("idx_atual", 0), serie.get("tempos", [])
    rel = ctx["series"][nome]["rel"]
    linhas = []

    def v(campo: str, i: int) -> Optional[float]:
        valores = serie.get(campo, [])
        return valores[i] if i < len(valores) else None

    for i in range(inicio, min(len(tempos), inicio + HORIZONTE_MAX_H + 1)):
        k = i - inicio
        t850, t500 = v("t850", i), v("t500", i)
        linhas.append(
            {
                "tempo": pd.to_datetime(tempos[i]),
                "CAPE": v("cape", i),
                "LI": v("li", i),
                "CIN": v("cin", i),
                "Score": rel[k] if k < len(rel) else None,
                "Precipitação": v("precip", i),
                "Rajada": v("rajada", i),
                "Gradiente": (t850 - t500) if t850 is not None and t500 is not None else None,
                "Nível 0 °C": v("nivel0", i),
            }
        )
    return pd.DataFrame(linhas)


def _grafico_score(df: pd.DataFrame, cores: dict[str, str], tempo_sel: pd.Timestamp) -> alt.Chart:
    limites = [0] + [min(limite, 100) for limite, _, _ in NIVEIS_RISCO]
    bandas = pd.DataFrame(
        [{"y0": limites[i], "y1": limites[i + 1], "Nível": rotulo} for i, (_, rotulo, _) in enumerate(NIVEIS_RISCO)]
    )
    eixo_x = alt.X("tempo:T", title=None, axis=alt.Axis(format="%d/%m %Hh", labelAngle=0, tickCount=8))
    eixo_y = alt.Y("Score:Q", scale=alt.Scale(domain=[0, 100]), title="Score")
    camadas = [
        alt.Chart(bandas)
        .mark_rect(opacity=0.17)
        .encode(
            y=alt.Y("y0:Q", scale=alt.Scale(domain=[0, 100]), title="Score"),
            y2="y1:Q",
            color=alt.Color("Nível:N", scale=alt.Scale(domain=ROTULOS, range=[cores[r] for r in ROTULOS]), legend=None),
        )
    ]
    camadas.append(
        alt.Chart(df)
        .mark_line(strokeWidth=3, color="#f2f4f8", interpolate="monotone")
        .encode(x=eixo_x, y=eixo_y, tooltip=["tempo:T", alt.Tooltip("Score:Q", format=".1f")])
    )
    camadas.append(
        alt.Chart(pd.DataFrame({"tempo": [tempo_sel]}))
        .mark_rule(color="#e0b400", strokeDash=[4, 3], strokeWidth=2)
        .encode(x="tempo:T")
    )
    return _tema_grafico(alt.layer(*camadas).properties(height=230, width="container"))


def _grafico_variavel(
    df: pd.DataFrame,
    coluna: str,
    titulo: str,
    cor: str,
    tempo_sel: Optional[pd.Timestamp] = None,
    linha_zero: bool = False,
    barras: bool = False,
    formato_eixo: str = "%Hh",
) -> alt.Chart:
    base = alt.Chart(df).encode(
        x=alt.X("tempo:T", title=None, axis=alt.Axis(format=formato_eixo, labelAngle=0, tickCount=5)),
        y=alt.Y(f"{coluna}:Q", title=titulo),
        tooltip=["tempo:T", alt.Tooltip(f"{coluna}:Q", format=".1f")],
    )
    if barras:
        camadas = [base.mark_bar(opacity=0.85, color=cor)]
    else:
        camadas = [base.mark_area(opacity=0.25, color=cor, line={"color": cor, "strokeWidth": 2}, interpolate="monotone")]
    if linha_zero:
        camadas.append(alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#8895a7", strokeDash=[2, 3]).encode(y="y:Q"))
    if tempo_sel is not None:
        camadas.append(
            alt.Chart(pd.DataFrame({"tempo": [tempo_sel]})).mark_rule(color="#e0b400", strokeDash=[4, 3], strokeWidth=1.5).encode(x="tempo:T")
        )
    return _tema_grafico(alt.layer(*camadas).properties(height=150, width="container"))


def _graficos_extras(df: pd.DataFrame, tempo_sel: Optional[pd.Timestamp] = None, formato_eixo: str = "%Hh") -> None:
    """Gráficos de precipitação, rajada, gradiente 850–500 hPa e nível de 0 °C (só os que têm dados)."""
    definicoes = [
        ("Precipitação", "Precipitação (mm/h)", "#38bdf8", False, True),
        ("Rajada", "Rajada de vento (km/h)", "#f472b6", False, False),
        ("Gradiente", "T850 − T500 (°C)", "#fb923c", False, False),
        ("Nível 0 °C", "Nível de 0 °C (m)", "#94a3b8", False, False),
    ]
    disponiveis = [d for d in definicoes if d[0] in df and df[d[0]].notna().any()]
    for inicio in range(0, len(disponiveis), 2):
        colunas = st.columns(2)
        for coluna_ui, (campo, titulo, cor, zero, barras) in zip(colunas, disponiveis[inicio : inicio + 2]):
            coluna_ui.altair_chart(
                _grafico_variavel(df, campo, titulo, cor, tempo_sel, linha_zero=zero, barras=barras, formato_eixo=formato_eixo),
                theme=None,
            )


def _tabela_horaria(nome: str, ctx: dict[str, Any]) -> pd.DataFrame:
    """24 leituras a partir da hora selecionada."""
    from risco_raio import classificar_risco

    serie = ctx["dados"].get(nome, {})
    inicio, tempos = serie.get("idx_atual", 0), serie.get("tempos", [])
    info = ctx["series"][nome]
    desloc = ctx["deslocamento"]
    linhas: list[dict[str, Any]] = []
    for k in range(desloc, min(desloc + 24, len(info["rel"]))):
        i = inicio + k
        score = info["rel"][k]
        nivel = "Sem dados" if score is None else classificar_risco(score)[0]
        try:
            horario = horario_local(tempos[i]).strftime("%H:%M (%d/%m)")
        except (ValueError, IndexError):
            horario = "—"
        if k == desloc:
            horario += "  ◀ agora" if desloc == 0 else "  ◀ selecionada"
        linha = {
            "Horário": horario,
            "CAPE (J/kg)": serie["cape"][i] if i < len(serie.get("cape", [])) else None,
            "Lifted Index": serie["li"][i] if i < len(serie.get("li", [])) else None,
            "CIN (J/kg)": serie["cin"][i] if i < len(serie.get("cin", [])) else None,
            "Score": score,
            "Risco": nivel,
        }
        linhas.append(linha)
    return pd.DataFrame(linhas)


def _html_explicacao(ex: dict[str, Any]) -> str:
    """Tabela (HTML em uma linha por bloco) com a contribuição de cada componente do score."""
    if ex["score"] is None:
        if ex.get("sem_chuva"):
            return "<div class='nota-tab'>O modelo selecionado não entregou precipitação para esta hora: sem ela o produto CAPE × chuva não pode ser calculado.</div>"
        return "<div class='nota-tab'>Sem dados de CAPE para esta hora.</div>"
    if ex.get("metodo") == "capexp":
        fc = ex["fator_cape_regiao"]
        cape_txt = f"{_numero(ex['cape_ef'])} J/kg" + (f" (ajustado por região ×{fc:.2f})" if abs(fc - 1) > 1e-9 else "")
        corpo = (
            f"<tr><td>CAPE</td><td>{escape(cape_txt)}</td><td></td></tr>"
            f"<tr><td>Chuva prevista</td><td>{ex['chuva']:.1f} mm/h nesta hora</td><td></td></tr>"
            f"<tr><td>CAPE × chuva</td><td>produto (Romps et al., 2014)</td><td>{ex['cape_x_chuva']:.0f}</td></tr>"
        )
        if abs(ex.get("fator_produto", 1.0) - 1.0) > 1e-9:
            corpo += (
                "<tr><td>Correção litorânea</td><td>capital litorânea: o CAPE × chuva superestima as descargas sobre o mar "
                f"(Romps et al., 2018); fator provisório</td><td>×{ex['fator_produto']:.2f} → {ex['produto_corrigido']:.0f}</td></tr>"
            )
        corpo += f"<tr><td><b>Score</b></td><td>escala logarítmica de 0 a 100 (limiares provisórios)</td><td>{ex['score']:.1f}</td></tr>"
        return f"<table class='expl'>{corpo}</table>"

    def pts(valor: float) -> str:
        return f"{valor:+.0f}" if valor else "0"

    linhas = []
    fc = ex["fator_cape_regiao"]
    cape_txt = f"{_numero(ex['cape_ef'])} J/kg" + (f" (ajustado por região ×{fc:.2f})" if abs(fc - 1) > 1e-9 else "")
    linhas.append(("CAPE", cape_txt, ex["pts_cape"]))
    if ex["li_ef"] is not None:
        linhas.append(("Lifted Index", f"{ex['li_ef']:.1f} °C" + (" · limitado: sem energia (CAPE < 300)" if ex["sem_energia"] else ""), ex["pts_li"]))
    if ex.get("cin") is not None:
        nivel_cin = "inibição baixa (favorece o disparo)" if ex["pts_cin"] > 0 else ("inibição moderada" if ex["pts_cin"] == 0 else "inibição alta (reduz o risco)")
        linhas.append(("CIN", f"{abs(ex['cin']):.0f} J/kg · {nivel_cin}" + (" · bônus ignorado: sem energia (CAPE < 300)" if ex["sem_energia"] else ""), ex["pts_cin"]))
    if ex["pts_extras"]:
        linhas.append(("Variáveis extras", "chuva, rajada, gradiente, nível de 0 °C", ex["pts_extras"]))
    corpo = "".join(f"<tr><td>{n}</td><td>{escape(d)}</td><td>{pts(p)}</td></tr>" for n, d, p in linhas)
    corpo += f"<tr><td><b>Soma</b></td><td></td><td>{ex['subtotal']:.0f}</td></tr>"
    if ex["gate_configurado"]:
        if ex["gate_sem_dado"]:
            texto, valor = "Região com exigência de chuva prevista, mas sem dado de precipitação: ajuste não aplicado", "×1"
        elif ex["gate_aplicado"]:
            texto = (f"Sem chuva prevista até +{ex['janela_h']} h (máx. {ex['chuva_janela']:.1f} mm/h, abaixo de {ex['limiar']:.1f}): "
                     "score reduzido nesta região")
            valor = f"×{ex['multiplicador']:.2f}"
        else:
            texto = f"Chuva prevista até +{ex['janela_h']} h ({ex['chuva_janela']:.1f} mm/h): score mantido"
            valor = "×1"
        corpo += f"<tr><td>Chuva prevista</td><td>{escape(texto)}</td><td>{valor}</td></tr>"
    corpo += f"<tr><td><b>Score</b></td><td></td><td>{ex['score']:.1f}</td></tr>"
    return f"<table class='expl'>{corpo}</table>"


def abrir_detalhamento(nome: str, ctx: dict[str, Any]) -> None:
    """Abre a janela com cartões, gráficos e tabela horária da unidade."""

    @st.dialog(nome, width="large", dismissible=False)
    def janela() -> None:
        cores = ctx["cores"]
        modelo_nome, origem, frequencia = MODELOS[ctx["modelo_id"]]
        st.caption(f"Modelo: {modelo_nome} · Origem: {origem} · Atualização: {frequencia}")

        linha_t = ctx["tabela"].loc[ctx["tabela"]["Capital"] == nome]
        if linha_t.empty or not ctx["series"][nome]["rel"]:
            st.info("Não há série horária disponível para esta capital no modelo selecionado.")
        else:
            agora = linha_t.iloc[0]
            cor_agora = cores.get(agora["Risco"], cores["Sem dados"])
            score_agora = "—" if pd.isna(agora["Score"]) else f"{agora['Score']:.1f}"
            rotulo = "Risco agora" if ctx["deslocamento"] == 0 else f"Risco em {ctx['rotulo_hora']}"
            cartoes = [
                f"<div class='dlg-card' style='--cor:{cor_agora}'><span>{escape(rotulo)}</span><b>{escape(str(agora['Risco']))}</b><small>score {score_agora}</small></div>",
                f"<div class='dlg-card'><span>CAPE</span><b>{_numero(agora['CAPE (J/kg)'])}</b><small>J/kg</small></div>",
                f"<div class='dlg-card'><span>Lifted Index</span><b>{_numero(agora['Lifted Index (°C)'], 1)}</b><small>°C</small></div>",
                f"<div class='dlg-card'><span>CIN</span><b>{_numero(agora['CIN (J/kg)'])}</b><small>J/kg</small></div>",
            ]
            pico_txt = "—" if pd.isna(agora["Pico 24 h"]) else f"{agora['Pico 24 h']:.0f}"
            delta = agora["Δ 6 h"]
            delta_txt = "" if pd.isna(delta) or not delta else f" {delta:+.0f}"
            seta_txt = agora["Tendência"] or "—"
            hora_pico = escape(str(agora["Hora do pico"]))
            cartoes.append(
                f"<div class='dlg-card'><span>Tendência (6 h)</span><b>{seta_txt}{delta_txt}</b>"
                f"<small>pico 24 h: {pico_txt} · {hora_pico}</small></div>"
            )
            if ctx.get("extras"):
                extras_cartoes = [
                    ("Precipitação", _numero(agora["Precip. (mm/h)"], 1), "mm/h"),
                    ("Rajada", _numero(agora["Rajada (km/h)"]), "km/h"),
                    ("T850 − T500", _numero(agora["Gradiente 850–500 (°C)"], 1), "°C"),
                    ("Nível de 0 °C", _numero(agora["Nível 0 °C (m)"]), "m"),
                ]
                cartoes += [f"<div class='dlg-card'><span>{n}</span><b>{v}</b><small>{u}</small></div>" for n, v, u in extras_cartoes]
            st.markdown(f"<div class='dlg-resumo'>{''.join(cartoes)}</div>", unsafe_allow_html=True)

            st.markdown("#### Como o score foi calculado")
            st.markdown(_html_explicacao(explicar_hora(
                ctx["dados"].get(nome, {}), ctx["deslocamento"], str(agora["UF"]),
                ctx["parametros"], ctx["regioes"], ctx["gate"],
            )), unsafe_allow_html=True)

            df = _dados_grafico(nome, ctx)
            tempo_sel = df["tempo"].iloc[min(ctx["deslocamento"], len(df) - 1)] if not df.empty else pd.Timestamp.now()

            st.markdown("#### Score nas próximas 48 horas")
            st.caption("Linha branca: score usado no mapa. Faixas coloridas: níveis de risco. Linha tracejada: hora selecionada.")
            st.altair_chart(_grafico_score(df, cores, tempo_sel), theme=None)

            st.markdown("#### Variáveis")
            c1, c2, c3 = st.columns(3)
            c1.altair_chart(_grafico_variavel(df, "CAPE", "CAPE (J/kg)", "#e0761f", tempo_sel), theme=None)
            c2.altair_chart(_grafico_variavel(df, "LI", "Lifted Index (°C)", "#56b4e9", tempo_sel, linha_zero=True), theme=None)
            c3.altair_chart(_grafico_variavel(df, "CIN", "CIN (J/kg)", "#a78bfa", tempo_sel, linha_zero=True), theme=None)
            _graficos_extras(df, tempo_sel)

            st.markdown("#### Previsão horária — 24 horas a partir da hora selecionada")
            horario = _tabela_horaria(nome, ctx)

            def colorir_linha(linha: pd.Series) -> list[str]:
                cor = cores.get(horario.loc[linha.name, "Risco"], cores["Sem dados"])
                return [f"background-color: {cor}; color: {_cor_texto(cor)};"] * len(linha)

            st.dataframe(
                horario.style.apply(colorir_linha, axis=1),
                hide_index=True,
                width="stretch",
                height=360,
                column_config={
                    "CAPE (J/kg)": st.column_config.NumberColumn(format="%.0f"),
                    "Lifted Index": st.column_config.NumberColumn(format="%.1f"),
                    "CIN (J/kg)": st.column_config.NumberColumn(format="%.0f"),
                    "Score": st.column_config.NumberColumn(format="%.1f"),
                },
            )
        if st.button("Fechar", type="primary", width="stretch"):
            st.session_state["abrir_popup"] = False
            st.session_state["ultimo_clique_mapa"] = None
            # Componente novo (esquece o clique); o mapa reabre na mesma posição/zoom.
            recriar_mapa()
            st.rerun()

    janela()


def renderizar_estilo() -> None:
    """Aplica a paleta escura do painel; o mapa em si permanece claro."""
    st.markdown(
        """
        <style>
        .stApp { background: radial-gradient(1200px 520px at 15% -10%, #1d2430 0%, #14171c 55%, #101215 100%); color: #f2f4f8; }
        .block-container { max-width: none; padding: 1.1rem 1.6rem 1.6rem; }
        header[data-testid="stHeader"] { background: transparent; }

        [data-testid="stSidebar"] { background: #181c22; border-right: 1px solid #2b323c; }
        [data-testid="stSidebar"] > div:first-child { padding-top: .7rem; }
        [data-testid="stSidebar"] .stButton > button,
        [data-testid="stSidebar"] [data-testid^="stBaseButton-secondary"] {
            border: 1px solid #323a46 !important; background: #212731 !important; color: #f2f4f8 !important;
            border-radius: 9px; min-height: 2.25rem; justify-content: flex-start !important; text-align: left !important; transition: all .15s ease; }
        [data-testid="stSidebar"] .stButton > button *,
        [data-testid="stSidebar"] [data-testid^="stBaseButton-secondary"] * { color: #f2f4f8 !important; text-align: left !important; justify-content: flex-start !important; }
        [data-testid="stSidebar"] .stButton > button:hover,
        [data-testid="stSidebar"] [data-testid^="stBaseButton-secondary"]:hover { border-color: #e0b400 !important; background: #2b323d !important; transform: translateX(2px); }
        [data-testid="stSidebar"] [data-baseweb="select"] > div { background: #212731; border-color: #323a46; color: #f2f4f8; border-radius: 9px; }
        [data-testid="stSidebar"] input { background: #212731 !important; color: #f2f4f8 !important; border-color: #323a46 !important; }
        .logo-cartao { background:#ffffff; border-radius:12px; padding:.45rem .7rem; display:inline-flex; align-items:center;
            box-shadow:0 4px 14px rgba(0,0,0,.28); }
        .logo-cartao img { display:block; height:auto; }
        .logo-cartao img.logo-lateral { width:100%; max-width:210px; height:auto; }
        .logo-cartao img.logo-topo { height:54px; width:auto; max-width:100%; }
        /* barras de rolagem mais grossas (barra lateral e página) */
        [data-testid="stSidebar"], [data-testid="stSidebar"] *, [data-testid="stMain"], [data-testid="stAppViewContainer"] { scrollbar-width: auto !important; }
        [data-testid="stSidebar"] *::-webkit-scrollbar, [data-testid="stMain"]::-webkit-scrollbar, [data-testid="stAppViewContainer"] *::-webkit-scrollbar { width: 16px; height: 16px; }
        [data-testid="stSidebar"] *::-webkit-scrollbar-track, [data-testid="stMain"]::-webkit-scrollbar-track, [data-testid="stAppViewContainer"] *::-webkit-scrollbar-track { background: #14171c; }
        [data-testid="stSidebar"] *::-webkit-scrollbar-thumb, [data-testid="stMain"]::-webkit-scrollbar-thumb, [data-testid="stAppViewContainer"] *::-webkit-scrollbar-thumb { background: #566274; border-radius: 10px; border: 3px solid #14171c; min-height: 48px; }
        [data-testid="stSidebar"] *::-webkit-scrollbar-thumb:hover, [data-testid="stMain"]::-webkit-scrollbar-thumb:hover, [data-testid="stAppViewContainer"] *::-webkit-scrollbar-thumb:hover { background: #7a889b; }
        @supports (-moz-appearance: none) { [data-testid="stSidebar"], [data-testid="stSidebar"] *, [data-testid="stMain"], [data-testid="stAppViewContainer"] { scrollbar-color: #566274 #14171c !important; } }
        .creditos-card { position:relative; text-align:center; margin:1.05rem 0 1.15rem; padding:.85rem .6rem .8rem; overflow:hidden;
            background:radial-gradient(220px 90px at 50% 0%, rgba(47,123,255,.20) 0%, rgba(47,123,255,0) 75%), linear-gradient(160deg,#232b38 0%,#1b2029 100%);
            border:1px solid #38424f; border-radius:14px; box-shadow:0 6px 18px rgba(0,0,0,.30); }
        .creditos-card::before { content:""; position:absolute; left:0; right:0; top:0; height:3px;
            background:linear-gradient(90deg,#2f7bff 0%,#2ecc40 38%,#ffd400 62%,#ff8a00 82%,#e11d1d 100%); opacity:.9; }
        .creditos-card .cr-titulo { display:flex; align-items:center; gap:.5rem; margin:.1rem .2rem .6rem; font-size:.62rem; font-weight:700;
            letter-spacing:.16em; text-transform:uppercase; color:#9fb0c6; }
        .creditos-card .cr-titulo::before, .creditos-card .cr-titulo::after { content:""; flex:1; height:1px; background:linear-gradient(90deg,transparent,#4a5668); }
        .creditos-card .cr-titulo::after { transform:scaleX(-1); }
        .creditos-card .cr-pessoa { display:flex; flex-direction:column; align-items:center; gap:.12rem; padding:.05rem 0; }
        .creditos-card .cr-nome { font-size:.98rem; font-weight:750; color:#ffffff; letter-spacing:.01em; line-height:1.2; }
        .creditos-card .cr-email { font-size:.71rem; color:#7fb0ff !important; text-decoration:none !important; overflow-wrap:anywhere; }
        .creditos-card .cr-email:hover { color:#a9ccff !important; text-decoration:underline !important; }
        .creditos-card .cr-sep { width:2.2rem; height:2px; margin:.55rem auto; border-radius:2px; background:linear-gradient(90deg,#2f7bff,#2ecc40); opacity:.8; }
        .sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
        .marca-lateral { display:flex; align-items:center; gap:.6rem; margin-bottom:.1rem; }
        .marca-lateral .raio { width:2.1rem; height:2.1rem; border-radius:10px; display:grid; place-items:center; font-size:1.15rem; background:linear-gradient(135deg,#f5c518,#e0761f); box-shadow:0 4px 14px rgba(224,118,31,.35); }
        .marca-lateral b { font-size:1.25rem; letter-spacing:.03em; }

        .pdea-cabecalho { position:relative; display:flex; flex-wrap:wrap; align-items:center; gap:.9rem 1.4rem; padding:.85rem 1.2rem .85rem 1rem; margin-bottom:1rem;
            background:radial-gradient(700px 160px at 0% 0%, rgba(47,123,255,.16) 0%, rgba(47,123,255,0) 70%), linear-gradient(120deg,#222a36 0%,#1b2029 60%,#171b22 100%);
            border:1px solid #333c49; border-radius:16px; box-shadow:0 10px 30px rgba(0,0,0,.30); overflow:hidden; }
        .pdea-cabecalho::before { content:""; position:absolute; left:0; right:0; top:0; height:3px;
            background:linear-gradient(90deg,#2f7bff 0%,#2ecc40 38%,#ffd400 62%,#ff8a00 82%,#e11d1d 100%); opacity:.9; }
        .pdea-cabecalho .logo-cartao { padding:.4rem .8rem; border-radius:12px; box-shadow:0 6px 18px rgba(0,0,0,.35); }
        .pdea-pills { margin-left:auto; display:flex; flex-wrap:wrap; gap:.6rem; align-items:stretch; }
        .pill { display:flex; flex-direction:column; justify-content:center; gap:.12rem; min-width:7.5rem; padding:.45rem .95rem;
            background:rgba(255,255,255,.045); border:1px solid #3a4452; border-radius:12px; }
        .pill .pl { font-size:.62rem; letter-spacing:.09em; text-transform:uppercase; color:#8f9bad; }
        .pill b { font-size:.9rem; color:#ffffff; font-weight:650; white-space:nowrap; }
        .pill .ponto { display:inline-block; width:.55rem; height:.55rem; border-radius:50%; margin-right:.4rem; vertical-align:.04rem; background:#2ecc40;
            box-shadow:0 0 0 0 rgba(46,204,64,.55); animation:pdea-pulso 2.2s ease-out infinite; }
        .pill .ponto.velho { background:#ff8a00; box-shadow:none; animation:none; }
        @keyframes pdea-pulso { 0% { box-shadow:0 0 0 0 rgba(46,204,64,.55); } 70% { box-shadow:0 0 0 .5rem rgba(46,204,64,0); } 100% { box-shadow:0 0 0 0 rgba(46,204,64,0); } }

        .kpi { background:#1c222b; border:1px solid #333c49; border-top:3px solid var(--cor); border-radius:12px; padding:.7rem .9rem; box-shadow:0 4px 14px rgba(0,0,0,.2); }
        .kpi .kpi-v { font-size:1.9rem; font-weight:800; line-height:1.1; color:#fff; }
        .kpi .kpi-r { font-size:.78rem; letter-spacing:.07em; text-transform:uppercase; color:var(--cor); font-weight:700; }

        .destaque { margin:.8rem 0 .7rem; padding:.65rem 1rem; background:#1c222b; border:1px solid #333c49; border-left:4px solid var(--cor); border-radius:10px; color:#c9d1db; font-size:.88rem; }
        .destaque b { color:#fff; }
        .secao { margin:.2rem 0 .5rem; font-size:1.05rem; font-weight:700; letter-spacing:.02em; }

        .stDataFrame { border: 1px solid #353d4a; border-radius: 8px; overflow: hidden; }
        iframe[title*="folium"], [data-testid="stCustomComponentV1"] iframe { border:1px solid #3a4452; border-radius:14px; box-shadow:0 10px 30px rgba(0,0,0,.35); background:#e9edf2; }

        [data-testid="stDialog"] > div { background: #1c2026 !important; color: #f2f4f8 !important; border: 1px solid #3b4350; border-radius:14px; }
        [data-testid="stDialog"] h1, [data-testid="stDialog"] h2, [data-testid="stDialog"] h3, [data-testid="stDialog"] h4, [data-testid="stDialog"] p, [data-testid="stDialog"] [data-testid="stCaptionContainer"] { color: #f2f4f8 !important; }
        [data-testid="stDialog"] .stButton > button { background: #e0b400; border-color: #e0b400; color: #141619; font-weight: 700; text-align: center; border-radius:9px; }
        [data-testid="stDialog"] .stButton > button:hover { background: #f0c52b; border-color: #f0c52b; color: #141619; }
        .dlg-resumo { display:grid; grid-template-columns:repeat(4,1fr); gap:.6rem; margin:.3rem 0 .9rem; }
        .dlg-card { background:#242a33; border:1px solid #38414e; border-top:3px solid var(--cor,#5a6472); border-radius:10px; padding:.5rem .75rem; display:flex; flex-direction:column; }
        .dlg-card span { font-size:.7rem; letter-spacing:.07em; text-transform:uppercase; color:#aab3c0; }
        .dlg-card b { font-size:1.35rem; line-height:1.2; color:#fff; }
        .dlg-card small { color:#aab3c0; }

        @media (max-width: 760px) {
            .block-container { padding: .9rem .75rem; }
            .pdea-pills { margin-left:0; width:100%; }
            .pill { flex:1 1 9rem; }
            .dlg-resumo { grid-template-columns:repeat(2,1fr); }
        }
        .dlg-resumo { grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)) !important; }
        [data-testid="stSidebar"] [data-testid="stExpander"] { border:1px solid #2b323c; border-radius:10px; background:#1b2027; }
        [data-testid="stSidebar"] [data-testid="stExpander"] summary { font-weight:600; }
        [data-testid="stMain"] [data-testid="stSlider"] { margin-top:.5rem; }
        .expl { width:100%; border-collapse:collapse; font-size:.85rem; margin:.1rem 0 .8rem; }
        .expl td { padding:.28rem .55rem; border-bottom:1px solid #2b323c; color:#d5dbe4; }
        .expl td:first-child { white-space:nowrap; color:#a9b3c1; }
        .expl td:last-child { text-align:right; font-weight:700; color:#fff; white-space:nowrap; }
        .nota-tab { color:#a9b3c1; font-size:.82rem; margin:.1rem 0 .5rem; }
        .rodape-sec { margin-top:.4rem; color:#8895a7; font-size:.78rem; }
        @media (max-width: 760px) {
            .kpi .kpi-v { font-size:1.45rem; }
            .logo-cartao img.logo-topo { height:44px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def serie_longa(dados: dict[str, Any], series: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Série horária de todas as capitais (formato longo), para análise/calibração externa."""
    linhas: list[dict[str, Any]] = []
    for estacao in ESTACOES:
        nome = estacao["nome"]
        serie = dados.get(nome, {})
        inicio, tempos = serie.get("idx_atual", 0), serie.get("tempos", [])
        info = series[nome]
        for k, score in enumerate(info["rel"][: HORIZONTE_MAX_H + 1]):
            i = inicio + k
            if i >= len(tempos):
                break
            linha = {
                "Capital": nome,
                "UF": estacao["uf"],
                "Horário local": tempos[i],
                "Horas à frente": k,
                "CAPE (J/kg)": serie["cape"][i] if i < len(serie.get("cape", [])) else None,
                "Lifted Index (°C)": serie["li"][i] if i < len(serie.get("li", [])) else None,
                "CIN (J/kg)": serie["cin"][i] if i < len(serie.get("cin", [])) else None,
                "Score": score,
            }
            for campo, rotulo in (("precip", "Precip. (mm/h)"), ("rajada", "Rajada (km/h)"), ("nivel0", "Nível 0 °C (m)"),
                                  ("t850", "T850 (°C)"), ("t500", "T500 (°C)")):
                if campo in serie:
                    linha[rotulo] = serie[campo][i] if i < len(serie[campo]) else None
            linhas.append(linha)
    return pd.DataFrame(linhas)


def _csv_br(df: pd.DataFrame) -> bytes:
    """CSV que abre corretamente no Excel em português (; e vírgula decimal, UTF-8 com BOM)."""
    df = df.copy()
    for coluna, casas in (("CAPE (J/kg)", 0), ("Lifted Index (°C)", 1), ("CIN (J/kg)", 0)):
        if coluna in df:
            df[coluna] = df[coluna].round(casas)
    return df.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")


def painel_historico(
    modelo_id: str,
    parametros: ParametrosRisco,
    regioes: dict[str, tuple[float, float]],
    unidades: list[str],
    cores: dict[str, str],
) -> None:
    """Score realizado e evolução das previsões, a partir do SQLite local."""
    info = historico.status()
    if not info["ativo"]:
        st.info("O histórico está desligado (variável PDEA_HISTORICO=desligado).")
        return
    if info["execucoes"] == 0:
        st.info(
            "Ainda não há nada gravado. O painel grava uma execução por modelo a cada hora em que é aberto; "
            "para gravar sem ninguém abrir o painel, agende `python historico.py registrar` (veja o README)."
        )
        return

    linhas_txt = f"{info['linhas']:,}".replace(",", ".")
    mb_txt = f"{info['tamanho_mb']}".replace(".", ",")
    n = info["execucoes"]
    st.markdown(
        f"<div class='nota-tab'>{n} {'execução gravada' if n == 1 else 'execuções gravadas'} ({linhas_txt} linhas, {mb_txt} MB), "
        f"de {info['primeira']} a {info['ultima']} · modelos: {', '.join(info['modelos'])}. "
        f"Arquivo: <code>{escape(str(info['caminho']))}</code></div>",
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns([3, 1])
    unidade = c1.selectbox("Capital", unidades, key="hist_unidade")
    dias = c2.selectbox("Período", [1, 3, 7, 14, 30], index=2, key="hist_dias", format_func=lambda d: f"{d} dia(s)")

    realizado = historico.serie_realizada(unidade, modelo_id, dias, parametros, regioes)
    st.markdown("##### Score realizado (previsão de 0 h de cada execução)")
    if realizado.empty:
        st.caption("Sem registros deste modelo para a unidade e o período escolhidos.")
    else:
        limites = [0] + [min(limite, 100) for limite, _, _ in NIVEIS_RISCO]
        bandas = pd.DataFrame([{"y0": limites[i], "y1": limites[i + 1], "Nível": r} for i, (_, r, _) in enumerate(NIVEIS_RISCO)])
        faixa = (
            alt.Chart(bandas).mark_rect(opacity=0.17)
            .encode(
                y=alt.Y("y0:Q", scale=alt.Scale(domain=[0, 100]), title="Score"), y2="y1:Q",
                color=alt.Color("Nível:N", scale=alt.Scale(domain=ROTULOS, range=[cores[r] for r in ROTULOS]), legend=None),
            )
        )
        linha = (
            alt.Chart(realizado).mark_line(point=True, strokeWidth=2.5, color="#f2f4f8", interpolate="monotone")
            .encode(x=alt.X("tempo:T", title=None, axis=alt.Axis(format="%d/%m %Hh", labelAngle=0, tickCount=6)),
                    y=alt.Y("score:Q", scale=alt.Scale(domain=[0, 100])),
                    tooltip=["tempo:T", alt.Tooltip("score:Q", format=".1f"), alt.Tooltip("cape:Q", title="CAPE", format=".0f"),
                             alt.Tooltip("li:Q", title="LI", format=".1f")])
        )
        st.altair_chart(_tema_grafico(alt.layer(faixa, linha).properties(height=220, width="container")), theme=None)

        # Demais variáveis no mesmo período (mesmos gráficos do detalhe da unidade).
        st.markdown("##### Variáveis no período")
        dfv = realizado.rename(columns={"cape": "CAPE", "li": "LI", "cin": "CIN", "precip": "Precipitação", "rajada": "Rajada", "nivel0": "Nível 0 °C"})
        dfv["Gradiente"] = dfv["t850"] - dfv["t500"]
        # Rótulo do eixo conforme a duração do histórico: com menos de 3 dias mostra também a hora.
        duracao = (dfv["tempo"].max() - dfv["tempo"].min()) if len(dfv) else pd.Timedelta(0)
        formato = "%d/%m %Hh" if duracao <= pd.Timedelta(days=3) else "%d/%m"
        v1, v2, v3 = st.columns(3)
        v1.altair_chart(_grafico_variavel(dfv, "CAPE", "CAPE (J/kg)", "#e0761f", formato_eixo=formato), theme=None)
        v2.altair_chart(_grafico_variavel(dfv, "LI", "Lifted Index (°C)", "#56b4e9", linha_zero=True, formato_eixo=formato), theme=None)
        v3.altair_chart(_grafico_variavel(dfv, "CIN", "CIN (J/kg)", "#a78bfa", linha_zero=True, formato_eixo=formato), theme=None)
        _graficos_extras(dfv, formato_eixo=formato)

    validos = historico.horarios_com_revisoes(unidade, modelo_id, dias)
    st.markdown("##### Como a previsão para um horário mudou entre as execuções")
    if not validos:
        st.caption("Ainda não há horários com 2 ou mais execuções gravadas para esta capital.")
    else:
        valido = st.selectbox("Horário previsto", validos, key="hist_valido", format_func=lambda v: pd.to_datetime(v).strftime("%d/%m %H:%M"))
        evolucao = historico.evolucao_previsao(unidade, modelo_id, valido, parametros, regioes)
        grafico = (
            alt.Chart(evolucao).mark_line(point=True, strokeWidth=2.5, color="#e0b400")
            .encode(x=alt.X("execucao_t:T", title="Execução do modelo", axis=alt.Axis(format="%d/%m %Hh", labelAngle=0)),
                    y=alt.Y("score:Q", scale=alt.Scale(domain=[0, 100]), title="Score previsto"),
                    tooltip=[alt.Tooltip("execucao_t:T", title="Execução"), alt.Tooltip("horas:Q", title="Horas à frente"),
                             alt.Tooltip("score:Q", format=".1f")])
        )
        st.altair_chart(_tema_grafico(grafico.properties(height=200, width="container")), theme=None)

    buffer = io.StringIO()
    historico.exportar_csv(buffer, modelo_id, parametros, regioes, dias=dias)
    st.download_button(
        "Baixar histórico deste modelo (CSV)",
        buffer.getvalue().encode("utf-8-sig"),
        file_name=f"pdea_historico_{modelo_id}.csv",
        mime="text/csv",
        on_click="ignore",
        help="Variáveis brutas e score de todas as capitais e execuções do período, para calibrar com observações.",
    )


@st.fragment
def secao_mapa(tabela: pd.DataFrame, ctx: dict[str, Any], estilo: str, mostrar_score: bool) -> None:
    """Mapa + clique + janela de detalhe. Como fragmento, clicar em um marcador não recarrega o resto da página."""
    mapa = criar_mapa(
        tabela,
        estilo,
        ctx["cores"],
        mostrar_score,
        ctx["rotulo_hora"],
        ctx["fonte"],
        ctx["divisas"],
        ctx["goes"],
        ctx["goes_opacidade"],
        ctx["glm"],
        ctx["alcance"],
    )
    resultado_mapa = st_folium(
        mapa,
        height=ALTURA_MAPA,
        use_container_width=True,
        key=f"mapa_{ctx['modelo_id']}_{estilo}_{st.session_state['versao_mapa']}",
        returned_objects=["last_object_clicked_tooltip"],
    )

    clique = resultado_mapa.get("last_object_clicked_tooltip") if resultado_mapa else None
    if clique and clique in set(tabela["Capital"]) and clique != st.session_state["ultimo_clique_mapa"]:
        st.session_state["ultimo_clique_mapa"] = clique
        selecionar_estacao(clique)

    if st.session_state["abrir_popup"] and st.session_state["estacao_popup"]:
        abrir_detalhamento(st.session_state["estacao_popup"], ctx)


@st.cache_resource
def _iniciar_coleta_glm() -> bool:
    """Liga, uma única vez por processo do servidor, a coleta dos raios do GLM (a cada 5 min)."""
    if glm_ao_vivo is None:
        return False
    glm_ao_vivo.obter_atualizador(iniciar=True)
    return True


def main() -> None:
    _inicializar_estado()
    renderizar_estilo()
    _iniciar_coleta_glm()  # a coleta dos raios começa assim que o app sobe: os raios recentes já estarão prontos

    # ------------------------------------------------------------------ barra lateral (controles)
    with st.sidebar:
        st.markdown(
            f"<div class='logo-cartao'>{_html_logo('logo-lateral')}</div>",
            unsafe_allow_html=True,
        )
        pessoas = "<div class='cr-sep'></div>".join(
            f"<div class='cr-pessoa'><span class='cr-nome'>{escape(nome)}</span>"
            f"<a class='cr-email' href='mailto:{escape(email)}'>{escape(email)}</a></div>"
            for nome, email in RESPONSAVEIS
        )
        st.markdown(
            f"<div class='creditos-card' title='Versão {escape(VERSAO_APP)}'><div class='cr-titulo'>Elaborado por</div>{pessoas}</div>",
            unsafe_allow_html=True,
        )

        with st.expander("Dados", expanded=True):
            modelo_id = st.selectbox(
                "Modelo numérico",
                options=list(MODELOS),
                format_func=lambda chave: MODELOS[chave][0],
            )
            nome_modelo, origem, frequencia = MODELOS[modelo_id]
            st.caption(f"Origem: {origem} · Atualiza: {frequencia}")
            if st.button("Atualizar dados de todos os modelos", width="stretch"):
                limpar_cache_dados()
                st.session_state["ultimo_clique_mapa"] = None
                recriar_mapa()

        with st.expander("Mapa"):
            estilo = st.selectbox("Estilo do mapa", options=list(TILES), index=0)
            mostrar_score = st.toggle("Mostrar o score dentro das bolinhas", value=True)
            divisas = st.toggle("Divisas estaduais", value=True)
            goes = st.toggle("Topos de nuvem (GOES-East)", help="Infravermelho do GOES-East (NASA GIBS), atualizado a cada ~10 min com atraso de cerca de 30 min. Requer internet no navegador.")
            goes_opacidade = st.slider("Opacidade das nuvens", 0.2, 0.9, 0.6, step=0.05) if goes else 0.6
            alcance = st.toggle(
                "Alcance ao redor das capitais (30, 50 e 100 km)", value=True,
                help="Anéis tracejados de 30 km (verde), 50 km (amarelo) e 100 km (vermelho) em volta de cada capital: os mesmos raios usados "
                     "para verificar o escore contra os raios observados pelo GLM.",
            )
            glm = False
            if glm_ao_vivo is not None:
                glm = st.toggle(
                    "Raios em tempo real (GLM)", value=True,
                    help="Flashes observados pelo GLM do GOES-East nos últimos 20 min (vermelho = mais recentes, depois laranja, amarelo e verde; somem após 20 min). Atualiza sozinho a cada 5 min, sem recarregar a página. "
                         "O GLM mede a atividade elétrica total (intranuvem e nuvem-solo), com atraso de alguns minutos. É observação, não previsão.",
                )


    if modelo_id != st.session_state["modelo_anterior"]:
        st.session_state["modelo_anterior"] = modelo_id
        st.session_state["ultimo_clique_mapa"] = None
        st.session_state["abrir_popup"] = False
        recriar_mapa()

    cores = CORES_NIVEL
    parametros = ParametrosRisco()  # regra original (sem ajustes de sensibilidade)
    regioes = carregar_regioes()  # fatores de CAPE/LI por UF (config_regioes.json; neutros por padrão)
    gate = carregar_gate()  # exigência de chuva prevista nas UFs do Nordeste (config_regioes.json)

    # ------------------------------------------------------------------ dados
    try:
        with st.spinner(f"Buscando {nome_modelo} para {len(ESTACOES)} capitais..."):
            dados, aviso = carregar_dados(modelo_id)
    except ErroBuscaModelo as erro:
        st.error(f"Falha ao buscar o modelo selecionado: {erro}")
        st.stop()
    except Exception as erro:
        st.error(f"Ocorreu um erro inesperado ao carregar o painel: {erro}")
        st.stop()
    if aviso:
        st.warning(aviso)
    elif historico.caminho_do_historico() is not None:
        try:  # o histórico nunca pode derrubar o painel
            historico.registrar(dados, modelo_id)
        except Exception as erro:
            st.sidebar.caption(f"Histórico indisponível: {erro}")
    extras_disponiveis = bool(dados.get("_extras"))
    if not extras_disponiveis:
        st.sidebar.warning(
            "Sem dados de precipitação deste modelo: o escore usa CAPE × chuva e, sem chuva, não pode ser calculado "
            f"({dados.get('_erro_extras') or 'sem detalhe'}). Tente outro modelo."
        )

    fonte = nome_modelo

    # ------------------------------------------------------------------ áreas da página (ordem visual)
    area_cabecalho = st.container()
    area_kpi = st.container()
    area_hora = st.container()
    area_destaque = st.container()

    limite_h = min(HORIZONTE_MAX_H, horas_a_frente(dados))
    with area_hora:
        if limite_h > 0:
            st.session_state["deslocamento"] = min(st.session_state.get("deslocamento", 0), limite_h)
            deslocamento = st.slider(
                "Hora da previsão exibida no mapa (horas a partir de agora)",
                0,
                limite_h,
                key="deslocamento",
                format="+%d h",
            )
        else:
            deslocamento = 0
    rotulo_hora = rotulo_horario(dados, deslocamento)

    # ------------------------------------------------------------------ cálculo
    series = series_por_unidade(dados, parametros, regioes, gate)
    tabela = consolidar(dados, series, deslocamento)
    niveis = list(reversed(ROTULOS))
    contagens = tabela["Risco"].value_counts()

    ctx = {
        "dados": dados,
        "series": series,
        "tabela": tabela,
        "cores": cores,
        "deslocamento": deslocamento,
        "rotulo_hora": rotulo_hora,
        "modelo_id": modelo_id,
        "fonte": fonte,
        "divisas": divisas,
        "goes": goes,
        "goes_opacidade": goes_opacidade,
        "glm": glm,
        "alcance": alcance,
        "extras": extras_disponiveis,
        "gate": gate,
        "regioes": regioes,
        "parametros": parametros,
    }

    # ------------------------------------------------------------------ barra lateral (lista de unidades)
    with st.sidebar:
        st.divider()
        st.caption(f"CAPITAIS ({len(ESTACOES)})")
        termo = st.text_input("Pesquisar capital", placeholder="Pesquisar capital...", label_visibility="collapsed")
        ordem = st.radio("Ordenar por", ["Nome", "Maior risco"], horizontal=True, label_visibility="collapsed")
        lista = tabela[tabela["Capital"].str.casefold().str.contains(termo.casefold().strip(), na=False, regex=False)]
        if ordem == "Maior risco":
            lista = lista.sort_values(["Score", "Capital"], ascending=[False, True], na_position="last", kind="stable")
        if lista.empty:
            st.caption("Nenhuma capital encontrada.")
        for _, linha in lista.iterrows():
            score_txt = "—" if pd.isna(linha["Score"]) else f"{linha['Score']:.0f}"
            seta = f" {linha['Tendência']}" if linha["Tendência"] else ""
            if st.button(
                f"{ICONES.get(linha['Risco'], '⚫')} {score_txt}{seta} · {linha['Capital']}",
                key=f"unidade_{linha['Capital']}",
                width="stretch",
                help=f"{linha['Risco']} — abrir previsão horária",
            ):
                selecionar_estacao(linha["Capital"])

    # ------------------------------------------------------------------ cabeçalho, indicadores e destaque
    minutos = _minutos_desde(dados)
    sufixo_hora = " · agora" if deslocamento == 0 else f" · +{deslocamento} h"
    def _pilula(rotulo: str, valor_html: str, dica: str = "") -> str:
        titulo = f" title='{escape(dica)}'" if dica else ""
        return f"<div class='pill'{titulo}><span class='pl'>{escape(rotulo)}</span><b>{valor_html}</b></div>"

    pilulas = [
        _pilula("Modelo", escape(fonte)),
        _pilula("Hora exibida", f"{escape(rotulo_hora)}{escape(sufixo_hora)}"),
    ]
    if minutos is not None:
        estado_dados = "ponto" if minutos <= 15 else "ponto velho"
        pilulas.append(_pilula("Dados", f"<span class='{estado_dados}'></span>há {minutos} min",
                               "Idade da previsão do modelo (atualiza a cada 10 min)"))
    # HTML numa única linha por bloco: linhas em branco quebrariam o parser de Markdown.
    cabecalho_html = (
        f"<div class='pdea-cabecalho'><div class='logo-cartao'>{_html_logo('logo-topo')}</div>"
        "<h1 class='sr-only'>PDEA: Preditor de Descargas Elétricas Atmosféricas</h1>"
        f"<div class='pdea-pills'>{''.join(pilulas)}</div></div>"
    )
    with area_cabecalho:
        st.markdown(cabecalho_html, unsafe_allow_html=True)
    with area_kpi:
        for coluna, nivel in zip(st.columns(5), niveis):
            coluna.markdown(
                f"<div class='kpi' style='--cor:{cores[nivel]}'><div class='kpi-v'>{int(contagens.get(nivel, 0))}</div>"
                f"<div class='kpi-r'>{nivel}</div></div>",
                unsafe_allow_html=True,
            )

    com_score = tabela.dropna(subset=["Score"])
    with area_destaque:
        if com_score.empty:
            texto, cor_destaque = "Sem dados de risco disponíveis para o modelo selecionado.", cores["Sem dados"]
        else:
            topo = com_score.sort_values(["Score", "Capital"], ascending=[False, True]).iloc[0]
            cor_destaque = cores.get(topo["Risco"], cores["Sem dados"])
            texto = (
                f"Maior risco em {escape(rotulo_hora)}: <b>{escape(str(topo['Capital']))}</b> — "
                f"<span style='color:{cor_destaque}; font-weight:700;'>{escape(str(topo['Risco']))} · {topo['Score']:.1f}/100</span>."
            )
            subindo = com_score[com_score["Tendência"] == "▲"].sort_values("Δ 6 h", ascending=False)
            if not subindo.empty:
                lider = subindo.iloc[0]
                texto += (
                    f" <b>{len(subindo)}</b> capital(is) com tendência de alta nas próximas 6 h "
                    f"(maior alta: <b>{escape(str(lider['Capital']))}</b>, {lider['Δ 6 h']:+.0f})."
                )
        sem_dados = int(contagens.get("Sem dados", 0))
        if sem_dados:
            texto += f" ({sem_dados} capital(is) sem dados.)"
        st.markdown(f"<div class='destaque' style='--cor:{cor_destaque}'>{texto}</div>", unsafe_allow_html=True)
        st.markdown("<div class='secao'>Mapa das capitais</div>", unsafe_allow_html=True)

    # ------------------------------------------------------------------ mapa (fragmento)
    secao_mapa(tabela, ctx, estilo, mostrar_score)

    # ------------------------------------------------------------------ tabela e exportação
    with st.expander("Tabela e exportação"):
        aba_tabela, aba_exportar = st.tabs(["Tabela", "Exportar"])
        with aba_tabela:
            st.markdown(
                f"<div class='nota-tab'>Leitura em {escape(rotulo_hora)} · modelo: {escape(fonte)}. "
                "Δ 6 h = maior variação do score nas próximas 6 h.</div>",
                unsafe_allow_html=True,
            )
            colunas = ["Capital", "UF", "Risco", "Score", "Tendência", "Δ 6 h", "Pico 24 h", "Hora do pico",
                       "CAPE (J/kg)", "Lifted Index (°C)", "CIN (J/kg)"]
            if extras_disponiveis:
                colunas += ["Ajuste chuva", "Precip. (mm/h)", "Rajada (km/h)", "Gradiente 850–500 (°C)", "Nível 0 °C (m)"]
            exibicao = tabela[colunas].sort_values("Score", ascending=False, na_position="last")

            def colorir_risco(valor: str) -> str:
                cor = cores.get(valor, cores["Sem dados"])
                return f"background-color: {cor}; color: {_cor_texto(cor)}; font-weight:700;"

            st.dataframe(
                exibicao.style.map(colorir_risco, subset=["Risco"]),
                hide_index=True,
                width="stretch",
                height=420,
                column_config={
                    "Score": st.column_config.NumberColumn(format="%.1f"),
                    "Δ 6 h": st.column_config.NumberColumn(format="%+.0f"),
                    "Pico 24 h": st.column_config.NumberColumn(format="%.0f"),
                    "CAPE (J/kg)": st.column_config.NumberColumn(format="%.0f"),
                    "Lifted Index (°C)": st.column_config.NumberColumn(format="%.1f"),
                    "CIN (J/kg)": st.column_config.NumberColumn(format="%.0f"),
                    "Precip. (mm/h)": st.column_config.NumberColumn(format="%.1f"),
                    "Rajada (km/h)": st.column_config.NumberColumn(format="%.0f"),
                    "Gradiente 850–500 (°C)": st.column_config.NumberColumn(format="%.1f"),
                    "Nível 0 °C (m)": st.column_config.NumberColumn(format="%.0f"),
                },
            )
        with aba_exportar:
            carimbo = datetime.now(TZ_BRASILIA).strftime("%Y%m%d_%H%M")
            c1, c2 = st.columns(2)
            c1.download_button(
                "Baixar tabela atual (CSV)",
                _csv_br(tabela.drop(columns=["Latitude", "Longitude"]).assign(**{"Horário": rotulo_hora, "Modelo": fonte})),
                file_name=f"pdea_tabela_{carimbo}.csv",
                mime="text/csv",
                on_click="ignore",
                width="stretch",
            )
            c2.download_button(
                "Baixar séries horárias de 48 h (CSV)",
                _csv_br(serie_longa(dados, series)),
                file_name=f"pdea_series_{carimbo}.csv",
                mime="text/csv",
                on_click="ignore",
                width="stretch",
                help="CAPE, LI, CIN e score hora a hora de todas as capitais — útil para calibrar com observações.",
            )
            st.markdown("<div class='rodape-sec'>Mapa estático e relatório (mapa + ranking de todas as capitais):</div>", unsafe_allow_html=True)
            if st.button("Gerar mapa PNG e relatório PDF", width="stretch"):
                titulo = "PDEA — Risco de raios"
                subtitulo = f"{rotulo_hora} · modelo: {fonte} · gerado em {datetime.now(TZ_BRASILIA):%d/%m/%Y %H:%M}"
                with st.spinner("Gerando arquivos..."):
                    st.session_state["relatorio"] = {
                        "png": gerar_png(tabela, titulo, subtitulo),
                        "pdf": gerar_pdf(tabela, titulo, subtitulo),
                        "descricao": subtitulo,
                        "carimbo": carimbo,
                    }
            relatorio = st.session_state.get("relatorio")
            if relatorio:
                st.caption(f"Pronto: {relatorio['descricao']}")
                d1, d2 = st.columns(2)
                d1.download_button("Baixar mapa (PNG)", relatorio["png"], file_name=f"pdea_mapa_{relatorio['carimbo']}.png",
                                   mime="image/png", on_click="ignore", width="stretch")
                d2.download_button("Baixar relatório (PDF)", relatorio["pdf"], file_name=f"pdea_relatorio_{relatorio['carimbo']}.pdf",
                                   mime="application/pdf", on_click="ignore", width="stretch")

    with st.expander("Histórico das previsões"):
        painel_historico(modelo_id, parametros, regioes, [e["nome"] for e in ESTACOES], cores)

    with st.expander("Como interpretar o painel"):
        st.write(
            "O score estima o potencial de descargas em cada capital a partir do produto CAPE × taxa de chuva previstos pelo "
            "modelo numérico, um indicador da taxa de descargas descrito por Romps et al. (2014, 2018): sem energia "
            "(CAPE) ou sem chuva prevista, o score é baixo. O produto é convertido em uma escala de 0 a 100, e os limiares "
            "dos níveis ainda são provisórios, em calibração com observações do satélite GOES (GLM). O método foi validado "
            "sobre terra nos Estados Unidos e, globalmente, sobre continentes, mas não reproduz a menor atividade de descargas sobre "
            "o oceano; por isso as capitais litorâneas (Maceió, Salvador, Fortaleza, Vitória, São Luís, João Pessoa, Recife, "
            "Rio de Janeiro, Natal, Florianópolis e Aracaju) recebem uma correção provisória que reduz o produto. Não considera convecção com pouco CAPE (por "
            "exemplo, sistemas frontais). A seta indica a tendência do score nas próximas 6 h (▲ sobe, ▼ desce, ▬ estável). "
            "É uma previsão de modelo, não uma detecção: o painel não substitui alertas oficiais (Defesa Civil, INMET) nem "
            "sistemas de detecção de descargas atmosféricas. Em caso de trovoada, procure abrigo em local fechado."
        )
        st.caption(f"Última renderização local: {datetime.now(TZ_BRASILIA).strftime('%d/%m/%Y %H:%M:%S')} (America/Sao_Paulo).")

    if glm:
        canal_glm()  # entrega os raios novos ao mapa a cada coleta (5 min), sem recarregar o mapa nem a página


if __name__ == "__main__":
    main()
