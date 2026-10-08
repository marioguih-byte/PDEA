"""As 27 capitais brasileiras (26 estados + Distrito Federal) monitoradas pelo PDEA.

Coordenadas aproximadas do centro urbano de cada capital. O modelo numérico usa a célula de grade mais próxima.
"""

from __future__ import annotations

import unicodedata


def _normalizar(texto: str) -> str:
    """Remove acentos para obter uma ordenação alfabética estável."""
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


_ESTACOES_BRUTO = [
    {"nome": "Rio Branco", "uf": "AC", "lat": -9.9747, "lon": -67.81},
    {"nome": "Maceió", "uf": "AL", "lat": -9.6658, "lon": -35.7353},
    {"nome": "Macapá", "uf": "AP", "lat": 0.0349, "lon": -51.0694},
    {"nome": "Manaus", "uf": "AM", "lat": -3.119, "lon": -60.0217},
    {"nome": "Salvador", "uf": "BA", "lat": -12.9714, "lon": -38.5014},
    {"nome": "Fortaleza", "uf": "CE", "lat": -3.7319, "lon": -38.5267},
    {"nome": "Brasília", "uf": "DF", "lat": -15.7939, "lon": -47.8828},
    {"nome": "Vitória", "uf": "ES", "lat": -20.3155, "lon": -40.3128},
    {"nome": "Goiânia", "uf": "GO", "lat": -16.6869, "lon": -49.2648},
    {"nome": "São Luís", "uf": "MA", "lat": -2.5307, "lon": -44.3068},
    {"nome": "Cuiabá", "uf": "MT", "lat": -15.6014, "lon": -56.0979},
    {"nome": "Campo Grande", "uf": "MS", "lat": -20.4697, "lon": -54.6201},
    {"nome": "Belo Horizonte", "uf": "MG", "lat": -19.9167, "lon": -43.9345},
    {"nome": "Belém", "uf": "PA", "lat": -1.4558, "lon": -48.4902},
    {"nome": "João Pessoa", "uf": "PB", "lat": -7.1195, "lon": -34.845},
    {"nome": "Curitiba", "uf": "PR", "lat": -25.4284, "lon": -49.2733},
    {"nome": "Recife", "uf": "PE", "lat": -8.0476, "lon": -34.877},
    {"nome": "Teresina", "uf": "PI", "lat": -5.0892, "lon": -42.8019},
    {"nome": "Rio de Janeiro", "uf": "RJ", "lat": -22.9068, "lon": -43.1729},
    {"nome": "Natal", "uf": "RN", "lat": -5.7945, "lon": -35.211},
    {"nome": "Porto Alegre", "uf": "RS", "lat": -30.0346, "lon": -51.2177},
    {"nome": "Porto Velho", "uf": "RO", "lat": -8.7612, "lon": -63.9004},
    {"nome": "Boa Vista", "uf": "RR", "lat": 2.8235, "lon": -60.6758},
    {"nome": "Florianópolis", "uf": "SC", "lat": -27.5954, "lon": -48.548},
    {"nome": "São Paulo", "uf": "SP", "lat": -23.5505, "lon": -46.6333},
    {"nome": "Aracaju", "uf": "SE", "lat": -10.9472, "lon": -37.0731},
    {"nome": "Palmas", "uf": "TO", "lat": -10.1689, "lon": -48.3317},
]

ESTACOES = sorted(_ESTACOES_BRUTO, key=lambda estacao: _normalizar(estacao["nome"]))
UFS = sorted({estacao["uf"] for estacao in ESTACOES})


def sigla(nome: str) -> str:
    """Rótulo curto da capital (o próprio nome)."""
    return nome.rsplit(" - ", 1)[-1] if " - " in nome else nome
