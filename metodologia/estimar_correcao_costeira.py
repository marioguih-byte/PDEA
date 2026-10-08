"""Estima a correção litorânea do produto CAPE x chuva a partir do GLM.

Ideia: com o escore em log10(1 + CAPE x chuva), ajusta-se
    logit P(raio na hora) = b0 + bx * log10(1 + CAPE x chuva) + bc * costeira
e converte-se o efeito do litoral em um fator sobre o produto: fator = 10 ** (bc / bx).
Fator < 1 significa que, para o mesmo CAPE x chuva, o litoral tem menos raios. O IC 95% vem de bootstrap de dias.

Cuidados: 'costeira' se confunde com a cidade e com o clima da região. Use várias capitais litorâneas e várias do
interior, em todas as estações, e confira o IC. Se o IC incluir 1, os dados não sustentam uma correção.

Uso:
  python estimar_correcao_costeira.py --csv tabela.csv        # colunas: local,hora,cape,precip,flashes,costeira
  python estimar_correcao_costeira.py --demo                  # dados sintéticos (só testa o código)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdea_prob as pp  # noqa: E402
from verificar_glm import gerar_demo  # noqa: E402


def ajustar(x: np.ndarray, costeira: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Devolve (bx, bc, fator). Fator = nan se bx <= 0."""
    X = np.column_stack([x, costeira])
    lr = LogisticRegression(C=1e6, max_iter=2000).fit(X, y)
    bx, bc = float(lr.coef_[0][0]), float(lr.coef_[0][1])
    return bx, bc, (10 ** (bc / bx) if bx > 0 else float("nan"))


def estimar(d: pd.DataFrame, limiar_flashes: int = 1, so_com_chuva: bool = True, n_boot: int = 500, semente: int = 0) -> dict:
    prod = (d["cape"].clip(lower=0) * d["precip"].clip(lower=0)).to_numpy(float)
    ok = np.isfinite(prod) & (prod > 0 if so_com_chuva else True)
    x = np.log10(1 + prod[ok])
    c = d["costeira"].to_numpy()[ok].astype(int)
    y = (d["flashes"].fillna(0).to_numpy()[ok] >= limiar_flashes).astype(int)
    dias = d["hora"].dt.date.astype(str).to_numpy()[ok]
    if len(np.unique(c)) < 2 or len(np.unique(y)) < 2:
        raise SystemExit("Preciso de locais litorâneos e do interior, e de horas com e sem raio.")
    bx, bc, fator = ajustar(x, c, y)
    rng = np.random.default_rng(semente)
    uniq, inv = np.unique(dias, return_inverse=True)
    idx = [np.where(inv == k)[0] for k in range(len(uniq))]
    fs = []
    for _ in range(n_boot):
        ix = np.concatenate([idx[k] for k in rng.integers(0, len(uniq), len(uniq))])
        if len(np.unique(c[ix])) < 2 or len(np.unique(y[ix])) < 2:
            continue
        f = ajustar(x[ix], c[ix], y[ix])[2]
        if np.isfinite(f):
            fs.append(f)
    lo, hi = (float(np.percentile(fs, 2.5)), float(np.percentile(fs, 97.5))) if len(fs) > 20 else (float("nan"),) * 2
    return {"n_linhas": int(ok.sum()), "n_locais_costeiros": int(d.loc[d["costeira"] == 1, "local"].nunique()),
            "n_locais_interior": int(d.loc[d["costeira"] == 0, "local"].nunique()),
            "bx": bx, "bc": bc, "fator": fator, "ic95": [lo, hi], "ic_inclui_1": bool(lo <= 1.0 <= hi)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--limiar-flashes", type=int, default=1)
    ap.add_argument("--incluir-sem-chuva", action="store_true", help="inclui as horas em que CAPE x chuva = 0")
    ap.add_argument("--bootstrap", type=int, default=500)
    ap.add_argument("--saida", help="grava o resultado em JSON")
    a = ap.parse_args()
    if a.demo:
        d = gerar_demo(0)
        print("MODO DEMO: dados sintéticos; o resultado NÃO é científico.\n")
    elif a.csv:
        d = pd.read_csv(a.csv)
        d["hora"] = pd.to_datetime(d["hora"])
    else:
        raise SystemExit("Informe --csv ou --demo.")
    if "costeira" not in d.columns:
        raise SystemExit("A tabela precisa da coluna 'costeira' (1 = litorânea, 0 = interior).")
    r = estimar(d, a.limiar_flashes, not a.incluir_sem_chuva, a.bootstrap)
    print(f"Locais: {r['n_locais_costeiros']} litorâneos, {r['n_locais_interior']} do interior; {r['n_linhas']} horas usadas.")
    print(f"Efeito do litoral: fator = {r['fator']:.2f} (IC 95%: {r['ic95'][0]:.2f} a {r['ic95'][1]:.2f}) sobre o produto CAPE x chuva.")
    atual = pp.fator_costeiro_do_painel()
    print(f"Fator atual do painel: {atual:.2f} " + ("(dentro do IC)" if r["ic95"][0] <= atual <= r["ic95"][1] else "(FORA do IC)"))
    if r["ic_inclui_1"]:
        print("O IC inclui 1,0: os dados não sustentam, sozinhos, uma correção litorânea.")
    if r["n_locais_costeiros"] < 4 or r["n_locais_interior"] < 4:
        print("Aviso: poucos locais por grupo; o efeito do litoral se confunde com o da cidade.")
    if a.saida:
        Path(a.saida).write_text(json.dumps(r, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
