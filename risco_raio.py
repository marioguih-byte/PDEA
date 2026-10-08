"""Escore de risco de raios do PDEA.

Método principal (``metodo="capexp"``): proxy CAPE x P de Romps et al. (2014, Science) e Romps et al. (2018, GRL):
a taxa de descargas é proporcional ao produto contemporâneo de CAPE (J/kg) e taxa de precipitação (mm/h).
O produto é convertido em escore de 0 a 100 por interpolação linear em log10 entre âncoras (``ANCORAS_CAPEXP``);
as âncoras são PROVISÓRIAS e devem ser calibradas com observações (ver ``metodologia/``).

Método legado (``metodo="pontos"``): heurística por faixas de CAPE, Lifted Index e CIN (PDEA-H), mantida para comparação.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

# (limite superior do score, rótulo, cor da paleta padrão)
NIVEIS_RISCO = [
    (15, "Nenhum", "#5a6472"),
    (35, "Baixo", "#3fa34d"),
    (55, "Moderado", "#e0b400"),
    (75, "Alto", "#e0761f"),
    (101, "Severo", "#d13b3b"),
]

ROTULOS = [rotulo for _, rotulo, _ in NIVEIS_RISCO]
LIMITES = {rotulo: limite for limite, rotulo, _ in NIVEIS_RISCO}
ORDEM = {rotulo: posicao for posicao, rotulo in enumerate(ROTULOS)}  # Nenhum=0 … Severo=4
COR_SEM_DADOS = "#3a3f47"

CORES_NIVEL = {rotulo: cor for _, rotulo, cor in NIVEIS_RISCO} | {"Sem dados": COR_SEM_DADOS}
ICONES = {"Nenhum": "⚪", "Baixo": "🟢", "Moderado": "🟡", "Alto": "🟠", "Severo": "🔴", "Sem dados": "⚫"}


@dataclass(frozen=True)
class ParametrosRisco:
    """Ajustes de sensibilidade da heurística (1,0 = comportamento original).

    - ``fator_cape``: multiplica o CAPE antes de aplicar os limiares (>1 = mais sensível).
    - ``fator_li``: multiplica o Lifted Index (>1 amplifica instabilidade e estabilidade).
    - ``peso_cin``: multiplica a contribuição do CIN ao score (0 = ignora o CIN).
    - ``peso_extras``: peso da heurística ampliada (0 = desligada; 1 = ajustes na escala padrão); só vale em ``"pontos"``.
    - ``metodo``: ``"capexp"`` (CAPE x chuva, padrão) ou ``"pontos"`` (heurística legada PDEA-H).
    - ``fator_produto``: multiplica o produto CAPE x chuva (só no método ``"capexp"``). Usado na correção litorânea:
      o CAPE x P não reproduz o menor número de descargas sobre o oceano (Romps et al., 2018), então as capitais
      litorâneas recebem um fator < 1 (ver ``correcao_costeira`` em ``config_regioes.json``).
    """

    fator_cape: float = 1.0
    fator_li: float = 1.0
    peso_cin: float = 1.0
    peso_extras: float = 0.0
    metodo: str = "capexp"
    fator_produto: float = 1.0

    @property
    def usa_extras(self) -> bool:
        """Se a série de precipitação (e demais extras) precisa ser entregue ao cálculo."""
        return self.metodo == "capexp" or self.peso_extras > 0


PARAMETROS_PADRAO = ParametrosRisco()


def classificar_risco(score: float) -> tuple[str, str]:
    """Converte um escore de 0 a 100 em rótulo e cor hexadecimal (paleta padrão)."""
    for limite, rotulo, cor in NIVEIS_RISCO:
        if score < limite:
            return rotulo, cor
    return "Severo", "#d13b3b"


# ----------------------------------------------------------------------------
# Método principal: CAPE x P (Romps et al., 2014, 2018).
# Âncoras (CAPE x P em J/kg x mm/h -> score). PROVISÓRIAS: os limites dos níveis do painel caem exatamente
# nas âncoras 15 / 35 / 55 / 75. Exemplos: CAPE 500 x 0,1 mm/h = 50 (Baixo); 1000 x 1 = 1000 (Alto);
# 2000 x 2 = 4000 (Severo).
# ----------------------------------------------------------------------------
ANCORAS_CAPEXP: list[tuple[float, float]] = [
    (10.0, 0.0), (50.0, 15.0), (200.0, 35.0), (1000.0, 55.0), (4000.0, 75.0), (20000.0, 100.0),
]


def pontuar_capexp(x: float) -> float:
    """Converte CAPE x P em escore 0-100 (interpolação linear em log10 entre as âncoras; monótona)."""
    if x <= ANCORAS_CAPEXP[0][0]:
        return 0.0
    if x >= ANCORAS_CAPEXP[-1][0]:
        return 100.0
    for (x0, s0), (x1, s1) in zip(ANCORAS_CAPEXP, ANCORAS_CAPEXP[1:]):
        if x <= x1:
            return s0 + (s1 - s0) * (math.log10(x) - math.log10(x0)) / (math.log10(x1) - math.log10(x0))
    return 100.0


def _detalhar_capexp(cape: float, lifted_index: Optional[float], parametros: "ParametrosRisco",
                     extras: Optional[dict[str, Optional[float]]], multiplicador_gate: Optional[float]) -> dict[str, Any]:
    cape_ef = max(cape, 0.0) * parametros.fator_cape
    li_ef = lifted_index * parametros.fator_li if lifted_index is not None else None
    chuva = (extras or {}).get("precip")
    base: dict[str, Any] = {"metodo": "capexp", "cape_ef": cape_ef, "li_ef": li_ef, "pts_cape": 0.0, "pts_li": 0.0,
                            "pts_cin": 0.0, "pts_extras": 0.0, "sem_energia": False, "chuva": chuva}
    if chuva is None:  # sem precipitação do modelo o produto não existe: melhor "sem dados" do que um risco inventado
        return {**base, "score": None, "subtotal": None, "multiplicador": 1.0, "cape_x_chuva": None, "sem_chuva": True}
    bruto = cape_ef * max(chuva, 0.0)
    x = bruto * parametros.fator_produto
    pontos = pontuar_capexp(x)
    mult = 1.0 if multiplicador_gate is None else multiplicador_gate
    return {**base, "score": round(max(0.0, min(100.0, pontos * mult)), 1), "subtotal": pontos, "multiplicador": mult,
            "cape_x_chuva": bruto, "fator_produto": parametros.fator_produto, "produto_corrigido": x, "sem_chuva": False}


# ----------------------------------------------------------------------------
# Heurística ampliada (método legado "pontos"): ajustes somados ao score básico (CAPE + LI + CIN).
# Os pontos são limitados e pequenos perto do score básico, e ainda NÃO foram
# calibrados com observações: use o peso para ajustá-los e valide com o histórico.
# ----------------------------------------------------------------------------
LIMITES_PRECIPITACAO = [(10.0, 10.0), (5.0, 7.0), (2.0, 4.0), (0.5, 1.0)]  # mm/h → pontos
LIMITES_RAJADA = [(70.0, 6.0), (50.0, 4.0), (35.0, 2.0)]  # km/h → pontos
LIMITES_GRADIENTE = [(29.0, 6.0), (27.0, 4.0), (25.0, 2.0)]  # T850 − T500 (°C) → pontos
NIVEL_0C_BAIXO, NIVEL_0C_ALTO = 4200.0, 5300.0  # m: abaixo = +2 (mais camada mista), acima = −2
AJUSTE_MAXIMO, AJUSTE_MINIMO = 20.0, -4.0


def _pontos(valor: Optional[float], limites: list[tuple[float, float]]) -> float:
    if valor is None:
        return 0.0
    for limite, pontos in limites:
        if valor >= limite:
            return pontos
    return 0.0


def ajuste_extras(extras: Optional[dict[str, Optional[float]]]) -> float:
    """Pontos adicionais (antes do peso) a partir de precipitação, rajada, gradiente 850–500 hPa e nível de 0 °C.

    Variáveis ausentes (``None``) simplesmente não contribuem.
    """
    if not extras:
        return 0.0
    pontos = _pontos(extras.get("precip"), LIMITES_PRECIPITACAO)
    pontos += _pontos(extras.get("rajada"), LIMITES_RAJADA)
    pontos += _pontos(extras.get("gradiente"), LIMITES_GRADIENTE)
    nivel0 = extras.get("nivel0")
    if nivel0 is not None:
        pontos += 2.0 if nivel0 < NIVEL_0C_BAIXO else (-2.0 if nivel0 > NIVEL_0C_ALTO else 0.0)
    return max(AJUSTE_MINIMO, min(AJUSTE_MAXIMO, pontos))


# Sem energia disponível (CAPE efetivo abaixo disto) não há tempestade a "disparar": o Lifted Index
# e a baixa inibição (CIN) deixam de somar pontos altos, para que ar estável não vire "Moderado".
ENERGIA_MINIMA_CAPE = 300.0  # J/kg
PONTOS_MAX_LI_SEM_ENERGIA = 5.0


# Faixas da heurística (PDEA-H). Fonte ÚNICA: o cálculo abaixo e a explicação mostrada no painel leem estas tabelas.
# As faixas são empíricas (definidas pela equipe a partir de valores usuais, sem artigo de referência) e NÃO foram calibradas
# com observações de descargas no Brasil.
FAIXAS_CAPE: list[tuple[float, float]] = [(3500.0, 45.0), (2500.0, 34.0), (1000.0, 22.0), (300.0, 10.0)]  # CAPE >= limite -> pontos (abaixo de 300: 0)
FAIXAS_LI: list[tuple[float, float]] = [(2.0, 0.0), (0.0, 5.0), (-2.0, 12.0), (-6.0, 22.0), (-9.0, 30.0)]  # LI > limite -> pontos
PONTOS_LI_MINIMO = 35.0  # LI <= -9 °C
FAIXAS_CIN: list[tuple[float, float]] = [(25.0, 20.0), (50.0, 10.0), (100.0, 0.0), (200.0, -15.0)]  # |CIN| < limite -> pontos
PONTOS_CIN_MAXIMO = -30.0  # |CIN| >= 200 J/kg

NOMES_METODO = {"capexp": "CAPE × chuva", "pontos": "Heurístico (CAPE, LI e CIN)"}


def _pontos_cape(cape: float) -> float:
    for limite, pontos in FAIXAS_CAPE:
        if cape >= limite:
            return pontos
    return 0.0


def _pontos_li(lifted_index: float) -> float:
    for limite, pontos in FAIXAS_LI:
        if lifted_index > limite:
            return pontos
    return PONTOS_LI_MINIMO


def _pontos_cin(cin: float) -> float:
    cin_abs = abs(cin)
    for limite, pontos in FAIXAS_CIN:
        if cin_abs < limite:
            return pontos
    return PONTOS_CIN_MAXIMO


def detalhar_risco(
    cape: Optional[float],
    lifted_index: Optional[float],
    cin: Optional[float],
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    extras: Optional[dict[str, Optional[float]]] = None,
    multiplicador_gate: Optional[float] = None,
) -> dict[str, Any]:
    """Score e a contribuição de cada componente (para explicar o resultado ao usuário).

    ``multiplicador_gate`` (ex.: 0,5) multiplica o score quando uma condição de disparo exigida
    (como chuva prevista) não é atendida; ``None`` = sem gate.
    """
    vazio: dict[str, Any] = {"score": None, "cape_ef": None, "li_ef": None, "pts_cape": 0.0, "pts_li": 0.0, "pts_cin": 0.0,
                              "pts_extras": 0.0, "sem_energia": False, "subtotal": None, "multiplicador": 1.0}
    if cape is None:
        return vazio
    if parametros.metodo == "capexp":
        return _detalhar_capexp(cape, lifted_index, parametros, extras, multiplicador_gate)

    cape_ef = cape * parametros.fator_cape
    li_ef = lifted_index * parametros.fator_li if lifted_index is not None else None
    pts_cape = _pontos_cape(cape_ef)
    pts_li = _pontos_li(li_ef) if li_ef is not None else 0.0
    pts_cin = _pontos_cin(cin) * parametros.peso_cin if cin is not None else 0.0
    sem_energia = cape_ef < ENERGIA_MINIMA_CAPE
    if sem_energia:
        pts_li = min(pts_li, PONTOS_MAX_LI_SEM_ENERGIA)
        pts_cin = min(pts_cin, 0.0)
    pts_extras = ajuste_extras(extras) * parametros.peso_extras if parametros.peso_extras > 0 else 0.0

    subtotal = pts_cape + pts_li + pts_cin + pts_extras
    multiplicador = 1.0 if multiplicador_gate is None else multiplicador_gate
    score = max(0.0, min(100.0, subtotal * multiplicador))
    return {"score": round(score, 1), "cape_ef": cape_ef, "li_ef": li_ef, "pts_cape": pts_cape, "pts_li": pts_li,
            "pts_cin": pts_cin, "pts_extras": pts_extras, "sem_energia": sem_energia, "subtotal": subtotal,
            "multiplicador": multiplicador}


def calcular_risco(
    cape: Optional[float],
    lifted_index: Optional[float],
    cin: Optional[float],
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    extras: Optional[dict[str, Optional[float]]] = None,
    multiplicador_gate: Optional[float] = None,
) -> tuple[Optional[float], str, str]:
    """Calcula um escore de risco de raio com base em CAPE, LI e CIN.

    CAPE alto favorece a convecção profunda; Lifted Index negativo representa maior instabilidade;
    e CIN alto reduz a probabilidade de disparo convectivo. Sem energia (CAPE < 300 J/kg), o LI e a
    baixa inibição não somam pontos altos. ``extras`` só entra na conta se ``parametros.peso_extras`` > 0.
    A função retorna ``(escore, nível, cor)`` (cor da paleta padrão).
    """
    detalhe = detalhar_risco(cape, lifted_index, cin, parametros, extras, multiplicador_gate)
    if detalhe["score"] is None:
        return None, "Sem dados", COR_SEM_DADOS
    rotulo, cor = classificar_risco(detalhe["score"])
    return detalhe["score"], rotulo, cor
