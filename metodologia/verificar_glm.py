"""Verificação do PDEA-R (CAPE x chuva) e de preditores alternativos contra as descargas do GLM.

Protocolo (para o artigo):
  1. Tabela única local x hora com as variáveis do Open-Meteo e o número de flashes do GLM.
  2. Evento = hora com >= N flashes (padrão 1; use também 10) dentro do raio escolhido.
  3. Validação cruzada que NÃO mistura períodos vizinhos: deixa um grupo de fora por vez
     (mês, cidade ou, no mínimo, blocos de dias). Treino, limiar e calibração só veem o treino.
  4. Todos os preditores passam pelo MESMO procedimento: calibração de Platt e limiar que maximiza
     o CSI, ambos escolhidos em escores fora da amostra dentro do treino.
  5. Métricas com IC 95% por bootstrap de dias inteiros: AUC, AP, POD, FAR, CSI, viés, HSS, PSS, BSS.
  6. Referências sem treino: PDEA-R (escore do painel), PDEA-H (heurística legada), CAPE x chuva bruto e só LI (Kunz, 2007).

Uso:
  python verificar_glm.py --csv tabela.csv --saida resultados/ --esquema mes
  python verificar_glm.py --demo            # dados sintéticos: só para testar o código, NÃO são resultados
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import GroupKFold, LeaveOneGroupOut

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdea_prob as pp  # noqa: E402


# ----------------------------------------------------------------------------- métricas
def contingencia(obs: np.ndarray, prev: np.ndarray) -> tuple[int, int, int, int]:
    obs, prev = obs.astype(bool), prev.astype(bool)
    return (int((obs & prev).sum()), int((~obs & prev).sum()), int((obs & ~prev).sum()), int((~obs & ~prev).sum()))


def metricas_binarias(obs: np.ndarray, prev: np.ndarray) -> dict[str, float]:
    a, b, c, d = contingencia(obs, prev)
    div = lambda x, y: float(x / y) if y > 0 else float("nan")  # noqa: E731
    den_hss = (a + c) * (c + d) + (a + b) * (b + d)
    return {
        "POD": div(a, a + c),
        "FAR": div(b, a + b),
        "CSI": div(a, a + b + c),
        "vies": div(a + b, a + c),
        "HSS": div(2 * (a * d - b * c), den_hss),
        "PSS": div(a, a + c) - div(b, b + d) if (a + c) > 0 and (b + d) > 0 else float("nan"),
    }


def escolher_limiar(obs: np.ndarray, escore: np.ndarray, maximos: int = 200) -> float:
    """Limiar que maximiza o CSI (equivale a maximizar o F1) entre candidatos por quantis."""
    v = escore[np.isfinite(escore)]
    if v.size == 0 or obs.sum() == 0:
        return float("inf")
    cand = np.unique(np.quantile(v, np.linspace(0.02, 0.995, maximos)))
    melhor, lim = -1.0, cand[-1]
    for t in cand:
        a, b, c, _ = contingencia(obs, escore >= t)
        csi = a / (a + b + c) if (a + b + c) > 0 else 0.0
        if csi > melhor:
            melhor, lim = csi, t
    return float(lim)


def platt(escore_treino: np.ndarray, obs_treino: np.ndarray):
    """Calibração de Platt: p = sigmoide(a + b*s); devolve uma função."""
    ok = np.isfinite(escore_treino)
    if obs_treino[ok].min() == obs_treino[ok].max():
        p0 = float(obs_treino[ok].mean())
        return lambda s: np.full_like(np.asarray(s, dtype=float), p0)
    mu, sd = escore_treino[ok].mean(), escore_treino[ok].std() or 1.0
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(((escore_treino[ok] - mu) / sd).reshape(-1, 1), obs_treino[ok])
    return lambda s: lr.predict_proba(((np.nan_to_num(np.asarray(s, dtype=float), nan=mu) - mu) / sd).reshape(-1, 1))[:, 1]


# ----------------------------------------------------------------------------- validação cruzada
def dividir(d: pd.DataFrame, esquema: str):
    dia = d["hora"].dt.date.astype(str)
    if esquema == "mes":
        g = d["hora"].dt.strftime("%Y-%m")
        if g.nunique() < 2:
            print("AVISO: só há um mês; usando blocos de dias (tende a ser otimista).", file=sys.stderr)
            esquema = "dia"
    if esquema == "mes":
        return list(LeaveOneGroupOut().split(d, groups=g)), esquema
    if esquema == "cidade":
        if d["local"].nunique() < 2:
            raise SystemExit("O esquema 'cidade' precisa de pelo menos 2 locais.")
        return list(LeaveOneGroupOut().split(d, groups=d["local"])), esquema
    k = min(5, dia.nunique())
    return list(GroupKFold(n_splits=k).split(d, groups=dia)), "dia"


def escore_oof_treino(modelo, X: np.ndarray, y: np.ndarray, grupos: np.ndarray) -> np.ndarray:
    """Escores fora da amostra dentro do treino (por blocos de dias), para calibrar e escolher limiar."""
    oof = np.full(len(y), np.nan)
    k = min(3, len(np.unique(grupos)))
    if k < 2:
        return oof
    for tr, va in GroupKFold(n_splits=k).split(X, y, grupos):
        if y[tr].min() == y[tr].max():
            continue
        m = clone(modelo).fit(X[tr], y[tr])
        oof[va] = m.predict_proba(X[va])[:, 1]
    return oof


def rodar_validacao(d: pd.DataFrame, y: np.ndarray, conjuntos: list[str], modelos: list[str], esquema: str, semente: int):
    folds, esquema = dividir(d, esquema)
    dias = d["hora"].dt.date.astype(str).to_numpy()
    n = len(d)
    preds: dict[str, dict[str, np.ndarray]] = {}

    def guardar(nome: str):
        preds[nome] = {"escore": np.full(n, np.nan), "p": np.full(n, np.nan), "prev": np.zeros(n, bool), "clim": np.full(n, np.nan)}

    for nome in pp.BASELINES:
        guardar(nome)
    base_escore = {nome: f(d) for nome, f in pp.BASELINES.items()}
    espec = [(f"PDEA-P {t} [{c}]", t, c) for t in modelos for c in conjuntos]
    for nome, _, _ in espec:
        guardar(nome)

    for tr, te in folds:
        clim = y[tr].mean()
        for nome, s in base_escore.items():
            ok_tr = np.isfinite(s[tr])
            cal = platt(s[tr][ok_tr], y[tr][ok_tr])
            lim = escolher_limiar(y[tr][ok_tr], s[tr][ok_tr])
            st = s[te]
            preds[nome]["escore"][te] = st
            preds[nome]["p"][te] = cal(st)
            preds[nome]["prev"][te] = np.nan_to_num(st, nan=-np.inf) >= lim
            preds[nome]["clim"][te] = clim
        for nome, tipo, conj in espec:
            cols = pp.CONJUNTOS[conj]
            X = d[cols].to_numpy(dtype=float)
            mod = pp.criar_modelo(tipo, semente)
            if y[tr].min() == y[tr].max():
                continue
            oof = escore_oof_treino(mod, X[tr], y[tr], dias[tr])
            ok = np.isfinite(oof)
            cal = platt(oof[ok], y[tr][ok]) if ok.any() else (lambda s: np.asarray(s, dtype=float))
            lim = escolher_limiar(y[tr][ok], oof[ok]) if ok.any() else 0.5
            ajustado = clone(mod).fit(X[tr], y[tr])
            st = ajustado.predict_proba(X[te])[:, 1]
            preds[nome]["escore"][te] = st
            preds[nome]["p"][te] = cal(st)
            preds[nome]["prev"][te] = st >= lim
            preds[nome]["clim"][te] = clim
    return preds, esquema


# ----------------------------------------------------------------------------- resumo com bootstrap
def _estatisticas(obs, escore, p, prev, clim, dia_idx=None) -> dict[str, float]:
    ok = np.isfinite(escore)
    obs, escore, p, prev, clim = obs[ok], escore[ok], p[ok], prev[ok], clim[ok]
    out: dict[str, float] = {}
    if obs.min() == obs.max():
        return {k: float("nan") for k in ["AUC", "AP", "BSS", "POD", "FAR", "CSI", "vies", "HSS", "PSS"]}
    out["AUC"] = float(roc_auc_score(obs, escore))
    out["AP"] = float(average_precision_score(obs, escore))
    bs, bs_ref = np.mean((p - obs) ** 2), np.mean((clim - obs) ** 2)
    out["BSS"] = float(1 - bs / bs_ref) if bs_ref > 0 else float("nan")
    out.update(metricas_binarias(obs, prev))
    return out


def resumir(d: pd.DataFrame, y: np.ndarray, preds, n_boot: int, semente: int) -> pd.DataFrame:
    rng = np.random.default_rng(semente)
    dias = d["hora"].dt.date.astype(str).to_numpy()
    uniq, inv = np.unique(dias, return_inverse=True)
    idx_por_dia = [np.where(inv == k)[0] for k in range(len(uniq))]
    amostras = [np.concatenate([idx_por_dia[k] for k in rng.integers(0, len(uniq), len(uniq))]) for _ in range(n_boot)]
    linhas = []
    for nome, pr in preds.items():
        base = _estatisticas(y, pr["escore"], pr["p"], pr["prev"], pr["clim"])
        boots = [_estatisticas(y[ix], pr["escore"][ix], pr["p"][ix], pr["prev"][ix], pr["clim"][ix]) for ix in amostras]
        linha = {"preditor": nome}
        for k, v in base.items():
            vals = np.array([b[k] for b in boots], dtype=float)
            vals = vals[np.isfinite(vals)]
            linha[k] = v
            linha[f"{k}_lo"] = float(np.percentile(vals, 2.5)) if vals.size > 20 else float("nan")
            linha[f"{k}_hi"] = float(np.percentile(vals, 97.5)) if vals.size > 20 else float("nan")
        linhas.append(linha)
    return pd.DataFrame(linhas)


def ganho_sobre_referencia(d, y, preds, referencia: str, n_boot: int, semente: int) -> pd.DataFrame:
    """Diferença de AUC/AP/CSI de cada preditor sobre a referência, com IC por bootstrap de dias."""
    rng = np.random.default_rng(semente + 1)
    dias = d["hora"].dt.date.astype(str).to_numpy()
    uniq, inv = np.unique(dias, return_inverse=True)
    idx_por_dia = [np.where(inv == k)[0] for k in range(len(uniq))]
    amostras = [np.concatenate([idx_por_dia[k] for k in rng.integers(0, len(uniq), len(uniq))]) for _ in range(n_boot)]
    ref = preds[referencia]
    linhas = []
    for nome, pr in preds.items():
        if nome == referencia:
            continue
        difs = {"AUC": [], "AP": [], "CSI": []}
        for ix in amostras:
            a = _estatisticas(y[ix], pr["escore"][ix], pr["p"][ix], pr["prev"][ix], pr["clim"][ix])
            b = _estatisticas(y[ix], ref["escore"][ix], ref["p"][ix], ref["prev"][ix], ref["clim"][ix])
            for k in difs:
                difs[k].append(a[k] - b[k])
        linha = {"preditor": nome, "referencia": referencia}
        a0 = _estatisticas(y, pr["escore"], pr["p"], pr["prev"], pr["clim"])
        b0 = _estatisticas(y, ref["escore"], ref["p"], ref["prev"], ref["clim"])
        for k, v in difs.items():
            v = np.array(v, dtype=float)
            v = v[np.isfinite(v)]
            linha[f"d{k}"] = a0[k] - b0[k]
            linha[f"d{k}_lo"] = float(np.percentile(v, 2.5)) if v.size > 20 else float("nan")
            linha[f"d{k}_hi"] = float(np.percentile(v, 97.5)) if v.size > 20 else float("nan")
        linhas.append(linha)
    return pd.DataFrame(linhas)


def escala_diaria(d: pd.DataFrame, y: np.ndarray, preds) -> pd.DataFrame:
    """AUC e AP do pico diário do escore contra 'houve raio no dia' (escala de quem lê o painel)."""
    chave = d["local"].astype(str) + "|" + d["hora"].dt.date.astype(str)
    linhas = []
    for nome, pr in preds.items():
        t = pd.DataFrame({"k": chave, "s": pr["escore"], "y": y}).dropna(subset=["s"])
        g = t.groupby("k").agg(s=("s", "max"), y=("y", "max"))
        if g["y"].nunique() < 2:
            continue
        linhas.append({"preditor": nome, "n_dias_cidade": len(g), "dias_com_raio": int(g["y"].sum()),
                       "AUC_diario": float(roc_auc_score(g["y"], g["s"])), "AP_diario": float(average_precision_score(g["y"], g["s"]))})
    return pd.DataFrame(linhas)


def tabela_confiabilidade(y, p, n_bins: int = 10) -> pd.DataFrame:
    ok = np.isfinite(p)
    df = pd.DataFrame({"p": p[ok], "y": y[ok]})
    df["faixa"] = pd.qcut(df["p"], q=min(n_bins, df["p"].nunique()), duplicates="drop")
    return df.groupby("faixa", observed=True).agg(p_prevista=("p", "mean"), freq_observada=("y", "mean"), n=("y", "size")).reset_index(drop=True)


# ----------------------------------------------------------------------------- dados sintéticos (teste do código)
def gerar_demo(semente: int = 0) -> pd.DataFrame:
    """Dados SINTÉTICOS, só para testar o fluxo. Não representam a atmosfera nem o GLM."""
    rng = np.random.default_rng(semente)
    linhas = []
    for mes in ["2025-09", "2026-09"]:
        horas = pd.date_range(f"{mes}-01", periods=30 * 24, freq="h")
        for local in ["Cidade A", "Cidade B", "Cidade C", "Cidade D", "Cidade E", "Cidade F"]:
            costeira = 1 if local in ("Cidade A", "Cidade D") else 0
            regime = np.repeat(rng.normal(0, 1, 30), 24)
            diurno = np.tile(np.clip(np.sin((np.arange(24) - 8) / 24 * 2 * np.pi), 0, None), 30)
            cape = np.clip(rng.gamma(2, 120, len(horas)) * (0.4 + 1.2 * diurno) * np.exp(0.5 * regime), 0, 4500)
            li = np.clip(3 - cape / 450 + rng.normal(0, 2, len(horas)), -10, 8)
            chuva = np.clip(rng.exponential(0.3, len(horas)) * np.exp(0.6 * regime + 0.5 * diurno) - 0.1, 0, 20)
            cin = -np.abs(rng.gamma(2, 25, len(horas)))
            z = -4.3 - 1.0 * costeira + 0.9 * np.log1p(cape) / 3 - 0.18 * li + 0.5 * np.log1p(chuva) + 0.9 * regime * (rng.random(len(horas)) < 0.5)
            ocorre = rng.random(len(horas)) < 1 / (1 + np.exp(-z))
            flashes = np.where(ocorre, rng.integers(1, 60, len(horas)), 0)
            linhas.append(pd.DataFrame({
                "local": local, "hora": horas, "cape": cape, "li": li, "cin": cin, "precip": chuva,
                "rajada": rng.gamma(4, 6, len(horas)), "nivel0": rng.normal(4300, 250, len(horas)),
                "t850": rng.normal(16, 3, len(horas)), "t500": rng.normal(-8, 2, len(horas)), "flashes": flashes,
                "costeira": costeira}))
    return pd.concat(linhas, ignore_index=True)


# ----------------------------------------------------------------------------- principal
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="tabela local x hora (colunas: local,hora,cape,li,cin,precip,rajada,nivel0,t850,t500,flashes; opcional: costeira = 0/1)")
    ap.add_argument("--demo", action="store_true", help="usa dados sintéticos para testar o código")
    ap.add_argument("--saida", default="resultados", help="pasta de saída")
    ap.add_argument("--esquema", choices=["mes", "cidade", "dia"], default="mes")
    ap.add_argument("--limiar-flashes", type=int, default=1, help="flashes/hora que definem o evento")
    ap.add_argument("--conjuntos", nargs="+", default=["nucleo", "nucleo_cin", "completo", "defasagem"], choices=list(pp.CONJUNTOS))
    ap.add_argument("--modelos", nargs="+", default=["logistica", "gbm"], choices=["logistica", "gbm"])
    ap.add_argument("--bootstrap", type=int, default=500)
    ap.add_argument("--semente", type=int, default=0)
    a = ap.parse_args()

    if a.demo:
        bruto = gerar_demo(a.semente)
        print("MODO DEMO: dados sintéticos; os números abaixo NÃO são resultados científicos.\n")
    elif a.csv:
        bruto = pd.read_csv(a.csv)
    else:
        raise SystemExit("Informe --csv ou --demo.")

    d = pp.construir_atributos(bruto)
    if "flashes" not in d.columns:
        raise SystemExit("A tabela precisa da coluna 'flashes' (contagem do GLM por hora).")
    y = (d["flashes"].fillna(0).to_numpy() >= a.limiar_flashes).astype(int)
    if "costeira" in d.columns:  # coluna opcional: 1 = local litorâneo; ativa a referência com correção litorânea
        pp.BASELINES["PDEA-R com correção litorânea"] = pp.escore_pdea_r_costeiro
    print(f"{len(d)} linhas, {d['local'].nunique()} locais, {d['hora'].dt.date.nunique()} dias; "
          f"frequência do evento = {y.mean():.3f} (>= {a.limiar_flashes} flash/h)")

    preds, esquema = rodar_validacao(d, y, a.conjuntos, a.modelos, a.esquema, a.semente)
    resumo = resumir(d, y, preds, a.bootstrap, a.semente)
    ganho = ganho_sobre_referencia(d, y, preds, "PDEA-R (CAPE x chuva, escore do painel)", a.bootstrap, a.semente)
    diario = escala_diaria(d, y, preds)

    out = Path(a.saida)
    out.mkdir(parents=True, exist_ok=True)
    resumo.to_csv(out / "metricas.csv", index=False, sep=";", decimal=",")
    ganho.to_csv(out / "ganho_sobre_pdea_r.csv", index=False, sep=";", decimal=",")
    diario.to_csv(out / "escala_diaria.csv", index=False, sep=";", decimal=",")

    # confiabilidade e modelo final do melhor preditor probabilístico (por AUC)
    treinaveis = resumo[resumo["preditor"].str.startswith("PDEA-P")].sort_values("AUC", ascending=False)
    if len(treinaveis):
        melhor = treinaveis.iloc[0]["preditor"]
        tabela_confiabilidade(y, preds[melhor]["p"]).to_csv(out / "confiabilidade.csv", index=False, sep=";", decimal=",")
        # modelo final: logística no melhor conjunto (exportável para o painel)
        logis = resumo[resumo["preditor"].str.contains("logistica")].sort_values("AUC", ascending=False)
        if len(logis):
            conj = logis.iloc[0]["preditor"].split("[")[1].rstrip("]")
            cols = pp.CONJUNTOS[conj]
            X = d[cols].to_numpy(float)
            mod = pp.criar_modelo("logistica", a.semente).fit(X, y)
            oof = escore_oof_treino(pp.criar_modelo("logistica", a.semente), X, y, d["hora"].dt.date.astype(str).to_numpy())
            ok = np.isfinite(oof)
            lim = escolher_limiar(y[ok], oof[ok]) if ok.any() else 0.5
            pp.exportar_logistica(mod, cols, lim, out / "modelo_pdea_p.json",
                                  {"conjunto": conj, "limiar_flashes": a.limiar_flashes, "esquema_validacao": esquema,
                                   "n_linhas": int(len(d)), "freq_evento": float(y.mean()),
                                   "aviso": "coeficientes ajustados a este conjunto de dados; reverifique em outro período"})

    cols_show = ["preditor", "AUC", "AUC_lo", "AUC_hi", "AP", "CSI", "POD", "FAR", "BSS"]
    with pd.option_context("display.width", 200, "display.max_columns", 20, "display.float_format", "{:.2f}".format):
        print(f"\nEsquema de validação: {esquema}\n")
        print(resumo[cols_show].to_string(index=False))
        print("\nGanho sobre a PDEA-R (IC 95% por bootstrap de dias):")
        print(ganho[["preditor", "dAUC", "dAUC_lo", "dAUC_hi", "dCSI", "dCSI_lo", "dCSI_hi"]].to_string(index=False))
        print("\nEscala diária (pico do escore):")
        print(diario.to_string(index=False))
    print(f"\nArquivos salvos em {out.resolve()}")


if __name__ == "__main__":
    main()
