"""Gera tests_js/legenda.html: a legenda do mapa (com todas as seções) dentro de uma página mínima, para o teste em Node."""
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import app  # noqa: E402

html = app._legenda_mapa(app.CORES_NIVEL, "19:00 (08/10)", "Best Match (automático)", goes=True, glm=True, alcance=True)
(AQUI / "legenda.html").write_text("<!doctype html><html><body>" + html + "</body></html>", encoding="utf-8")
print("legenda.html gerado")
