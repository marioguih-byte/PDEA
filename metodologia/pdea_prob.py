"""PDEA-P: preditor probabilístico de descargas, para comparar com a heurística (PDEA-H).

Ideia central (sem limiares escolhidos à mão):
  P(raio na hora | ambiente) = regressão logística sobre ingredientes físicos
  (energia, instabilidade, umidade/precipitação), com o proxy CAPE x chuva de Romps et al. (2014)
  como atributo explícito. Gradient boosting é oferecido como alternativa não linear.

Este módulo não depende do Streamlit. Colunas esperadas na tabela de entrada:
  local, hora, cape, li, cin, precip, rajada, nivel0, t850, t500  (e 'flashes' para treinar/verificar).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from risco_raio import ParametrosRisco, calcular_risco, pontuar_capexp  # noqa: E402

COLUNAS_BRUTAS = ["cape", "li", "cin", "precip", "rajada", "nivel0", "t850", "t500"]

CONJUNTOS = {
    # ingredientes mínimos: energia, instabilidade, chuva e o proxy CAPE x chuva
    "nucleo": ["cape_log", "li", "precip_log", "cape_x_chuva"],
    # mesmo núcleo + CIN, para testar se o CIN acrescenta algo (no artigo do PDEA ele não acrescentou)
    "nucleo_cin": ["cape_log", "li", "precip_log", "cape_x_chuva", "cin_abs_log"],
    "completo": ["cape_log", "li", "precip_log", "cape_x_chuva", "vt", "nivel0_km", "rajada_log", "cin_abs_log"],
    # condições de 3 h antes (tendência do ambiente convectivo)
    "defasagem": ["cape_log", "li", "precip_log", "cape_x_chuva", "vt", "nivel0_km", "rajada_log",
                  "cape_log_l3", "li_l3", "precip_log_l3", "d_cape_3h"],
    # núcleo + ciclo diurno (a convecção tem hora preferida)
    "diurno": ["cape_log", "li", "precip_log", "cape_x_chuva", "sen_hora", "cos_hora"],
}


def _defasar(d: pd.DataFrame, colunas: list[str], horas: int) -> pd.DataFrame:
    """Valor de cada coluna 'horas' h antes, casando por horário (seguro com falhas na série)."""
    ant = d[["local", "hora"] + colunas].copy()
    ant["hora"] = ant["hora"] + pd.Timedelta(hours=horas)
    ant = ant.rename(columns={c: f"{c}__ant" for c in colunas})
    return d[["local", "hora"]].merge(ant, on=["local", "hora"], how="left")


def construir_atributos(df: pd.DataFrame) -> pd.DataFrame:
    """Devolve a tabela ordenada, com os atributos de todos os conjuntos."""
    faltam = [c for c in ["local", "hora", "cape", "li"] if c not in df.columns]
    if faltam:
        raise ValueError(f"Colunas ausentes: {faltam}")
    d = df.copy()
    d["hora"] = pd.to_datetime(d["hora"])
    for c in COLUNAS_BRUTAS:
        if c not in d.columns:
            d[c] = np.nan
    d = d.sort_values(["local", "hora"]).reset_index(drop=True)

    cape = d["cape"].clip(lower=0)
    chuva = d["precip"].clip(lower=0)
    d["cape_log"] = np.log1p(cape)
    d["li"] = d["li"].clip(-12, 12)
    d["precip_log"] = np.log1p(chuva)
    d["cape_x_chuva"] = np.log1p(cape * chuva)            # proxy de Romps et al. (2014), em escala log
    d["cin_abs_log"] = np.log1p(d["cin"].abs())
    d["vt"] = d["t850"] - d["t500"]
    d["nivel0_km"] = d["nivel0"] / 1000.0
    d["rajada_log"] = np.log1p(d["rajada"].clip(lower=0))
    h = d["hora"].dt.hour + d["hora"].dt.minute / 60.0
    d["sen_hora"] = np.sin(2 * np.pi * h / 24.0)
    d["cos_hora"] = np.cos(2 * np.pi * h / 24.0)

    ant = _defasar(d, ["cape_log", "li", "precip_log"], 3)
    d["cape_log_l3"] = ant["cape_log__ant"].to_numpy()
    d["li_l3"] = ant["li__ant"].to_numpy()
    d["precip_log_l3"] = ant["precip_log__ant"].to_numpy()
    d["d_cape_3h"] = d["cape_log"] - d["cape_log_l3"]
    return d


# ----------------------------------------------------------------------------- baselines (sem treino)
def escore_pdea_r(d: pd.DataFrame) -> np.ndarray:
    """Escore 0-100 do método principal do painel (CAPE x chuva), exatamente como em risco_raio.py."""
    x = (d["cape"].clip(lower=0) * d["precip"].clip(lower=0)).to_numpy(dtype=float)
    return np.array([np.nan if np.isnan(v) else pontuar_capexp(float(v)) for v in x])


def fator_costeiro_do_painel() -> float:
    """Fator litorâneo atual do painel (config_regioes.json); 1,0 se ausente."""
    try:
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config_regioes.json").read_text(encoding="utf-8"))
        return float(cfg.get("correcao_costeira", {}).get("fator", 1.0))
    except (OSError, ValueError, TypeError):
        return 1.0


def escore_pdea_r_costeiro(d: pd.DataFrame, fator: float | None = None) -> np.ndarray:
    """PDEA-R com a correção litorânea: o produto CAPE x chuva das linhas com costeira == 1 é multiplicado pelo fator."""
    f = fator_costeiro_do_painel() if fator is None else fator
    x = (d["cape"].clip(lower=0) * d["precip"].clip(lower=0)).to_numpy(dtype=float)
    x = x * np.where(d["costeira"].to_numpy() == 1, f, 1.0)
    return np.array([np.nan if np.isnan(v) else pontuar_capexp(float(v)) for v in x])


def escore_pdea_h(d: pd.DataFrame) -> np.ndarray:
    """Escore 0-100 da heurística legada (método "pontos": CAPE, LI e CIN), como em risco_raio.py."""
    out = np.full(len(d), np.nan)
    for i, (c, l, n) in enumerate(zip(d["cape"].to_numpy(), d["li"].to_numpy(), d["cin"].to_numpy())):
        if np.isnan(c):
            continue
        s, _, _ = calcular_risco(float(c), None if np.isnan(l) else float(l), None if np.isnan(n) else float(n),
                                 ParametrosRisco(metodo="pontos"))
        out[i] = np.nan if s is None else s
    return out


def escore_cape_x_chuva(d: pd.DataFrame) -> np.ndarray:
    """Proxy de Romps et al. (2014): CAPE x taxa de precipitação (sem treino)."""
    return (d["cape"].clip(lower=0) * d["precip"].clip(lower=0)).to_numpy()


def escore_li_negativo(d: pd.DataFrame) -> np.ndarray:
    """Preditor de um só índice (Kunz, 2007): quanto mais negativo o LI, maior o escore."""
    return (-d["li"]).to_numpy()


BASELINES: dict[str, Callable[[pd.DataFrame], np.ndarray]] = {
    "PDEA-R (CAPE x chuva, escore do painel)": escore_pdea_r,
    "PDEA-H (heurística legada)": escore_pdea_h,
    "CAPE x chuva (Romps)": escore_cape_x_chuva,
    "Só LI (Kunz)": escore_li_negativo,
}


# ----------------------------------------------------------------------------- modelos treináveis
def criar_modelo(tipo: str, semente: int = 0):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    if tipo == "logistica":
        return Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("esc", StandardScaler()),
            ("lr", LogisticRegression(C=1.0, max_iter=1000)),
        ])
    if tipo == "gbm":
        return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200,
                                              min_samples_leaf=40, l2_regularization=1.0, random_state=semente)
    raise ValueError(f"Modelo desconhecido: {tipo}")


def exportar_logistica(modelo, colunas: list[str], limiar: float, caminho: str | Path, meta: dict | None = None) -> None:
    """Salva a regressão logística em JSON legível (para usar no painel sem scikit-learn)."""
    imp, esc, lr = modelo.named_steps["imp"], modelo.named_steps["esc"], modelo.named_steps["lr"]
    conteudo = {
        "tipo": "logistica",
        "atributos": colunas,
        "mediana_imputacao": [float(x) for x in imp.statistics_],
        "media": [float(x) for x in esc.mean_],
        "desvio": [float(x) for x in esc.scale_],
        "coeficientes": [float(x) for x in lr.coef_[0]],
        "intercepto": float(lr.intercept_[0]),
        "limiar_probabilidade": float(limiar),
        "meta": meta or {},
    }
    Path(caminho).write_text(json.dumps(conteudo, indent=2, ensure_ascii=False), encoding="utf-8")


def prever_probabilidade(arquivo_modelo: str | Path, atributos: dict[str, float | None]) -> float:
    """Probabilidade de raio na hora a partir do JSON exportado (usa só numpy)."""
    m = json.loads(Path(arquivo_modelo).read_text(encoding="utf-8"))
    z = m["intercepto"]
    for nome, med, mu, sd, coef in zip(m["atributos"], m["mediana_imputacao"], m["media"], m["desvio"], m["coeficientes"]):
        v = atributos.get(nome)
        v = med if v is None or (isinstance(v, float) and np.isnan(v)) else v
        z += coef * (v - mu) / sd
    return float(1.0 / (1.0 + np.exp(-z)))
