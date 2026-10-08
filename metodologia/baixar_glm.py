"""Conta flashes do GLM (GOES-East) por capital e por hora UTC, nos raios de 30, 50 e 100 km.

Fonte: arquivos de nível 2 (GLM-L2-LCFA) do repositório público da NOAA na Amazon S3, um arquivo a cada 20 s
(cerca de 180 por hora). O satélite muda com a data: GOES-16 era o GOES-East até 06/04/2025 e o GOES-19 passou a
ser o GOES-East em 07/04/2025; o script escolhe o bucket pela data (``noaa-goes16`` ou ``noaa-goes19``).
Cada arquivo é atribuído à hora do seu início (nome do arquivo), o que erra no máximo 20 s nas bordas da hora.

Saída: um CSV por dia (retoma de onde parou) com
  hora_utc, capital, flashes_30, flashes_50, flashes_100, n_arquivos
``n_arquivos`` é o número de arquivos lidos na hora: horas com poucos arquivos (< ~160 de ~180) estão incompletas e
devem ser descartadas na montagem da tabela (a ausência de arquivos NÃO é ausência de raios).

Uso:
  python baixar_glm.py --testar                          # lista uma hora e lê um arquivo
  python baixar_glm.py --inicio 2025-09-01 --fim 2025-09-30 --saida glm/
Dependências: requests, numpy, pandas, netCDF4.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capitais as cap  # noqa: E402

RAIOS_KM = (30, 50, 100)
TROCA_GOES19 = date(2025, 4, 7)
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
PADRAO_NOME = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})\d_")


def bucket_da_data(d: date) -> str:
    return "noaa-goes19" if d >= TROCA_GOES19 else "noaa-goes16"


def hora_do_arquivo(chave: str) -> datetime | None:
    """Início do arquivo (UTC, truncado à hora) a partir de ``..._sAAAADDDHHMMSSt_...``."""
    m = PADRAO_NOME.search(chave)
    if not m:
        return None
    ano, doy, hh = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return datetime(ano, 1, 1) + timedelta(days=doy - 1, hours=hh)


def listar(cliente, bucket: str, prefixo: str) -> list[str]:
    """Chaves do prefixo (ListObjectsV2 anônimo, com paginação)."""
    chaves, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefixo}
        if token:
            params["continuation-token"] = token
        r = cliente.get(f"https://{bucket}.s3.amazonaws.com/", params=params, timeout=60)
        r.raise_for_status()
        raiz = ET.fromstring(r.content)
        chaves += [c.findtext("s3:Key", namespaces=NS) for c in raiz.findall("s3:Contents", NS)]
        if raiz.findtext("s3:IsTruncated", namespaces=NS) == "true":
            token = raiz.findtext("s3:NextContinuationToken", namespaces=NS)
        else:
            return [c for c in chaves if c and c.endswith(".nc")]


def distancias_km(lat: np.ndarray, lon: np.ndarray, lat0: np.ndarray, lon0: np.ndarray) -> np.ndarray:
    """Distância de grande círculo (km) entre cada flash e cada capital: matriz (n_flashes, n_capitais)."""
    R = 6371.0088
    p1, p2 = np.radians(lat)[:, None], np.radians(lat0)[None, :]
    dl = np.radians(lon0)[None, :] - np.radians(lon)[:, None]
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def contar_arquivo(conteudo: bytes, lat0: np.ndarray, lon0: np.ndarray, filtro_qualidade: bool = False) -> np.ndarray:
    """Flashes de um arquivo GLM por capital e raio: matriz (n_capitais, len(RAIOS_KM))."""
    import netCDF4

    out = np.zeros((len(lat0), len(RAIOS_KM)), dtype=int)
    with netCDF4.Dataset("glm.nc", mode="r", memory=conteudo) as ds:
        lat = np.ma.filled(ds.variables["flash_lat"][:], np.nan).astype(float)
        lon = np.ma.filled(ds.variables["flash_lon"][:], np.nan).astype(float)
        ok = np.isfinite(lat) & np.isfinite(lon)
        if filtro_qualidade and "flash_quality_flag" in ds.variables:
            ok &= np.ma.filled(ds.variables["flash_quality_flag"][:], 1) == 0  # 0 = boa qualidade
    if not ok.any():
        return out
    dist = distancias_km(lat[ok], lon[ok], lat0, lon0)
    for j, r in enumerate(RAIOS_KM):
        out[:, j] = (dist <= r).sum(axis=0)
    return out


def processar_dia(cliente, dia: date, locais: list[dict], filtro_qualidade: bool, trabalhadores: int = 16) -> pd.DataFrame:
    bucket = bucket_da_data(dia)
    lat0 = np.array([l["lat"] for l in locais]); lon0 = np.array([l["lon"] for l in locais])
    doy = dia.timetuple().tm_yday
    linhas = []
    for hh in range(24):
        chaves = listar(cliente, bucket, f"GLM-L2-LCFA/{dia.year}/{doy:03d}/{hh:02d}/")
        hora = datetime(dia.year, dia.month, dia.day, hh)
        soma = np.zeros((len(locais), len(RAIOS_KM)), dtype=int)
        lidos = 0

        def baixar(chave: str) -> bytes:
            r = cliente.get(f"https://{bucket}.s3.amazonaws.com/{chave}", timeout=120)
            r.raise_for_status()
            return r.content

        # Download em paralelo; leitura NetCDF/HDF5 um arquivo por vez (a biblioteca não é segura para várias threads).
        with ThreadPoolExecutor(trabalhadores) as ex:
            conteudos = list(ex.map(lambda c: _seguro(baixar, c), chaves))
        for conteudo in conteudos:
            if conteudo is None:
                continue
            res = _seguro(lambda c: contar_arquivo(c, lat0, lon0, filtro_qualidade), conteudo)
            if res is not None:
                soma += res
                lidos += 1
        for i, l in enumerate(locais):
            linhas.append({"hora_utc": hora, "capital": l["local"], **{f"flashes_{r}": int(soma[i, j]) for j, r in enumerate(RAIOS_KM)},
                           "n_arquivos": lidos})
    return pd.DataFrame(linhas)


def _seguro(f, chave):
    try:
        return f(chave)
    except Exception:  # noqa: BLE001 - um arquivo ruim não derruba o dia; n_arquivos registra a perda
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inicio"); ap.add_argument("--fim"); ap.add_argument("--saida", default="glm")
    ap.add_argument("--filtro-qualidade", action="store_true", help="só flashes com flash_quality_flag == 0")
    ap.add_argument("--testar", action="store_true")
    a = ap.parse_args()
    import requests
    cliente = requests.Session()
    locais = cap.tabela()
    if a.testar:
        d = date.today() - timedelta(days=3)
        b = bucket_da_data(d)
        chaves = listar(cliente, b, f"GLM-L2-LCFA/{d.year}/{d.timetuple().tm_yday:03d}/12/")
        print(f"{b}: {len(chaves)} arquivos na hora 12 UTC de {d} (esperado ~180)")
        if chaves:
            r = cliente.get(f"https://{b}.s3.amazonaws.com/{chaves[0]}", timeout=120)
            c = contar_arquivo(r.content, np.array([l['lat'] for l in locais]), np.array([l['lon'] for l in locais]))
            print("primeiro arquivo lido; flashes por capital (30/50/100 km):", int(c.sum()), "no total")
        return
    if not (a.inicio and a.fim):
        raise SystemExit("Informe --inicio e --fim (AAAA-MM-DD) ou use --testar.")
    pasta = Path(a.saida); pasta.mkdir(parents=True, exist_ok=True)
    d, fim = date.fromisoformat(a.inicio), date.fromisoformat(a.fim)
    while d <= fim:
        arq = pasta / f"glm_{d:%Y-%m-%d}.csv"
        if not arq.exists():
            df = processar_dia(cliente, d, locais, a.filtro_qualidade)
            df.to_csv(arq, index=False)
            print(f"{d}: {int(df['flashes_30'].sum())} flashes (30 km), arquivos/h mediana {int(df['n_arquivos'].median())}", flush=True)
        d += timedelta(days=1)


if __name__ == "__main__":
    main()
