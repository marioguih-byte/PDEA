"""Histórico das previsões do PDEA (SQLite).

Guarda, a cada execução do modelo (uma por hora cheia), as variáveis brutas de cada
unidade para as próximas horas. Como o histórico guarda CAPE, LI, CIN e os extras
(e não o score), é possível recalcular o score depois com outra calibração e comparar
com observações (por exemplo, raios do GLM/GOES) numa análise externa.

Local do arquivo: variável de ambiente ``PDEA_HISTORICO`` (padrão ``historico/pdea.sqlite``);
use ``desligado`` para não gravar nada.

Atenção: no Streamlit Community Cloud o disco é descartado quando o app reinicia. Para um
histórico permanente, rode ``python historico.py registrar`` a cada hora em uma máquina ou
servidor seu (cron / Agendador de Tarefas) ou aponte ``PDEA_HISTORICO`` para um volume persistente.

Linha de comando:
    python historico.py registrar [--modelo best_match]   busca o modelo e grava
    python historico.py status                            resumo do que há gravado
    python historico.py exportar --saida historico.csv    exporta (variáveis brutas + score)
    python historico.py limpar --manter-dias 90           apaga o que for mais antigo
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from analise import GatePrecipitacao, carregar_gate, extras_na_hora, janela_frente, multiplicador_do_gate, parametros_da_unidade
from modelos import MODELOS, TZ_BRASILIA, ErroBuscaModelo, buscar_modelo
from risco_raio import PARAMETROS_PADRAO, ParametrosRisco, calcular_risco
from unidades import ESTACOES

CAMINHO_PADRAO = Path(__file__).resolve().parent / "historico" / "pdea.sqlite"
HORAS_GRAVADAS = 48  # horas à frente gravadas em cada execução
COLUNAS_EXTRAS = ["precip", "rajada", "nivel0", "t850", "t500"]

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS execucoes (
    modelo TEXT NOT NULL, execucao TEXT NOT NULL, obtido_em TEXT NOT NULL,
    PRIMARY KEY (modelo, execucao)
);
CREATE TABLE IF NOT EXISTS previsoes (
    modelo TEXT NOT NULL, execucao TEXT NOT NULL, unidade TEXT NOT NULL,
    valido TEXT NOT NULL, horas INTEGER NOT NULL,
    cape REAL, li REAL, cin REAL, precip REAL, rajada REAL, nivel0 REAL, t850 REAL, t500 REAL,
    PRIMARY KEY (modelo, execucao, unidade, valido)
);
CREATE INDEX IF NOT EXISTS ix_previsoes_unidade_valido ON previsoes (unidade, valido);
"""


def caminho_do_historico() -> Optional[Path]:
    """Caminho configurado, ou ``None`` se o histórico está desligado."""
    valor = os.environ.get("PDEA_HISTORICO", "").strip()
    if valor.lower() in {"desligado", "off", "0", "false", "nao", "não"}:
        return None
    return Path(valor) if valor else CAMINHO_PADRAO


def _conectar(caminho: Path) -> sqlite3.Connection:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho, timeout=30)
    conexao.executescript(_ESQUEMA)
    return conexao


def _execucao_de(dados: dict[str, Any]) -> tuple[str, str]:
    """(execução = hora cheia da busca, instante exato da busca) em horário de Brasília."""
    try:
        obtido = datetime.fromisoformat(dados["_obtido_em"]).astimezone(TZ_BRASILIA)
    except (KeyError, ValueError):
        obtido = datetime.now(TZ_BRASILIA)
    return obtido.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M"), obtido.isoformat(timespec="seconds")


def registrar(dados: dict[str, Any], modelo_id: str, caminho: Optional[Path] = None) -> bool:
    """Grava a execução se ainda não existe (uma por modelo e hora cheia). Devolve ``True`` se gravou."""
    caminho = caminho or caminho_do_historico()
    if caminho is None:
        return False
    execucao, obtido_em = _execucao_de(dados)
    with closing(_conectar(caminho)) as conexao:
        if conexao.execute("SELECT 1 FROM execucoes WHERE modelo=? AND execucao=?", (modelo_id, execucao)).fetchone():
            return False
        linhas = []
        for estacao in ESTACOES:
            serie = dados.get(estacao["nome"], {})
            tempos, inicio = serie.get("tempos", []), serie.get("idx_atual", 0)
            for i in range(inicio, min(len(tempos), inicio + HORAS_GRAVADAS + 1)):
                def v(campo: str, i: int = i) -> Optional[float]:
                    valores = serie.get(campo, [])
                    return valores[i] if i < len(valores) else None

                linhas.append(
                    (modelo_id, execucao, estacao["nome"], tempos[i], i - inicio,
                     v("cape"), v("li"), v("cin"), v("precip"), v("rajada"), v("nivel0"), v("t850"), v("t500"))
                )
        with conexao:  # transação única: ou grava tudo ou nada
            conexao.executemany("INSERT OR IGNORE INTO previsoes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", linhas)
            conexao.execute("INSERT INTO execucoes VALUES (?,?,?)", (modelo_id, execucao, obtido_em))
    return True


def status(caminho: Optional[Path] = None) -> dict[str, Any]:
    """Resumo do histórico: execuções, período e tamanho do arquivo."""
    caminho = caminho or caminho_do_historico()
    vazio = {"ativo": caminho is not None, "caminho": str(caminho) if caminho else None,
             "execucoes": 0, "primeira": None, "ultima": None, "linhas": 0, "tamanho_mb": 0.0, "modelos": []}
    if caminho is None or not Path(caminho).exists():
        return vazio
    with closing(_conectar(caminho)) as conexao:
        n, primeira, ultima = conexao.execute("SELECT COUNT(*), MIN(execucao), MAX(execucao) FROM execucoes").fetchone()
        linhas = conexao.execute("SELECT COUNT(*) FROM previsoes").fetchone()[0]
        modelos = [m for (m,) in conexao.execute("SELECT DISTINCT modelo FROM execucoes ORDER BY modelo")]
    return {**vazio, "execucoes": n, "primeira": primeira, "ultima": ultima, "linhas": linhas,
            "tamanho_mb": round(Path(caminho).stat().st_size / 1e6, 2), "modelos": modelos}


def _com_score(
    df: pd.DataFrame,
    parametros: ParametrosRisco,
    regioes: Optional[dict[str, tuple[float, float]]],
    gate: Optional[GatePrecipitacao] = None,
) -> pd.DataFrame:
    """Acrescenta a coluna ``score`` recalculada com a calibração (e o gate de chuva) informados.

    O gate usa a chuva prevista na mesma execução entre a hora e ``janela_h`` horas à frente,
    igual ao cálculo do painel; por isso a consulta precisa trazer essas horas seguintes.
    """
    gate = carregar_gate() if gate is None else gate
    uf_de = {e["nome"]: e["uf"] for e in ESTACOES}
    chaves = ["modelo", "execucao", "unidade"]
    ordenado = df.sort_values(chaves + ["valido"], kind="stable")
    if gate.ufs:
        chuva = ordenado.groupby(chaves, sort=False)["precip"].transform(
            lambda s: pd.Series(janela_frente([None if pd.isna(v) else float(v) for v in s], gate.janela_h), index=s.index, dtype="float64")
        )
    else:
        chuva = pd.Series(float("nan"), index=ordenado.index)

    cache: dict[str, ParametrosRisco] = {}
    scores: list[Optional[float]] = []
    for linha, c in zip(ordenado.itertuples(index=False), chuva):
        uf = uf_de.get(linha.unidade, "")
        p = cache.setdefault(uf, parametros_da_unidade(parametros, uf, regioes))
        extras = None
        if p.usa_extras:
            serie = {campo: [getattr(linha, campo)] for campo in COLUNAS_EXTRAS}
            extras = extras_na_hora(serie, 0)
        cape = None if pd.isna(linha.cape) else linha.cape
        li = None if pd.isna(linha.li) else linha.li
        cin = None if pd.isna(linha.cin) else linha.cin
        mult = multiplicador_do_gate(None if pd.isna(c) else float(c), uf, gate)
        scores.append(calcular_risco(cape, li, cin, p, extras, mult)[0])
    return df.assign(score=pd.Series(scores, index=ordenado.index, dtype="float64"))


def _ler(sql: str, parametros: tuple, caminho: Optional[Path]) -> pd.DataFrame:
    caminho = caminho or caminho_do_historico()
    if caminho is None or not Path(caminho).exists():
        return pd.DataFrame()
    with closing(_conectar(caminho)) as conexao:
        return pd.read_sql_query(sql, conexao, params=parametros)


def serie_realizada(
    unidade: str,
    modelo_id: str,
    dias: int = 7,
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    regioes: Optional[dict[str, tuple[float, float]]] = None,
    caminho: Optional[Path] = None,
) -> pd.DataFrame:
    """Score "realizado" segundo o modelo: o valor da previsão de 0 h de cada execução, ao longo do tempo."""
    limite = (datetime.now(TZ_BRASILIA) - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M")
    janela = carregar_gate().janela_h  # horas seguintes necessárias para o gate de chuva
    df = _ler(
        "SELECT * FROM previsoes WHERE modelo=? AND unidade=? AND horas<=? AND valido>=? ORDER BY valido",
        (modelo_id, unidade, janela, limite), caminho,
    )
    if df.empty:
        return df
    df = _com_score(df, parametros, regioes)
    df = df[df["horas"] == 0].copy()  # só a previsão de 0 h de cada execução
    df["tempo"] = pd.to_datetime(df["valido"])
    return df


def horarios_com_revisoes(unidade: str, modelo_id: str, dias: int = 7, caminho: Optional[Path] = None) -> list[str]:
    """Horários válidos para os quais há 2 ou mais execuções (permitem ver como a previsão mudou)."""
    limite = (datetime.now(TZ_BRASILIA) - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M")
    df = _ler(
        "SELECT valido, COUNT(*) AS n FROM previsoes WHERE modelo=? AND unidade=? AND valido>=? "
        "GROUP BY valido HAVING n>=2 ORDER BY valido DESC",
        (modelo_id, unidade, limite), caminho,
    )
    return df["valido"].tolist() if not df.empty else []


def evolucao_previsao(
    unidade: str,
    modelo_id: str,
    valido: str,
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    regioes: Optional[dict[str, tuple[float, float]]] = None,
    caminho: Optional[Path] = None,
) -> pd.DataFrame:
    """Como o score previsto para um mesmo horário mudou entre as execuções (da mais antiga à mais recente)."""
    fim = (datetime.fromisoformat(valido) + timedelta(hours=carregar_gate().janela_h)).strftime("%Y-%m-%dT%H:%M")
    df = _ler(
        "SELECT * FROM previsoes WHERE modelo=? AND unidade=? AND valido>=? AND valido<=? ORDER BY execucao, valido",
        (modelo_id, unidade, valido, fim), caminho,
    )
    if df.empty:
        return df
    df = _com_score(df, parametros, regioes)
    df = df[df["valido"] == valido].copy()
    df["execucao_t"] = pd.to_datetime(df["execucao"])
    return df


def exportar_csv(
    saida: Any,
    modelo_id: Optional[str] = None,
    parametros: ParametrosRisco = PARAMETROS_PADRAO,
    regioes: Optional[dict[str, tuple[float, float]]] = None,
    caminho: Optional[Path] = None,
    dias: Optional[int] = None,
) -> int:
    """Escreve o histórico em CSV (separador ``;``, vírgula decimal, UTF-8 com BOM). Devolve o nº de linhas."""
    sql, args = "SELECT * FROM previsoes", []
    filtros = []
    if modelo_id:
        filtros.append("modelo=?")
        args.append(modelo_id)
    if dias:
        filtros.append("valido>=?")
        args.append((datetime.now(TZ_BRASILIA) - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M"))
    if filtros:
        sql += " WHERE " + " AND ".join(filtros)
    df = _ler(sql + " ORDER BY unidade, valido, execucao", tuple(args), caminho)
    if df.empty:
        df = pd.DataFrame(columns=["modelo", "execucao", "unidade", "valido", "horas", "cape", "li", "cin", *COLUNAS_EXTRAS, "score"])
    else:
        df = _com_score(df, parametros, regioes)
    df.to_csv(saida, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    return len(df)


def limpar(manter_dias: int, caminho: Optional[Path] = None) -> int:
    """Apaga previsões cujo horário válido é mais antigo que ``manter_dias``. Devolve as linhas removidas."""
    caminho = caminho or caminho_do_historico()
    if caminho is None or not Path(caminho).exists():
        return 0
    limite = (datetime.now(TZ_BRASILIA) - timedelta(days=manter_dias)).strftime("%Y-%m-%dT%H:%M")
    with closing(_conectar(caminho)) as conexao:
        with conexao:
            removidas = conexao.execute("DELETE FROM previsoes WHERE valido<?", (limite,)).rowcount
            conexao.execute("DELETE FROM execucoes WHERE execucao<?", (limite,))
        conexao.execute("VACUUM")
    return removidas


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)
    r = sub.add_parser("registrar", help="busca o modelo e grava no histórico")
    r.add_argument("--modelo", default="best_match", choices=list(MODELOS))
    sub.add_parser("status", help="resumo do histórico")
    e = sub.add_parser("exportar", help="exporta o histórico em CSV")
    e.add_argument("--saida", type=Path, required=True)
    e.add_argument("--modelo", choices=list(MODELOS))
    e.add_argument("--dias", type=int, help="só os últimos N dias")
    c = sub.add_parser("limpar", help="apaga o que for mais antigo")
    c.add_argument("--manter-dias", type=int, required=True)
    args = ap.parse_args(argv)

    if caminho_do_historico() is None and args.comando != "status":
        print("Histórico desligado (PDEA_HISTORICO=desligado).")
        return 1
    if args.comando == "registrar":
        try:
            dados = buscar_modelo(args.modelo)
        except ErroBuscaModelo as erro:
            print(f"Não foi possível buscar o modelo: {erro}")
            return 2
        gravou = registrar(dados, args.modelo)
        print(("Execução gravada." if gravou else "Esta execução (modelo + hora cheia) já estava gravada.")
              + f" Extras: {'sim' if dados.get('_extras') else 'não'}.")
    elif args.comando == "status":
        for chave, valor in status().items():
            print(f"{chave}: {valor}")
    elif args.comando == "exportar":
        print(f"{exportar_csv(args.saida, args.modelo, dias=args.dias)} linhas em {args.saida}")
    elif args.comando == "limpar":
        print(f"{limpar(args.manter_dias)} linhas removidas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
