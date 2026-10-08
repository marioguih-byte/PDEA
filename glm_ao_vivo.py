"""Raios do GLM (GOES-East) quase em tempo real, atualizados a cada 5 minutos, sem recarregar a página.

Como funciona
-------------
1. Uma thread em segundo plano (``AtualizadorGLM``) roda no servidor do painel. A cada 5 minutos ela lista no bucket público
   da NOAA (Amazon S3) os arquivos GLM-L2-LCFA mais recentes (um arquivo a cada 20 s), baixa só os que ainda não leu,
   guarda os flashes dos últimos 20 minutos e grava o resultado em ``static/glm_flashes.txt`` (JSON em texto) e, depois, o minúsculo ``static/glm_meta.txt`` (só o instante da coleta).
2. O mapa (JavaScript, no navegador) busca esse arquivo de tempos em tempos e redesenha só a camada de raios, sem acionar o
   Streamlit. O Streamlit serve a pasta ``static/`` quando ``server.enableStaticServing = true`` (já em ``.streamlit/config.toml``).
3. O primeiro desenho usa o instantâneo em memória (``dados_atuais()``), embutido no próprio mapa.
4. Raios de antes da abertura: na primeira coleta (servidor recém-ligado ou acordado), o coletor busca no S3 TODOS os arquivos
   da janela (os 20 min anteriores). Quem abre o painel já vê os raios recentes, e não só os que ocorrerem depois de abrir.

Formato do arquivo (JSON): ``{"gerado": <epoch s>, "janela_min": 20, "ultimo_arquivo": <epoch s>, "arquivos": N,
"raios": [[lat, lon, idade_s], ...]}``, em que ``idade_s`` é a idade do flash no instante ``gerado``.

Limites: o GLM mede a atividade elétrica total (intranuvem e nuvem-solo), tem eficiência de detecção que varia com a
posição e o horário, e os arquivos chegam ao S3 com atraso de alguns minutos. É uma observação por satélite, não um alerta.
Dependências: requests, numpy, netCDF4.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

BUCKET = os.environ.get("PDEA_GLM_BUCKET", "noaa-goes19")  # GOES-East desde 07/04/2025
VERSAO_GLM = "2026-10-08f"  # aparece no texto de ajuda do cartão do mapa (ajuda a ver qual coletor está rodando)
JANELA_MIN = 20
TENTATIVAS_ARQUIVO = 3  # um arquivo que falha é tentado de novo nas rodadas seguintes, até este número de vezes
IDADE_MAX_ARQUIVO_S = 900  # ao ligar, um arquivo de dados mais velho que isto é considerado resto de execução antiga e descartado
INTERVALO_S = 300
MAX_RAIOS = 24000
FAIXA_S = 300  # faixas de idade de 5 min (vermelho, laranja, amarelo, verde)
CAIXA_BRASIL = (-35.0, 7.0, -75.0, -32.0)  # lat_min, lat_max, lon_min, lon_max (com folga)
ARQUIVO_PADRAO = Path(__file__).resolve().parent / "static" / "glm_flashes.txt"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
PADRAO_NOME = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})\d_")


def inicio_do_arquivo(chave: str) -> Optional[datetime]:
    """Início do arquivo (UTC) a partir de ``..._sAAAADDDHHMMSSt_...``."""
    m = PADRAO_NOME.search(chave)
    if not m:
        return None
    ano, doy, hh, mm, ss = (int(x) for x in m.groups())
    return datetime(ano, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hh, minutes=mm, seconds=ss)


def listar_hora(cliente, hora: datetime, bucket: str = BUCKET) -> list[str]:
    """Chaves .nc de uma hora UTC (ListObjectsV2 anônimo, com paginação)."""
    prefixo = f"GLM-L2-LCFA/{hora.year}/{hora.timetuple().tm_yday:03d}/{hora.hour:02d}/"
    chaves, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefixo}
        if token:
            params["continuation-token"] = token
        r = cliente.get(f"https://{bucket}.s3.amazonaws.com/", params=params, timeout=30)
        r.raise_for_status()
        raiz = ET.fromstring(r.content)
        chaves += [c.findtext("s3:Key", namespaces=NS) for c in raiz.findall("s3:Contents", NS)]
        if raiz.findtext("s3:IsTruncated", namespaces=NS) == "true":
            token = raiz.findtext("s3:NextContinuationToken", namespaces=NS)
        else:
            return [c for c in chaves if c and c.endswith(".nc")]


_TRAVA_NETCDF = threading.Lock()  # a biblioteca NetCDF/HDF5 não é segura para uso paralelo


def extrair_flashes(conteudo: bytes) -> np.ndarray:
    """Matriz (n, 2) com [lat, lon] dos flashes do arquivo que caem na caixa do Brasil."""
    import netCDF4

    with netCDF4.Dataset("glm.nc", mode="r", memory=conteudo) as ds:
        lat = np.ma.filled(ds.variables["flash_lat"][:], np.nan).astype(float)
        lon = np.ma.filled(ds.variables["flash_lon"][:], np.nan).astype(float)
    la0, la1, lo0, lo1 = CAIXA_BRASIL
    ok = np.isfinite(lat) & np.isfinite(lon) & (lat >= la0) & (lat <= la1) & (lon >= lo0) & (lon <= lo1)
    return np.column_stack([lat[ok], lon[ok]]) if ok.any() else np.empty((0, 2))


def compactar(raios: list[list]) -> list[list]:
    """Junta flashes repetidos: mesma célula (0,01 grau, cerca de 1 km) no mesmo minuto viram um só ponto.

    No mapa eles se sobrepõem, então nada se perde na imagem, e o arquivo encolhe bastante em tempestades fortes.
    """
    vistos: set[tuple] = set()
    saida = []
    for lat, lon, idade in raios:
        chave = (lat, lon, idade // 60)
        if chave not in vistos:
            vistos.add(chave)
            saida.append([lat, lon, idade])
    return saida


def amostrar_por_faixa(raios: list[list], maximo: int, faixa_s: int = FAIXA_S, n_faixas: int = JANELA_MIN * 60 // FAIXA_S) -> list[list]:
    """Limita a quantidade SEM favorecer só os mais recentes: cada faixa de idade (5 min) recebe a mesma cota.

    Cortar sempre os mais antigos faria sumirem o amarelo e o verde nas tempestades fortes. Dentro da faixa, a amostra é
    uniforme (um a cada k), o que preserva a distribuição espacial.
    """
    if len(raios) <= maximo:
        return raios
    faixas: dict[int, list[list]] = {}
    for r in raios:
        faixas.setdefault(min(r[2] // faixa_s, n_faixas - 1), []).append(r)  # idade == 20 min fica na última faixa (verde)
    cota = max(1, maximo // len(faixas))
    saida: list[list] = []
    for k in sorted(faixas):
        itens = faixas[k]
        if len(itens) <= cota:
            saida += itens
        else:
            passo = len(itens) / cota
            saida += [itens[int(i * passo)] for i in range(cota)]
    return saida


class AtualizadorGLM:
    """Mantém os flashes dos últimos ``janela_min`` minutos e grava o arquivo lido pelo mapa."""

    def __init__(self, caminho: Path = ARQUIVO_PADRAO, janela_min: int = JANELA_MIN, intervalo_s: int = INTERVALO_S,
                 cliente: Any = None, bucket: str = BUCKET, trabalhadores: int = 8) -> None:
        self.caminho, self.janela_min, self.intervalo_s = Path(caminho), janela_min, intervalo_s
        self.bucket, self.trabalhadores = bucket, trabalhadores
        self._cliente = cliente
        self._por_arquivo: dict[str, tuple[datetime, np.ndarray]] = {}
        self._falhas: dict[str, int] = {}
        self._rapidas = 0  # rodadas extras de reforço logo depois de ligar
        self._limpou = False
        self._lock = threading.Lock()
        self.ultimo: Optional[dict[str, Any]] = None
        self.ultimo_erro: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self.primeira = threading.Event()  # sinaliza o fim da primeira coleta (com sucesso ou não)

    # -------------------------------------------------------------- coleta
    def _cli(self):
        if self._cliente is None:
            import requests

            self._cliente = requests.Session()
        return self._cliente

    def _baixar(self, chave: str) -> bytes:
        r = self._cli().get(f"https://{self.bucket}.s3.amazonaws.com/{chave}", timeout=60)
        r.raise_for_status()
        return r.content

    def atualizar(self, agora: Optional[datetime] = None) -> dict[str, Any]:
        """Uma rodada: descobre os arquivos novos, baixa, descarta os antigos e devolve o instantâneo."""
        agora = agora or datetime.now(timezone.utc)
        corte = agora - timedelta(minutes=self.janela_min)
        horas = {corte.replace(minute=0, second=0, microsecond=0), agora.replace(minute=0, second=0, microsecond=0)}
        candidatos = []
        for h in sorted(horas):
            for chave in listar_hora(self._cli(), h, self.bucket):
                t = inicio_do_arquivo(chave)
                if t is not None and t >= corte and chave not in self._por_arquivo and self._falhas.get(chave, 0) < TENTATIVAS_ARQUIVO:
                    candidatos.append((t, chave))
        if candidatos:
            # Download em paralelo (só rede). A leitura NetCDF/HDF5 é feita um arquivo por vez: a biblioteca não é
            # segura para várias threads e derruba o processo (erro de barramento) se for chamada em paralelo.
            with ThreadPoolExecutor(self.trabalhadores) as ex:
                conteudos = list(ex.map(lambda tc: self._seguro(tc[1]), candidatos))
            resultados = [self._ler(c) for c in conteudos]
            with self._lock:
                for (t, chave), arr in zip(candidatos, resultados):
                    if arr is None:
                        self._falhas[chave] = self._falhas.get(chave, 0) + 1  # tenta de novo na próxima rodada, até TENTATIVAS_ARQUIVO
                    else:
                        self._por_arquivo[chave] = (t, arr)
                        self._falhas.pop(chave, None)
        with self._lock:
            self._por_arquivo = {k: v for k, v in self._por_arquivo.items() if v[0] >= corte}
            self._falhas = {k: n for k, n in self._falhas.items() if (inicio_do_arquivo(k) or agora) >= corte}
            pares = sorted(self._por_arquivo.values(), key=lambda p: p[0])
        raios: list[list] = []
        for t, arr in pares:
            idade = int((agora - t).total_seconds())
            raios += [[round(float(la), 2), round(float(lo), 2), idade] for la, lo in arr]
        raios = amostrar_por_faixa(compactar(raios), MAX_RAIOS)  # sem cortar só os antigos: todas as cores seguem presentes
        dados = {"versao": VERSAO_GLM, "gerado": int(agora.timestamp()), "janela_min": self.janela_min,
                 "ultimo_arquivo": int(pares[-1][0].timestamp()) if pares else None, "arquivos": len(pares), "raios": raios}
        self.ultimo = dados
        return dados

    def _seguro(self, chave: str):
        try:
            return self._baixar(chave)
        except Exception:  # noqa: BLE001 - um arquivo ruim não derruba a rodada
            return None

    @staticmethod
    def _ler(conteudo: Optional[bytes]):
        if conteudo is None:
            return None
        try:
            with _TRAVA_NETCDF:
                return extrair_flashes(conteudo)
        except Exception:  # noqa: BLE001 - arquivo corrompido: conta como falha
            return None

    # -------------------------------------------------------------- saída
    def gravar(self, dados: dict[str, Any]) -> None:
        """Escrita atômica: o navegador nunca lê um arquivo pela metade."""
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.caminho)
        # Arquivo minúsculo, gravado DEPOIS do principal: o navegador o consulta com frequência e só baixa o arquivo
        # grande quando "gerado" mudar (o servidor do Streamlit não responde 304 à revalidação).
        meta = self.caminho.with_name("glm_meta.txt")
        tmp_meta = meta.with_suffix(".tmp")
        tmp_meta.write_text(json.dumps({"gerado": dados["gerado"], "raios": len(dados["raios"])}, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp_meta, meta)

    def rodada(self) -> None:
        try:
            self.gravar(self.atualizar())
            self.ultimo_erro = None
        except Exception as exc:  # noqa: BLE001 - mantém o último arquivo bom e tenta de novo no próximo ciclo
            self.ultimo_erro = f"{type(exc).__name__}: {exc}"
        finally:
            self.primeira.set()

    def limpar_arquivos_antigos(self) -> None:
        """Ao ligar, apaga os arquivos de dados que sobraram de execuções antigas (inclusive os enviados por engano ao GitHub).

        Eles seriam servidos como se fossem atuais, com dados velhos e uma janela diferente. A pasta static/ só deve conter o que
        ESTE processo gerou.
        """
        if self._limpou:
            return
        self._limpou = True
        for nome in (self.caminho.name, "glm_meta.txt", self.caminho.stem + ".tmp", "glm_meta.tmp"):
            try:
                (self.caminho.parent / nome).unlink(missing_ok=True)
            except OSError:
                pass

    def espera_s(self, agora_ts: Optional[float] = None) -> float:
        """Segundos até a próxima rodada: alinhada ao relógio (minutos 0, 5, 10... mais 20 s) ou, logo depois de ligar com a janela
        incompleta, um reforço em 30 s para preencher o que faltou."""
        agora_ts = time.time() if agora_ts is None else agora_ts
        esperados = self.janela_min * 60 / 20  # um arquivo a cada 20 s
        if self.ultimo is not None and self.ultimo["arquivos"] < 0.8 * esperados and self._rapidas < 6:
            self._rapidas += 1
            return 30.0
        return max(self.intervalo_s - (agora_ts % self.intervalo_s) + 20, 5.0)

    def _laco(self) -> None:
        while True:
            self.rodada()
            time.sleep(self.espera_s())

    def iniciar(self) -> "AtualizadorGLM":
        if self._thread is None or not self._thread.is_alive():
            self.limpar_arquivos_antigos()
            self._thread = threading.Thread(target=self._laco, name="pdea-glm", daemon=True)
            self._thread.start()
        return self


_UNICO: Optional[AtualizadorGLM] = None
_TRAVA = threading.Lock()


def obter_atualizador(iniciar: bool = True) -> AtualizadorGLM:
    """Instância única por processo (várias sessões do painel compartilham a mesma coleta)."""
    global _UNICO
    with _TRAVA:
        if _UNICO is None:
            _UNICO = AtualizadorGLM()
        if iniciar:
            _UNICO.iniciar()
        return _UNICO


def dados_atuais(aguardar_s: float = 0.0) -> Optional[dict[str, Any]]:
    """Instantâneo mais recente em memória; se a coleta ainda não terminou, tenta ler o arquivo gravado.

    ``aguardar_s``: tempo máximo (s) para esperar a PRIMEIRA coleta (servidor recém-ligado), para o mapa já abrir com os
    raios dos últimos 20 min em vez de vazio.
    """
    a = obter_atualizador(iniciar=False)
    if not a.ultimo and aguardar_s > 0 and a._thread is not None and a._thread.is_alive():
        a.primeira.wait(timeout=aguardar_s)
    if a.ultimo:
        return a.ultimo
    try:
        d = json.loads(a.caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if time.time() - d.get("gerado", 0) <= IDADE_MAX_ARQUIVO_S else None  # arquivo velho não vale como "atual"
