"""Consulta de modelos numéricos na API de previsão meteorológica."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from unidades import ESTACOES

API_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 25
TENTATIVAS = 3  # tentativas em caso de falha de rede/servidor (espera 1 s, 2 s, …)
HORIZONTE_DIAS = 3  # dias de previsão (a partir de 00:00 de hoje) para cobrir +48 h
VARIAVEIS_HOURLY = "cape,lifted_index,convective_inhibition"
# Variáveis da heurística ampliada. Vêm em uma 2ª requisição, tolerante a falhas: se a API recusar
# alguma delas (ou o modelo não as fornecer), o painel segue só com CAPE, LI e CIN.
VARIAVEIS_EXTRAS = "precipitation,wind_gusts_10m,freezing_level_height,temperature_850hPa,temperature_500hPa"
CAMPOS_EXTRAS = {
    "precipitation": "precip",
    "wind_gusts_10m": "rajada",
    "freezing_level_height": "nivel0",
    "temperature_850hPa": "t850",
    "temperature_500hPa": "t500",
}
TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

MODELOS = {
    "best_match": ("Best Match (automático)", "—", "Automática"),
    "ecmwf_ifs025": ("ECMWF IFS 0,25°", "União Europeia", "A cada 6 horas"),
    "ecmwf_aifs025": ("ECMWF AIFS 0,25° (IA)", "União Europeia", "A cada 6 horas"),
    "gfs_seamless": ("NOAA GFS Seamless", "Estados Unidos", "A cada 1 hora"),
    "icon_seamless": ("DWD ICON Seamless", "Alemanha", "A cada 3 horas"),
    "gem_seamless": ("GEM Seamless", "Canadá", "A cada 6 horas"),
    "meteofrance_seamless": ("Météo-France Seamless", "França", "A cada 1 hora"),
    "jma_seamless": ("JMA Seamless (GSM)", "Japão", "A cada 6 horas"),
    "kma_seamless": ("KMA Seamless (GDPS)", "Coreia do Sul", "A cada 6 horas"),
    "ukmo_seamless": ("UK Met Office Seamless", "Reino Unido", "A cada 1 hora"),
    "bom_access_global": ("BOM ACCESS-G", "Austrália", "A cada 6 horas"),
    "cma_grapes_global": ("CMA GRAPES Global", "China", "A cada 6 horas"),
}


class ErroBuscaModelo(RuntimeError):
    """Erro tratado durante a obtenção de previsão de um modelo."""


def horario_local(tempo: str) -> datetime:
    """Interpreta a hora da API e a devolve explicitamente em America/Sao_Paulo."""
    data_hora = datetime.fromisoformat(tempo)
    if data_hora.tzinfo is None:
        return data_hora.replace(tzinfo=TZ_BRASILIA)
    return data_hora.astimezone(TZ_BRASILIA)


def _indice_hora_atual(tempos: list[str]) -> int:
    """Localiza, na série horária, a previsão mais próxima do horário atual."""
    if not tempos:
        return 0

    agora = datetime.now(TZ_BRASILIA)
    alvos: list[datetime] = []
    for tempo in tempos:
        try:
            alvos.append(horario_local(tempo))
        except ValueError:
            alvos.append(agora)
    return min(range(len(alvos)), key=lambda indice: abs(alvos[indice] - agora))


def _como_numero(valor: Any) -> Optional[float]:
    """Normaliza valores ausentes devolvidos pela API."""
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _requisitar(params: dict[str, Any], cliente: Any) -> list[dict[str, Any]]:
    """GET na API com tentativas; devolve a lista de blocos (um por localização)."""
    resposta = None
    ultimo_erro = ""
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resposta = cliente.get(API_URL, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            ultimo_erro = f"Não foi possível conectar ao serviço de previsão: {exc}"
            resposta = None
        else:
            if resposta.status_code == 200:
                break
            detalhe = resposta.text[:300].replace("\n", " ")
            ultimo_erro = f"O serviço retornou HTTP {resposta.status_code}: {detalhe}"
            # Erros do cliente (4xx, exceto 429) não melhoram ao repetir.
            if 400 <= resposta.status_code < 500 and resposta.status_code != 429:
                raise ErroBuscaModelo(ultimo_erro)
            resposta = None
        if tentativa < TENTATIVAS:
            time.sleep(tentativa)
    if resposta is None:
        raise ErroBuscaModelo(ultimo_erro)

    try:
        payload = resposta.json()
    except ValueError as exc:
        raise ErroBuscaModelo("O serviço retornou uma resposta inválida.") from exc

    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != len(ESTACOES):
        quantidade = len(payload) if isinstance(payload, list) else 0
        raise ErroBuscaModelo(
            f"Foram recebidas {quantidade} localidades, mas eram esperadas {len(ESTACOES)}."
        )
    return payload


def _parametros(model_id: str, variaveis: str) -> dict[str, Any]:
    params = {
        "latitude": ",".join(str(estacao["lat"]) for estacao in ESTACOES),
        "longitude": ",".join(str(estacao["lon"]) for estacao in ESTACOES),
        "hourly": variaveis,
        "forecast_days": HORIZONTE_DIAS,
        "timezone": "America/Sao_Paulo",
        "cell_selection": "nearest",
    }
    if model_id != "best_match":
        params["models"] = model_id
    return params


def _anexar_extras(resultado: dict[str, Any], payload_extras: list[dict[str, Any]]) -> int:
    """Anexa as séries extras a cada unidade; devolve quantas unidades receberam ao menos uma variável com dados."""
    com_dados = 0
    for estacao, bloco in zip(ESTACOES, payload_extras):
        hourly = bloco.get("hourly", {}) if isinstance(bloco, dict) else {}
        serie = resultado[estacao["nome"]]
        n = len(serie["tempos"])
        recebeu = False
        for variavel, campo in CAMPOS_EXTRAS.items():
            valores = hourly.get(variavel) or []
            if len(valores) != n:  # eixo de tempo diferente do núcleo: descarta em vez de desalinhar
                continue
            numeros = [_como_numero(v) for v in valores]
            serie[campo] = numeros
            recebeu = recebeu or any(v is not None for v in numeros)
        com_dados += int(recebeu)
    return com_dados


def buscar_modelo(model_id: str, session: Optional[requests.Session] = None, extras: bool = True) -> dict[str, Any]:
    """Busca CAPE, Lifted Index e CIN horários para todas as estações.

    A API recebe coordenadas separadas por vírgula e retorna uma série por
    localização. O resultado é indexado pelo nome da estação e inclui uma
    referência temporal, permitindo que a interface represente a situação
    atual e o horizonte de previsão.

    Com ``extras=True`` busca também precipitação, rajada, nível de 0 °C e temperaturas
    em 850/500 hPa (heurística ampliada). Essa 2ª consulta nunca derruba a primeira:
    se falhar, ``resultado["_extras"]`` fica ``False`` e ``resultado["_erro_extras"]`` explica.
    """
    if model_id not in MODELOS:
        raise ErroBuscaModelo(f"Modelo inválido: {model_id}")

    cliente = session or requests.Session()
    if extras:
        # As duas consultas são independentes: rodam em paralelo para não somar a latência.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futuro_extras = pool.submit(_requisitar, _parametros(model_id, VARIAVEIS_EXTRAS), cliente)
            payload = _requisitar(_parametros(model_id, VARIAVEIS_HOURLY), cliente)
            try:
                payload_extras: Optional[list[dict[str, Any]]] = futuro_extras.result()
                erro_extras = ""
            except ErroBuscaModelo as exc:
                payload_extras, erro_extras = None, str(exc)
    else:
        payload = _requisitar(_parametros(model_id, VARIAVEIS_HOURLY), cliente)
        payload_extras, erro_extras = None, "desligadas"

    resultado: dict[str, Any] = {}
    hora_referencia = "—"

    for estacao, bloco in zip(ESTACOES, payload):
        hourly = bloco.get("hourly", {}) if isinstance(bloco, dict) else {}
        tempos = hourly.get("time", []) or []
        capes = hourly.get("cape", []) or []
        lis = hourly.get("lifted_index", []) or []
        cins = hourly.get("convective_inhibition", []) or []
        indice = _indice_hora_atual(tempos)

        resultado[estacao["nome"]] = {
            "tempos": tempos,
            "cape": [_como_numero(valor) for valor in capes],
            "li": [_como_numero(valor) for valor in lis],
            "cin": [_como_numero(valor) for valor in cins],
            "idx_atual": indice,
        }

        if hora_referencia == "—" and tempos:
            try:
                hora_referencia = horario_local(tempos[indice]).strftime("%H:%M (%d/%m)")
            except (ValueError, IndexError):
                hora_referencia = tempos[indice] if indice < len(tempos) else "—"

    resultado["_extras"] = False
    resultado["_erro_extras"] = erro_extras
    if payload_extras is not None:
        com_dados = _anexar_extras(resultado, payload_extras)
        resultado["_extras"] = com_dados > 0
        if com_dados == 0:
            resultado["_erro_extras"] = "o modelo não devolveu valores para as variáveis extras"
    resultado["_hora_referencia"] = hora_referencia
    resultado["_obtido_em"] = datetime.now(TZ_BRASILIA).isoformat()
    return resultado
