"""Baixa previsões REAIS com antecedência (execuções arquivadas) da Single Runs API da Open-Meteo.

Para cada execução (ex.: 00 e 12 UTC de cada dia), pede as 27 capitais de uma vez e guarda, para cada hora válida,
a antecedência = hora válida - hora da execução. É isso que permite verificar a previsão que o painel de fato
mostra (+6 h, +24 h, +48 h), e não uma análise.

Disponibilidade (documentação da Open-Meteo, consultada em 8/10/2026): execuções arquivadas desde 02/04/2026 para a
maioria dos modelos e desde 14/03/2024 para o ECMWF IFS HRES 9 km (modelo ``ecmwf_ifs``). Cada execução só fica
disponível 4 a 6 h depois da hora de inicialização. A Previous Runs API NÃO serve aqui: ela não lista CAPE, LI nem CIN.

Obrigatórias (o escore do painel só precisa delas): cape, precipitation. Opcionais (tolerantes a falha):
lifted_index, convective_inhibition, wind_gusts_10m, freezing_level_height, temperature_850hPa, temperature_500hPa.

Uso:
  python baixar_previsoes_runs.py --testar                       # confere o acesso e quais variáveis voltam
  python baixar_previsoes_runs.py --inicio 2024-03-14 --fim 2025-03-13 --saida runs/ --modelo ecmwf_ifs
Retoma de onde parou (pula execuções já baixadas). Confira os limites do plano gratuito (uso não comercial).
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capitais as cap  # noqa: E402

API = "https://single-runs-api.open-meteo.com/v1/forecast"
OBRIGATORIAS = ["cape", "precipitation"]
OPCIONAIS = ["lifted_index", "convective_inhibition", "wind_gusts_10m", "freezing_level_height",
             "temperature_850hPa", "temperature_500hPa"]
RENOMEAR = {"cape": "cape", "precipitation": "precip", "lifted_index": "li", "convective_inhibition": "cin",
            "wind_gusts_10m": "rajada", "freezing_level_height": "nivel0", "temperature_850hPa": "t850",
            "temperature_500hPa": "t500"}


class ErroAPI(RuntimeError):
    pass


def requisitar(cliente, params: dict, tentativas: int = 4, espera: float = 2.0) -> list[dict]:
    """GET com tentativas (rede, 429 e 5xx são repetidos com espera crescente; outros 4xx abortam)."""
    ultimo = ""
    for t in range(1, tentativas + 1):
        try:
            r = cliente.get(API, params=params, timeout=60)
        except Exception as exc:  # noqa: BLE001 - qualquer falha de rede entra na repetição
            ultimo = f"rede: {exc}"
        else:
            if r.status_code == 200:
                dados = r.json()
                return dados if isinstance(dados, list) else [dados]
            ultimo = f"HTTP {r.status_code}: {r.text[:200]}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise ErroAPI(ultimo)
        if t < tentativas:
            time.sleep(espera * t)
    raise ErroAPI(ultimo)


def _params(modelo: str, run: datetime, variaveis: list[str], lead_max: int, locais: list[dict]) -> dict:
    return {
        "latitude": ",".join(str(l["lat"]) for l in locais), "longitude": ",".join(str(l["lon"]) for l in locais),
        "run": run.strftime("%Y-%m-%dT%H:%M"), "models": modelo, "hourly": ",".join(variaveis),
        "forecast_days": math.ceil((lead_max + 24) / 24), "timezone": "GMT", "cell_selection": "nearest",
    }


def baixar_run(cliente, modelo: str, run: datetime, lead_max: int = 72) -> pd.DataFrame:
    """Uma execução -> tabela longa (capital x hora válida) com a antecedência. Levanta ErroAPI se faltar cape/chuva."""
    locais = cap.tabela()
    blocos = requisitar(cliente, _params(modelo, run, OBRIGATORIAS, lead_max, locais))
    if len(blocos) != len(locais):
        raise ErroAPI(f"{len(blocos)} localidades recebidas, esperadas {len(locais)}")
    try:
        extras = requisitar(cliente, _params(modelo, run, OPCIONAIS, lead_max, locais))
        if len(extras) != len(locais):
            extras = None
    except ErroAPI:
        extras = None  # variáveis opcionais indisponíveis neste modelo: segue só com cape e chuva
    linhas = []
    for i, l in enumerate(locais):
        h = blocos[i]["hourly"]
        df = pd.DataFrame({"hora_utc": pd.to_datetime(h["time"])})
        for v in OBRIGATORIAS:
            df[RENOMEAR[v]] = h.get(v)
        if extras is not None:
            he = extras[i]["hourly"]
            if len(he.get("time", [])) == len(df):
                for v in OPCIONAIS:
                    if v in he:
                        df[RENOMEAR[v]] = he[v]
        df["antecedencia_h"] = ((df["hora_utc"] - pd.Timestamp(run)).dt.total_seconds() // 3600).astype(int)
        df = df[(df["antecedencia_h"] >= 0) & (df["antecedencia_h"] <= lead_max)]
        df.insert(0, "local", l["local"])
        df.insert(0, "run_utc", run.strftime("%Y-%m-%dT%H:%M"))
        df.insert(0, "modelo", modelo)
        linhas.append(df)
    out = pd.concat(linhas, ignore_index=True)
    if out["cape"].isna().all() or out["precip"].isna().all():
        raise ErroAPI("a resposta veio sem cape ou sem precipitação (modelo/execução sem essas variáveis?)")
    return out


def execucoes(inicio: datetime, fim: datetime, horas: list[int]):
    d = inicio
    while d <= fim:
        for h in horas:
            yield d.replace(hour=h, minute=0, second=0, microsecond=0)
        d += timedelta(days=1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inicio"); ap.add_argument("--fim")
    ap.add_argument("--runs", nargs="+", type=int, default=[0, 12], help="horas UTC das execuções (padrão: 0 12)")
    ap.add_argument("--modelo", default="ecmwf_ifs")
    ap.add_argument("--lead-max", type=int, default=72)
    ap.add_argument("--saida", default="runs")
    ap.add_argument("--pausa", type=float, default=1.0, help="segundos entre execuções (respeita o plano gratuito)")
    ap.add_argument("--testar", action="store_true")
    a = ap.parse_args()
    import requests
    cliente = requests.Session()
    if a.testar:
        run = (datetime.utcnow() - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        print(f"Teste: modelo {a.modelo}, execução {run:%Y-%m-%d %H:%M} UTC")
        df = baixar_run(cliente, a.modelo, run, a.lead_max)
        print(f"{len(df)} linhas. Variáveis com dados:", {c: f"{100 * df[c].notna().mean():.0f}%" for c in df.columns
                                                          if c in RENOMEAR.values()})
        return
    if not (a.inicio and a.fim):
        raise SystemExit("Informe --inicio e --fim (AAAA-MM-DD) ou use --testar.")
    pasta = Path(a.saida); pasta.mkdir(parents=True, exist_ok=True)
    falhas = pasta / "falhas.txt"
    ini, fim = datetime.fromisoformat(a.inicio), datetime.fromisoformat(a.fim)
    total = ok = 0
    for run in execucoes(ini, fim, a.runs):
        total += 1
        arq = pasta / f"{a.modelo}_{run:%Y%m%dT%H}.csv"
        if arq.exists():
            ok += 1
            continue
        try:
            baixar_run(cliente, a.modelo, run, a.lead_max).to_csv(arq, index=False)
            ok += 1
        except ErroAPI as e:
            with falhas.open("a", encoding="utf-8") as f:
                f.write(f"{run:%Y-%m-%dT%H} {e}\n")
        time.sleep(a.pausa)
    print(f"{ok}/{total} execuções disponíveis em {pasta.resolve()} (falhas em {falhas.name}, se houver).")


if __name__ == "__main__":
    main()
