"""Gera tests_js/mapa_aneis.html: um mapa Folium só com os anéis de alcance (como no app) e o Leaflet local, para o teste em Node."""
import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import folium  # noqa: E402
import pandas as pd  # noqa: E402

import app  # noqa: E402

tabela = pd.DataFrame({"Latitude": [-3.7319, -23.5505], "Longitude": [-38.5267, -46.6333]})
m = folium.Map(location=[-14, -52], zoom_start=4, tiles=None, zoom_control=False, zoom_snap=0.25, zoom_delta=0.5)  # sem prefer_canvas: o jsdom não implementa canvas (o app usa canvas; a lógica de painel e zoom é a mesma)
app.adicionar_alcance(m, tabela)
html = m.get_root().render()
html = re.sub(r"<script[^>]*src=[^>]*></script>", "", html)
html = re.sub(r"<link[^>]*>", "", html)
leaflet = (AQUI / "node_modules" / "leaflet" / "dist" / "leaflet.js").read_text(encoding="utf-8")
(AQUI / "mapa_aneis.html").write_text(html.replace("<head>", "<head><script>" + leaflet + "</script>", 1), encoding="utf-8")
print("mapa_aneis.html gerado")
