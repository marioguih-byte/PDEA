"""Gera tests_js/mapa.html: um mapa Folium só com a camada RaiosGLM e o Leaflet local (sem rede), para o teste em Node."""
import re
import sys
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import folium  # noqa: E402

import app  # noqa: E402

agora = int(time.time())
inicial = {"gerado": agora, "janela_min": 30, "ultimo_arquivo": agora - 60, "arquivos": 90,
           "raios": [[-3.7, -38.5, 60], [-23.5, -46.6, 600], [-8.0, -35.0, 1500], [-10.0, -50.0, 2400]]}  # o último passa de 30 min
m = folium.Map(location=[-14, -52], zoom_start=4, tiles=None, zoom_control=False)
m.add_child(app.RaiosGLM(inicial, caminho_base="", janela_s=1800, intervalo_ms=400))
html = m.get_root().render()
html = re.sub(r"<script[^>]*src=[^>]*></script>", "", html)
html = re.sub(r"<link[^>]*>", "", html)
leaflet = (AQUI / "node_modules" / "leaflet" / "dist" / "leaflet.js").read_text(encoding="utf-8")
(AQUI / "mapa.html").write_text(html.replace("<head>", "<head><script>" + leaflet + "</script>", 1), encoding="utf-8")
print("mapa.html gerado")
