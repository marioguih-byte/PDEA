"""Análise dos dados do PDEA (sem dependência do Streamlit).

Concentra o cálculo das séries de score (com heurística ampliada e ajuste por UF)
e da tendência, para ser reutilizado pelo app, pelos alertas, pelo histórico e
pelo relatório.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from modelos import horario_local
from risco_raio import PARAMETROS_PADRAO, ParametrosRisco, calcular_risco, classificar_risco, detalhar_risco
from unidades import ESTACOES, UFS

JANELA_TENDENCIA_H = 6  # horas à frente usadas para decidir ▲ / ▼
LIMIAR_TENDENCIA = 10.0  # variação mínima de score para indicar subida/descida
JANELA_PICO_H = 24  # horas à frente usadas para localizar o pico

SETA_SOBE, SETA_DESCE, SETA_ESTAVEL = "▲", "▼", "▬"


def _valor(serie: list[Optional[float]], indice: int) -> Optional[float]:
    return serie[indice] if 0 <= indice < len(serie) else None


ARQUIVO_REGIOES = Path(__file__).resolve().parent / "config_regioes.json"
FATORES_NEUTROS = (1.0, 1.0)  # (fator_cape, fator_li) de uma UF sem ajuste


def carregar_regioes(caminho: Path = ARQUIVO_REGIOES) -> dict[str, tuple[float, float]]:
    """Fatores (CAPE, LI) por UF a partir de ``config_regioes.json``; UFs ausentes ficam neutras (1,0)."""
    fatores = {uf: FATORES_NEUTROS for uf in UFS}
    try:
        bruto = json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fatores
    for uf, valores in bruto.items():
        if uf in fatores and isinstance(valores, dict):
            try:
                fatores[uf] = (float(valores.get("fator_cape", 1.0)), float(valores.get("fator_li", 1.0)))
            except (TypeError, ValueError):
                pass
    return fatores


def extras_na_hora(serie: dict[str, Any], indice: int) -> Optional[dict[str, Optional[float]]]:
    """Variáveis da heurística ampliada na hora ``indice`` (índice absoluto da série), ou ``None`` se não há extras."""
    if not any(campo in serie for campo in ("precip", "rajada", "nivel0", "t850", "t500")):
        return None
    t850, t500 = _valor(serie.get("t850", []), indice), _valor(serie.get("t500", []), indice)
    return {
        "precip": _valor(serie.get("precip", []), indice),
        "rajada": _valor(serie.get("rajada", []), indice),
        "nivel0": _valor(serie.get("nivel0", []), indice),
        "gradiente": (t850 - t500) if t850 is not None and t500 is not None else None,
    }


@dataclass(frozen=True)
class CorrecaoCosteira:
    """Correção a priori para capitais litorâneas (fator sobre o produto CAPE x chuva).

    O CAPE x P reproduz a distribuição das descargas sobre terra, mas não o contraste terra-oceano (Romps et al.,
    2018), em que as descargas sobre o mar são várias vezes menos frequentes (cerca de 5 a 10 vezes; Williams e
    Stanfill, 2002). Uma capital litorânea recebe um fator intermediário entre 1 (terra) e o oceano puro (~0,14):
    o ponto médio geométrico, ~0,38, arredondado para 0,4. É um valor PROVISÓRIO, a ser substituído pela estimativa
    com o GLM (``metodologia/estimar_correcao_costeira.py``).
    """

    ufs: frozenset = frozenset()
    fator: float = 1.0


@lru_cache(maxsize=8)
def _correcao_costeira(caminho: str) -> CorrecaoCosteira:
    try:
        bruto = json.loads(Path(caminho).read_text(encoding="utf-8")).get("correcao_costeira", {})
        fator = float(bruto.get("fator", 1.0))
        if not 0.0 < fator <= 1.0:
            return CorrecaoCosteira()
        return CorrecaoCosteira(ufs=frozenset(uf for uf in bruto.get("ufs", []) if uf in UFS), fator=fator)
    except (OSError, ValueError, TypeError, AttributeError):
        return CorrecaoCosteira()


def carregar_correcao_costeira(caminho: Path = ARQUIVO_REGIOES) -> CorrecaoCosteira:
    """Lê a seção ``correcao_costeira`` de ``config_regioes.json`` (sem a seção, a correção fica desligada)."""
    return _correcao_costeira(str(caminho))


def fator_costeiro(uf: str) -> float:
    """Fator sobre o produto CAPE x chuva da capital da UF (1,0 se a capital não é litorânea)."""
    c = carregar_correcao_costeira()
    return c.fator if uf in c.ufs else 1.0


def parametros_da_unidade(
    parametros: ParametrosRisco, uf: str, regioes: Optional[dict[str, tuple[float, float]]] = None
) -> ParametrosRisco:
    """Aplica os fatores regionais (CAPE e LI) e a correção litorânea sobre os parâmetros globais."""
    fator_cape, fator_li = (regioes or {}).get(uf, FATORES_NEUTROS)
    costeiro = fator_costeiro(uf)
    if (fator_cape, fator_li) == FATORES_NEUTROS and costeiro == 1.0:
        return parametros
    return replace(
        parametros,
        fator_cape=parametros.fator_cape * fator_cape,
        fator_li=parametros.fator_li * fator_li,
        fator_produto=parametros.fator_produto * costeiro,
    )


@dataclass(frozen=True)
class GatePrecipitacao:
    """Exige chuva prevista para manter o score nas UFs listadas.

    Em regiões onde CAPE/LI/CIN costumam ser "altos" sem haver tempestade (litoral tropical sob
    inversão de alísios, por exemplo), o score só vale integralmente se o modelo também prevê
    precipitação (>= ``limiar_mm_h``) em alguma hora entre a atual e ``janela_h`` horas à frente.
    Caso contrário, o score é multiplicado por ``multiplicador``.
    """

    ufs: frozenset = frozenset()
    limiar_mm_h: float = 0.1
    janela_h: int = 3
    multiplicador: float = 0.5


def carregar_gate(caminho: Path = ARQUIVO_REGIOES) -> GatePrecipitacao:
    """Lê a seção ``gate_precipitacao`` de ``config_regioes.json`` (sem a seção, o gate fica desligado)."""
    try:
        bruto = json.loads(Path(caminho).read_text(encoding="utf-8")).get("gate_precipitacao", {})
        return GatePrecipitacao(
            ufs=frozenset(uf for uf in bruto.get("ufs", []) if uf in UFS),
            limiar_mm_h=float(bruto.get("limiar_mm_h", 0.1)),
            janela_h=int(bruto.get("janela_h", 3)),
            multiplicador=float(bruto.get("multiplicador", 0.5)),
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return GatePrecipitacao()


GATE_DESLIGADO = GatePrecipitacao()  # nenhuma UF: não age


def gate_efetivo(gate: Optional[GatePrecipitacao], parametros: ParametrosRisco) -> Optional[GatePrecipitacao]:
    """O ajuste por chuva prevista é um complemento do método HEURÍSTICO, que não usa a chuva.

    No método CAPE × chuva a chuva já faz parte do produto e as capitais litorâneas têm a própria correção: aplicar o ajuste também
    corrigiria duas vezes. Por isso, em qualquer outro método, o ajuste é ignorado (aqui, em um só lugar, para o painel, o histórico
    e os alertas ficarem sempre de acordo).
    """
    if gate is None or parametros.metodo == "pontos":
        return gate
    return GATE_DESLIGADO


def janela_frente(valores: list[Optional[float]], janela: int) -> list[Optional[float]]:
    """Máximo de cada posição e das ``janela`` seguintes (ignora ``None``; ``None`` se não houver nenhum valor)."""
    saida: list[Optional[float]] = []
    for i in range(len(valores)):
        trecho = [v for v in valores[i : i + janela + 1] if v is not None]
        saida.append(max(trecho) if trecho else None)
    return saida


def multiplicador_do_gate(chuva_na_janela: Optional[float], uf: str, gate: Optional[GatePrecipitacao]) -> Optional[float]:
    """Multiplicador a aplicar (ou ``None``). Sem dado de chuva, o gate NÃO é aplicado (evita esconder risco por falta de dado)."""
    if gate is None or uf not in gate.ufs or chuva_na_janela is None:
        return None
    return gate.multiplicador if chuva_na_janela < gate.limiar_mm_h else None


def scores_relativos(
    serie: dict[str, Any],
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    uf: str = "",
    gate: Optional[GatePrecipitacao] = None,
) -> list[Optional[float]]:
    """Scores horários a partir da hora atual: posição 0 = agora, 1 = +1 h, …"""
    gate = gate_efetivo(gate, parametros)
    inicio = serie.get("idx_atual", 0)
    n = len(serie.get("tempos", []))
    capes, lis, cins = serie.get("cape", []), serie.get("li", []), serie.get("cin", [])
    usa_extras = parametros.usa_extras
    chuva = janela_frente(serie["precip"], gate.janela_h) if gate and uf in gate.ufs and "precip" in serie else None
    return [
        calcular_risco(
            _valor(capes, i),
            _valor(lis, i),
            _valor(cins, i),
            parametros,
            extras_na_hora(serie, i) if usa_extras else None,
            multiplicador_do_gate(_valor(chuva, i) if chuva is not None else None, uf, gate),
        )[0]
        for i in range(inicio, n)
    ]


def gate_relativo(serie: dict[str, Any], uf: str, gate: Optional[GatePrecipitacao]) -> list[Optional[bool]]:
    """Por hora (0 = agora): ``True`` = score reduzido por falta de chuva prevista; ``False`` = chuva prevista;
    ``None`` = o gate não se aplica (UF fora da lista ou sem dado de precipitação)."""
    inicio, n = serie.get("idx_atual", 0), len(serie.get("tempos", []))
    if not (gate and uf in gate.ufs and "precip" in serie):
        return [None] * max(0, n - inicio)
    chuva = janela_frente(serie["precip"], gate.janela_h)
    return [None if chuva[i] is None else chuva[i] < gate.limiar_mm_h for i in range(inicio, n)]


def explicar_hora(
    serie: dict[str, Any],
    deslocamento: int,
    uf: str,
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    regioes: Optional[dict[str, tuple[float, float]]] = None,
    gate: Optional[GatePrecipitacao] = None,
) -> dict[str, Any]:
    """Como o score de uma hora foi formado (pontos por componente, ajuste regional e gate)."""
    gate = gate_efetivo(gate, parametros)
    i = serie.get("idx_atual", 0) + deslocamento
    p = parametros_da_unidade(parametros, uf, regioes)
    chuva = None
    if gate and uf in gate.ufs and "precip" in serie:
        chuva = _valor(janela_frente(serie["precip"], gate.janela_h), i)
    mult = multiplicador_do_gate(chuva, uf, gate)
    detalhe = detalhar_risco(
        _valor(serie.get("cape", []), i), _valor(serie.get("li", []), i), _valor(serie.get("cin", []), i),
        p, extras_na_hora(serie, i) if p.usa_extras else None, mult,
    )
    return {
        **detalhe,
        "cin": _valor(serie.get("cin", []), i),
        "gate_configurado": bool(gate and uf in gate.ufs),
        "gate_sem_dado": bool(gate and uf in gate.ufs and chuva is None),
        "gate_aplicado": mult is not None,
        "chuva_janela": chuva,
        "janela_h": gate.janela_h if gate else None,
        "limiar": gate.limiar_mm_h if gate else None,
        "fator_cape_regiao": (regioes or {}).get(uf, FATORES_NEUTROS)[0],
        "fator_li_regiao": (regioes or {}).get(uf, FATORES_NEUTROS)[1],
    }


def horas_a_frente(dados: dict[str, Any]) -> int:
    """Maior deslocamento (em horas, a partir de agora) disponível para todas as unidades."""
    restantes = [
        len(s.get("tempos", [])) - s.get("idx_atual", 0) - 1
        for nome, s in dados.items()
        if not nome.startswith("_") and isinstance(s, dict)
    ]
    return max(0, min(restantes)) if restantes else 0


def rotulo_horario(dados: dict[str, Any], deslocamento: int) -> str:
    """Horário local (HH:MM dd/mm) do deslocamento informado."""
    for nome, serie in dados.items():
        if nome.startswith("_") or not isinstance(serie, dict):
            continue
        tempos, inicio = serie.get("tempos", []), serie.get("idx_atual", 0)
        if 0 <= inicio + deslocamento < len(tempos):
            try:
                return horario_local(tempos[inicio + deslocamento]).strftime("%H:%M (%d/%m)")
            except ValueError:
                return tempos[inicio + deslocamento]
    return "—"


def series_por_unidade(
    dados: dict[str, Any],
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    regioes: Optional[dict[str, tuple[float, float]]] = None,
    gate: Optional[GatePrecipitacao] = None,
) -> dict[str, dict[str, Any]]:
    """Série de score por unidade (``rel``: posição 0 = agora, 1 = +1 h, …), com ajuste por UF e gate de chuva."""
    gate = gate_efetivo(gate, parametros)
    return {
        estacao["nome"]: {
            "rel": scores_relativos(
                dados.get(estacao["nome"], {}),
                parametros_da_unidade(parametros, estacao["uf"], regioes),
                estacao["uf"],
                gate,
            ),
            "gate": gate_relativo(dados.get(estacao["nome"], {}), estacao["uf"], gate),
        }
        for estacao in ESTACOES
    }


def tendencia(rel: list[Optional[float]], deslocamento: int = 0) -> dict[str, Any]:
    """Seta de tendência (próximas 6 h), variação e pico (próximas 24 h) a partir do deslocamento."""
    agora = rel[deslocamento] if 0 <= deslocamento < len(rel) else None
    vazio = {"seta": "", "delta6h": np.nan, "pico": np.nan, "desloc_pico": None}
    if agora is None:
        return vazio

    def janela(horas: int) -> list[tuple[int, float]]:
        fim = min(len(rel), deslocamento + horas + 1)
        return [(i, rel[i]) for i in range(deslocamento + 1, fim) if rel[i] is not None]

    proximas = janela(JANELA_TENDENCIA_H)
    delta_alta = max((v for _, v in proximas), default=agora) - agora
    delta_baixa = min((v for _, v in proximas), default=agora) - agora
    if delta_alta >= LIMIAR_TENDENCIA:
        seta, delta = SETA_SOBE, delta_alta
    elif delta_baixa <= -LIMIAR_TENDENCIA:
        seta, delta = SETA_DESCE, delta_baixa
    else:
        seta, delta = SETA_ESTAVEL, 0.0

    candidatos = [(deslocamento, agora)] + janela(JANELA_PICO_H)
    desloc_pico, pico = max(candidatos, key=lambda par: (par[1], -par[0]))
    return {"seta": seta, "delta6h": round(delta, 1), "pico": pico, "desloc_pico": desloc_pico}


def consolidar(
    dados: dict[str, Any],
    series: dict[str, dict[str, Any]],
    deslocamento: int = 0,
) -> pd.DataFrame:
    """Tabela com a leitura de todas as unidades no deslocamento (horas a partir de agora)."""
    linhas: list[dict[str, Any]] = []
    for estacao in ESTACOES:
        nome = estacao["nome"]
        serie = dados.get(nome, {})
        i = serie.get("idx_atual", 0) + deslocamento
        info = series[nome]
        rel = info["rel"]
        score = rel[deslocamento] if 0 <= deslocamento < len(rel) else None
        if score is None:
            nivel = "Sem dados"
        else:
            nivel = classificar_risco(score)[0]

        flags = info.get("gate", [])
        ajuste = "reduzido" if 0 <= deslocamento < len(flags) and flags[deslocamento] is True else ""
        tend = tendencia(rel, deslocamento)
        pico_hora = rotulo_horario({nome: serie}, tend["desloc_pico"]) if tend["desloc_pico"] is not None else "—"

        extras = extras_na_hora(serie, i) or {}
        linhas.append(
            {
                "Capital": nome,
                "UF": estacao["uf"],
                "CAPE (J/kg)": _valor(serie.get("cape", []), i),
                "Lifted Index (°C)": _valor(serie.get("li", []), i),
                "CIN (J/kg)": _valor(serie.get("cin", []), i),
                "Risco": nivel,
                "Score": score,
                "Tendência": tend["seta"],
                "Δ 6 h": tend["delta6h"],
                "Pico 24 h": tend["pico"],
                "Hora do pico": pico_hora,
                "Ajuste chuva": ajuste,
                "Precip. (mm/h)": extras.get("precip"),
                "Rajada (km/h)": extras.get("rajada"),
                "Gradiente 850–500 (°C)": extras.get("gradiente"),
                "Nível 0 °C (m)": extras.get("nivel0"),
                "Latitude": estacao["lat"],
                "Longitude": estacao["lon"],
            }
        )
    return pd.DataFrame(linhas).sort_values("Capital", kind="stable").reset_index(drop=True)
