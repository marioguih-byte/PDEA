"""Exportação do estado atual do painel: mapa estático (PNG) e relatório (PDF)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # sem interface gráfica (servidor)
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Polygon as PoligonoMpl  # noqa: E402

from risco_raio import CORES_NIVEL, ROTULOS  # noqa: E402
from unidades import sigla  # noqa: E402

CONTORNO = Path(__file__).resolve().parent / "dados" / "brasil_contorno.geojson"
FUNDO, TEXTO, SECUNDARIO = "#ffffff", "#1f2933", "#52606d"


def _poligonos() -> list[list[list[float]]]:
    """Anéis externos do contorno do Brasil (lon, lat)."""
    geometria = json.loads(CONTORNO.read_text(encoding="utf-8"))["geometry"]
    partes = geometria["coordinates"] if geometria["type"] == "MultiPolygon" else [geometria["coordinates"]]
    return [parte[0] for parte in partes]


def _texto_sobre(cor_hex: str) -> str:
    cor_hex = cor_hex.lstrip("#")
    r, g, b = (int(cor_hex[i : i + 2], 16) for i in (0, 2, 4))
    return "#111111" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


def _desenhar_mapa(ax: plt.Axes, tabela: pd.DataFrame) -> None:
    cores = CORES_NIVEL
    for anel in _poligonos():
        ax.add_patch(PoligonoMpl(anel, closed=True, facecolor="#e6e9ee", edgecolor="#1d4e89", linewidth=1.1, zorder=1))
    ordenada = tabela.assign(_s=tabela["Score"].fillna(-1)).sort_values("_s")
    for posicao, (_, linha) in enumerate(ordenada.iterrows()):
        cor = cores.get(linha["Risco"], cores["Sem dados"])
        score = linha["Score"]
        tamanho = 120 if pd.isna(score) else 120 + float(score) * 5
        # Cada marcador e seu número ganham um zorder próprio: o de maior risco cobre o texto dos de baixo.
        base = 3 + posicao * 0.01
        ax.scatter(linha["Longitude"], linha["Latitude"], s=tamanho, c=cor, edgecolors="white", linewidths=1.6, zorder=base)
        if not pd.isna(score):
            ax.text(linha["Longitude"], linha["Latitude"], f"{score:.0f}", ha="center", va="center",
                    fontsize=6.5, fontweight="bold", color=_texto_sobre(cor), zorder=base + 0.005)
    # Rótulos apenas para as capitais de maior risco (evita poluição visual).
    destaque = tabela.dropna(subset=["Score"]).sort_values("Score", ascending=False).head(6)
    for _, linha in destaque.iterrows():
        if linha["Score"] >= 45:
            ax.annotate(sigla(linha["Capital"]), (linha["Longitude"], linha["Latitude"]), xytext=(9, 7),
                        textcoords="offset points", fontsize=7, color=TEXTO, zorder=20,
                        bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "#cbd2d9", "alpha": 0.92})
    ax.set_xlim(-74.5, -33.5)
    ax.set_ylim(-34.5, 5.8)
    ax.set_aspect(1.0)
    ax.axis("off")
    manipuladores = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=cores[r], markeredgecolor="white",
               markersize=9, label=r)
        for r in reversed(ROTULOS)
    ]
    ax.legend(handles=manipuladores, title="Risco de raios", loc="lower left", frameon=True, fontsize=8, title_fontsize=8)


def _figura_mapa(tabela: pd.DataFrame, titulo: str, subtitulo: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(8.27, 8.8), dpi=150)
    fig.patch.set_facecolor(FUNDO)
    _desenhar_mapa(ax, tabela)
    fig.text(0.05, 0.96, titulo, fontsize=15, fontweight="bold", color=TEXTO, va="top")
    fig.text(0.05, 0.925, subtitulo, fontsize=9, color=SECUNDARIO, va="top")
    fig.text(0.05, 0.02, "Score heurístico (CAPE, Lifted Index, CIN). Não substitui alertas oficiais. Dados: Open-Meteo.",
             fontsize=7, color=SECUNDARIO)
    fig.subplots_adjust(left=0.03, right=0.97, top=0.88, bottom=0.05)
    return fig


def _figura_tabela(titulo: str, subtitulo: str, pagina: pd.DataFrame, numero: int, total: int) -> plt.Figure:
    cores = CORES_NIVEL
    fig = plt.figure(figsize=(8.27, 11.69), dpi=150)
    fig.patch.set_facecolor(FUNDO)
    fig.text(0.05, 0.965, titulo, fontsize=14, fontweight="bold", color=TEXTO, va="top")
    fig.text(0.05, 0.94, f"{subtitulo} · página {numero}/{total}", fontsize=8.5, color=SECUNDARIO, va="top")
    ax = fig.add_axes([0.04, 0.05, 0.92, 0.85])
    ax.axis("off")
    colunas = ["Capital", "UF", "Risco", "Score", "Tend.", "CAPE", "LI", "CIN"]
    celulas = []
    for _, l in pagina.iterrows():
        celulas.append([
            (l["Capital"] if len(l["Capital"]) <= 46 else l["Capital"][:45] + "…"), l["UF"], l["Risco"],
            "—" if pd.isna(l["Score"]) else f"{l['Score']:.1f}",
            l.get("Tendência", "") or "",
            "—" if pd.isna(l["CAPE (J/kg)"]) else f"{l['CAPE (J/kg)']:.0f}",
            "—" if pd.isna(l["Lifted Index (°C)"]) else f"{l['Lifted Index (°C)']:.1f}",
            "—" if pd.isna(l["CIN (J/kg)"]) else f"{l['CIN (J/kg)']:.0f}",
        ])
    tab = ax.table(cellText=celulas, colLabels=colunas, loc="upper center", cellLoc="left",
                   colWidths=[0.40, 0.06, 0.11, 0.08, 0.07, 0.09, 0.08, 0.08])
    tab.auto_set_font_size(False)
    tab.set_fontsize(7.5)
    tab.scale(1, 1.55)
    for (linha, coluna), celula in tab.get_celld().items():
        celula.set_edgecolor("#d9dee4")
        if linha == 0:
            celula.set_facecolor("#1f2933")
            celula.set_text_props(color="white", fontweight="bold")
        elif coluna == 2:
            cor = cores.get(celulas[linha - 1][2], cores["Sem dados"])
            celula.set_facecolor(cor)
            celula.set_text_props(color=_texto_sobre(cor), fontweight="bold")
    return fig


def gerar_png(tabela: pd.DataFrame, titulo: str, subtitulo: str) -> bytes:
    fig = _figura_mapa(tabela, titulo, subtitulo)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buffer.getvalue()


def gerar_pdf(tabela: pd.DataFrame, titulo: str, subtitulo: str, por_pagina: int = 32) -> bytes:
    """PDF com o mapa na 1ª página e o ranking das capitais (maior risco primeiro) nas seguintes."""
    ranking = tabela.assign(_s=tabela["Score"].fillna(-1)).sort_values(["_s", "Capital"], ascending=[False, True])
    paginas = [ranking.iloc[i : i + por_pagina] for i in range(0, len(ranking), por_pagina)] or [ranking]
    buffer = io.BytesIO()
    with PdfPages(buffer) as pdf:
        fig = _figura_mapa(tabela, titulo, subtitulo)
        pdf.savefig(fig, facecolor=fig.get_facecolor())
        plt.close(fig)
        for numero, pagina in enumerate(paginas, start=1):
            fig = _figura_tabela(titulo, subtitulo, pagina, numero, len(paginas))
            pdf.savefig(fig, facecolor=fig.get_facecolor())
            plt.close(fig)
    return buffer.getvalue()
