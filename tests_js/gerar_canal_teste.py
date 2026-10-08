"""Gera tests_js/canal.html: o <script> que o app entrega (canal da página) com alguns raios, para o teste de iframes em Node."""
import sys
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
import app  # noqa: E402

agora = int(time.time())
dados = {"versao": "teste-iframes", "gerado": agora + 120, "janela_min": 20, "ultimo_arquivo": agora + 90, "arquivos": 60,
         "raios": [[-1.0, -50.0, 20], [-2.0, -51.0, 400], [-3.0, -52.0, 700]]}
(AQUI / "canal.html").write_text("<!doctype html><html><body>" + app._html_canal_glm(dados) + "</body></html>", encoding="utf-8")
print("canal.html gerado")
