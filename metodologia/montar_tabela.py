"""Junta as previsões com antecedência (baixar_previsoes_runs.py) e os flashes do GLM (baixar_glm.py).

Gera um CSV (um por raio) no formato do verificar_glm.py, com uma linha por capital x hora válida x execução:
  local, uf, regiao, costeira, modelo, run_utc, hora_utc, hora (local), antecedencia_h,
  cape, precip, [li, cin, rajada, nivel0, t850, t500], flashes, n_arquivos

Horas com menos de ``--min-arquivos`` arquivos do GLM são descartadas (hora incompleta não é hora sem raio).

Uso:
  python montar_tabela.py --runs runs/ --glm glm/ --raio 30 --saida tabela_30km.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capitais as cap  # noqa: E402


def montar(runs: pd.DataFrame, glm: pd.DataFrame, raio: int, min_arquivos: int = 160) -> pd.DataFrame:
    meta = pd.DataFrame(cap.tabela())[["local", "uf", "regiao", "costeira", "utc_offset"]]
    col = f"flashes_{raio}"
    if col not in glm.columns:
        raise SystemExit(f"Raio {raio} km ausente no GLM (colunas: {list(glm.columns)}).")
    g = glm.rename(columns={"capital": "local", col: "flashes"})[["hora_utc", "local", "flashes", "n_arquivos"]].copy()
    g["hora_utc"] = pd.to_datetime(g["hora_utc"])
    r = runs.copy(); r["hora_utc"] = pd.to_datetime(r["hora_utc"])
    t = r.merge(g, on=["local", "hora_utc"], how="inner").merge(meta, on="local", how="left")
    t = t[t["n_arquivos"] >= min_arquivos].copy()
    t["hora"] = t["hora_utc"] + pd.to_timedelta(t["utc_offset"], unit="h")  # hora local de cada capital
    return t.drop(columns=["utc_offset"]).sort_values(["local", "hora_utc", "antecedencia_h"]).reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", required=True); ap.add_argument("--glm", required=True)
    ap.add_argument("--raio", type=int, default=30, choices=[30, 50, 100])
    ap.add_argument("--min-arquivos", type=int, default=160)
    ap.add_argument("--saida", required=True)
    a = ap.parse_args()
    arq_r = sorted(Path(a.runs).glob("*.csv")); arq_g = sorted(Path(a.glm).glob("glm_*.csv"))
    if not arq_r or not arq_g:
        raise SystemExit("Sem arquivos em --runs ou --glm.")
    runs = pd.concat((pd.read_csv(f) for f in arq_r if f.name != "falhas.txt"), ignore_index=True)
    glm = pd.concat((pd.read_csv(f) for f in arq_g), ignore_index=True)
    t = montar(runs, glm, a.raio, a.min_arquivos)
    t.to_csv(a.saida, index=False)
    print(f"{len(t)} linhas, {t['local'].nunique()} capitais, antecedência {t['antecedencia_h'].min()}–{t['antecedencia_h'].max()} h, "
          f"frequência de raio = {(t['flashes'] >= 1).mean():.3f}; salvo em {a.saida}")


if __name__ == "__main__":
    main()
