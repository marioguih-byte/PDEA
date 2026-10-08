"""Metadados das 27 capitais para a verificação: região, UTC e se é litorânea (lido de config_regioes.json)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from unidades import ESTACOES  # noqa: E402

REGIAO = {
    **{uf: "Norte" for uf in ("AC", "AP", "AM", "PA", "RO", "RR", "TO")},
    **{uf: "Nordeste" for uf in ("AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE")},
    **{uf: "Centro-Oeste" for uf in ("DF", "GO", "MT", "MS")},
    **{uf: "Sudeste" for uf in ("ES", "MG", "RJ", "SP")},
    **{uf: "Sul" for uf in ("PR", "RS", "SC")},
}
# Deslocamento fixo em relação ao UTC (horas), sem horário de verão. Serve ao ciclo diurno (hora local de cada capital).
OFFSET_UTC = {"AC": -5, "AM": -4, "RO": -4, "RR": -4, "MT": -4, "MS": -4}


def ufs_costeiras() -> frozenset[str]:
    try:
        cfg = json.loads((RAIZ / "config_regioes.json").read_text(encoding="utf-8"))
        return frozenset(cfg.get("correcao_costeira", {}).get("ufs", []))
    except (OSError, ValueError):
        return frozenset()


def tabela() -> list[dict]:
    cost = ufs_costeiras()
    return [
        {"local": e["nome"], "uf": e["uf"], "lat": e["lat"], "lon": e["lon"], "regiao": REGIAO[e["uf"]],
         "costeira": int(e["uf"] in cost), "utc_offset": OFFSET_UTC.get(e["uf"], -3)}
        for e in ESTACOES
    ]
