"""Camadas opcionais do mapa (sem dependência do Streamlit, para o app e o validar.py usarem).

Topos de nuvem: GOES-East (banda 13, infravermelho "limpo") servido pela NASA GIBS (WMTS, EPSG:3857).
"default" no lugar do horário pede a imagem mais recente disponível. Se a NASA mudar o nome da camada,
basta ajustar ``GOES_CAMADA`` (lista em https://nasa-gibs.github.io/gibs-api-docs/available-visualizations/).
Rode ``python validar.py --online`` para conferir se o servidor responde com uma imagem.
"""

GOES_CAMADA = "GOES-East_ABI_Band13_Clean_Infrared"
GOES_ATRIBUICAO = "Imagem: GOES-East ABI banda 13 · NASA GIBS"
GOES_ZOOM_NATIVO = 6  # GoogleMapsCompatible_Level6


def url_goes(camada: str = GOES_CAMADA, tempo: str = "default") -> str:
    """Modelo de URL de tiles XYZ (``{z}/{y}/{x}``) da camada; ``tempo`` = "default" (mais recente) ou ISO 8601."""
    return (
        "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
        f"{camada}/default/{tempo}/GoogleMapsCompatible_Level6/{{z}}/{{y}}/{{x}}.png"
    )
