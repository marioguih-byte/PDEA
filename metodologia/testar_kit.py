"""Testes offline do kit de verificação (sem rede): metadados, contagem do GLM, download simulado, junção e CLI."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import baixar_glm as bg  # noqa: E402
import baixar_previsoes_runs as bp  # noqa: E402
import capitais as cap  # noqa: E402
import montar_tabela as mt  # noqa: E402


def teste_capitais() -> None:
    t = cap.tabela()
    assert len(t) == 27 and sum(x["costeira"] for x in t) == 11
    assert {x["regiao"] for x in t} == {"Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul"}
    por = {x["local"]: x for x in t}
    assert por["Fortaleza"]["costeira"] == 1 and por["Brasília"]["costeira"] == 0 and por["Belém"]["costeira"] == 0
    assert por["Rio Branco"]["utc_offset"] == -5 and por["Manaus"]["utc_offset"] == -4 and por["Recife"]["utc_offset"] == -3


def _nc_sintetico(caminho: Path, lats, lons, qual=None) -> bytes:
    import netCDF4

    with netCDF4.Dataset(caminho, "w", format="NETCDF4") as ds:
        ds.createDimension("number_of_flashes", len(lats))
        for nome, v in (("flash_lat", lats), ("flash_lon", lons)):
            var = ds.createVariable(nome, "f4", ("number_of_flashes",)); var[:] = v
        if qual is not None:
            q = ds.createVariable("flash_quality_flag", "i2", ("number_of_flashes",)); q[:] = qual
    return caminho.read_bytes()


def teste_glm() -> None:
    assert bg.bucket_da_data(date(2025, 4, 6)) == "noaa-goes16" and bg.bucket_da_data(date(2025, 4, 7)) == "noaa-goes19"
    assert bg.hora_do_arquivo("GLM-L2-LCFA/2026/253/14/OR_GLM-L2-LCFA_G19_s20262531437200_e2026253143740_c1.nc") == datetime(2026, 9, 10, 14)
    assert bg.hora_do_arquivo("sem-padrao.nc") is None
    # distância: 1 grau de latitude ~ 111,2 km
    d = bg.distancias_km(np.array([-3.7319]), np.array([-38.5267]), np.array([-2.7319]), np.array([-38.5267]))
    assert abs(d[0, 0] - 111.2) < 0.5
    t = cap.tabela(); lat0 = np.array([x["lat"] for x in t]); lon0 = np.array([x["lon"] for x in t])
    i = [x["local"] for x in t].index("Fortaleza")
    # flashes: 0 km (conta nos 3 raios), ~44 km ao norte (50 e 100), ~89 km (só 100), ~200 km (nenhum)
    lats = [lat0[i], lat0[i] + 0.40, lat0[i] + 0.80, lat0[i] + 1.80]
    lons = [lon0[i]] * 4
    with tempfile.TemporaryDirectory() as p:
        cont = _nc_sintetico(Path(p) / "a.nc", lats, lons, qual=[0, 0, 1, 0])
        c = bg.contar_arquivo(cont, lat0, lon0)
        assert list(c[i]) == [1, 2, 3], c[i]
        cq = bg.contar_arquivo(cont, lat0, lon0, filtro_qualidade=True)  # o flash a ~89 km tem qualidade ruim
        assert list(cq[i]) == [1, 2, 2], cq[i]
        vazio = bg.contar_arquivo(_nc_sintetico(Path(p) / "b.nc", [], []), lat0, lon0)
        assert vazio.sum() == 0


class _S3:
    """S3 simulado: lista e devolve arquivos NetCDF sintéticos (para testar o download em paralelo)."""

    def __init__(self, arquivos):
        self.arq = arquivos

    class R:
        def __init__(self, content):
            self.content, self.status_code = content, 200

        def raise_for_status(self):
            pass

    def get(self, url, params=None, timeout=None):
        if params and "list-type" in params:
            itens = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in sorted(self.arq) if k.startswith(params["prefix"]))
            return self.R(f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{itens}<IsTruncated>false</IsTruncated></ListBucketResult>'.encode())
        return self.R(self.arq[url.split(".amazonaws.com/")[1]])


def teste_glm_dia_em_paralelo() -> None:
    """Regressão: a leitura NetCDF/HDF5 não pode rodar em várias threads (derrubava o processo com 'Bus error')."""
    t = cap.tabela(); i = [x["local"] for x in t].index("Recife")
    lat0 = np.array([x["lat"] for x in t]); lon0 = np.array([x["lon"] for x in t])
    with tempfile.TemporaryDirectory() as p:
        conteudo = _nc_sintetico(Path(p) / "a.nc", [lat0[i], lat0[i] + 0.40, lat0[i] + 0.80], [lon0[i]] * 3)  # 30/50/100 km: 1, 2, 3
        arquivos = {f"GLM-L2-LCFA/2026/253/12/OR_GLM-L2-LCFA_G19_s2026253120{m:02d}{s:02d}0_e0_c0.nc": conteudo
                    for m in range(0, 10) for s in (0, 20, 40)}  # 30 arquivos na hora 12
    df = bg.processar_dia(_S3(arquivos), date(2026, 9, 10), t, filtro_qualidade=False, trabalhadores=8)
    h12 = df[(df["capital"] == "Recife") & (df["hora_utc"] == datetime(2026, 9, 10, 12))].iloc[0]
    assert (h12["flashes_30"], h12["flashes_50"], h12["flashes_100"], h12["n_arquivos"]) == (30, 60, 90, 30), dict(h12)
    assert df[df["hora_utc"] == datetime(2026, 9, 10, 13)]["n_arquivos"].eq(0).all()


class _Resp:
    def __init__(self, codigo, payload=None):
        self.status_code, self._p, self.text = codigo, payload, "x"

    def json(self):
        return self._p


class _Cliente:
    """Single Runs simulada: 'cape,precipitation' obrigatórias; opcionais podem falhar."""

    def __init__(self, opcionais="ok", sem_chuva=False):
        self.opcionais, self.sem_chuva, self.chamadas = opcionais, sem_chuva, []

    def get(self, url, params=None, timeout=None):
        self.chamadas.append(params)
        n = 24 * int(params["forecast_days"])
        ini = pd.Timestamp(params["run"]).normalize()
        tempos = [(ini + pd.Timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(n)]
        nloc = len(params["latitude"].split(","))
        if params["hourly"] == ",".join(bp.OBRIGATORIAS):
            bloco = {"hourly": {"time": tempos, "cape": [float(h) for h in range(n)], "precipitation": [None if self.sem_chuva else 1.0] * n}}
        elif self.opcionais == "erro400":
            return _Resp(400)
        else:
            bloco = {"hourly": {"time": tempos, **{v: [1.0] * n for v in bp.OPCIONAIS}}}
        return _Resp(200, [bloco] * nloc)


def teste_runs() -> pd.DataFrame:
    run = datetime(2026, 9, 10, 12)
    cl = _Cliente()
    df = bp.baixar_run(cl, "ecmwf_ifs", run, lead_max=72)
    assert cl.chamadas[0]["run"] == "2026-09-10T12:00" and cl.chamadas[0]["models"] == "ecmwf_ifs"
    assert cl.chamadas[0]["forecast_days"] == 4 and cl.chamadas[0]["timezone"] == "GMT"
    assert df["antecedencia_h"].min() == 0 and df["antecedencia_h"].max() == 72 and df["local"].nunique() == 27
    assert len(df) == 27 * 73
    # a série simulada começa 00 UTC do dia da execução: a hora 12 UTC (antecedência 0) tem cape == 12
    assert df[(df["local"] == "Fortaleza") & (df["antecedencia_h"] == 0)]["cape"].iloc[0] == 12.0
    assert {"li", "cin", "rajada", "nivel0", "t850", "t500"} <= set(df.columns)
    # opcionais indisponíveis: segue só com cape e chuva
    so = bp.baixar_run(_Cliente(opcionais="erro400"), "ecmwf_ifs", run, 72)
    assert "li" not in so.columns and so["precip"].notna().all()
    # sem chuva: erro explícito
    try:
        bp.baixar_run(_Cliente(sem_chuva=True), "ecmwf_ifs", run, 72)
        raise AssertionError("deveria falhar sem precipitação")
    except bp.ErroAPI:
        pass
    assert len(list(bp.execucoes(datetime(2026, 9, 1), datetime(2026, 9, 3), [0, 12]))) == 6
    return df


def teste_montar(runs: pd.DataFrame) -> pd.DataFrame:
    horas = pd.date_range("2026-09-10 12:00", periods=73, freq="h")
    linhas = []
    for l in cap.tabela():
        for k, h in enumerate(horas):
            linhas.append({"hora_utc": h, "capital": l["local"], "flashes_30": int(k % 5 == 0), "flashes_50": 1, "flashes_100": 2,
                           "n_arquivos": 100 if k == 7 else 180})  # a hora k=7 está incompleta
    glm = pd.DataFrame(linhas)
    t = mt.montar(runs, glm, 30, 160)
    assert len(t) == 27 * 72  # 73 horas menos a incompleta
    f = t[(t["local"] == "Manaus")].iloc[0]
    assert f["hora"] == f["hora_utc"] + pd.Timedelta(hours=-4) and f["regiao"] == "Norte"
    assert t[t["local"] == "Salvador"]["costeira"].iloc[0] == 1 and (t["flashes"] >= 0).all()
    assert mt.montar(runs, glm, 100, 160)["flashes"].eq(2).all()
    return t


def teste_cli(t: pd.DataFrame) -> None:
    # duas execuções para a mesma hora: sem --antecedencia deve recusar; com ela, usa uma por capital e hora
    rng = np.random.default_rng(0)
    a = pd.concat([t.assign(hora=t["hora"] + pd.Timedelta(days=k), hora_utc=t["hora_utc"] + pd.Timedelta(days=k)) for k in range(8)], ignore_index=True)
    b = a.copy(); b["antecedencia_h"] = b["antecedencia_h"] + 12; b["run_utc"] = "outra"
    tab = pd.concat([a, b], ignore_index=True)
    for c in ("li", "cin", "rajada", "nivel0", "t850", "t500"):
        tab[c] = rng.normal(0, 1, len(tab))
    tab["flashes"] = (rng.random(len(tab)) < 0.1 + 0.00005 * tab["cape"].clip(0)).astype(int)
    with tempfile.TemporaryDirectory() as p:
        csv = Path(p) / "tab.csv"; tab.to_csv(csv, index=False)
        base = [sys.executable, str(AQUI / "verificar_glm.py"), "--csv", str(csv), "--saida", str(Path(p) / "r"),
                "--bootstrap", "30", "--esquema", "dia", "--conjuntos", "nucleo", "--modelos", "logistica"]
        r1 = subprocess.run(base, capture_output=True, text=True)
        assert r1.returncode != 0 and "--antecedencia" in (r1.stderr + r1.stdout)
        r2 = subprocess.run(base + ["--antecedencia", "0-12"], capture_output=True, text=True)
        assert r2.returncode == 0, r2.stderr[-500:]
        for arq in ("metricas.csv", "por_estrato.csv", "ganho_sobre_climatologia.csv"):
            assert (Path(p) / "r" / arq).exists(), arq
        est = pd.read_csv(Path(p) / "r" / "por_estrato.csv", sep=";", decimal=",")
        assert {"regiao", "faixa_cape", "estacao"} <= set(est["estrato"].unique())


if __name__ == "__main__":
    teste_capitais(); print("capitais: OK")
    teste_glm(); print("GLM (contagem por raio, qualidade, bucket, nome): OK")
    teste_glm_dia_em_paralelo(); print("GLM (dia inteiro com download em paralelo, leitura sequencial): OK")
    df = teste_runs(); print("previsões por execução (antecedência, opcionais, erros): OK")
    tab = teste_montar(df); print("junção previsões x GLM (hora incompleta, hora local): OK")
    teste_cli(tab); print("verificar_glm.py (antecedência, estratos, climatologia): OK")
