"""Sobe o painel com previsão e raios SIMULADOS (sem rede) para o teste de navegador. Uso: streamlit run launcher_teste.py"""
import os
import random
import runpy
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)
os.environ.setdefault("PDEA_HISTORICO", "desligado")
import glm_ao_vivo  # noqa: E402
import modelos  # noqa: E402
from unidades import ESTACOES  # noqa: E402


def previsao_falsa(modelo_id, *a, **k):
    agora = datetime.now(modelos.TZ_BRASILIA).replace(minute=0, second=0, microsecond=0)
    ini = agora.replace(hour=0)
    tempos = [(ini + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(72)]
    idx = int((agora - ini).total_seconds() // 3600)
    d = {}
    for i, e in enumerate(ESTACOES):
        f = (i % 9) / 8
        d[e["nome"]] = {"tempos": tempos, "idx_atual": idx, "li": [-3.0] * 72, "cin": [-20.0] * 72, "cape": [3000.0 * f] * 72, "precip": [2.0 * f] * 72,
                        "rajada": [20.0] * 72, "nivel0": [4800.0] * 72, "t850": [18.0] * 72, "t500": [-8.0] * 72}
    d.update({"_hora_referencia": "12:00", "_obtido_em": datetime.now(modelos.TZ_BRASILIA).isoformat(), "_extras": True, "_erro_extras": None})
    return d


modelos.buscar_modelo = previsao_falsa
glm_ao_vivo.AtualizadorGLM.iniciar = lambda self: self  # sem coleta real (sem rede)
coletor = glm_ao_vivo.obter_atualizador(iniciar=False)
rnd = random.Random(3)


def nova_coleta() -> None:
    agora = int(time.time())
    coletor.ultimo = {"versao": "teste", "gerado": agora, "janela_min": 20, "ultimo_arquivo": agora - 60, "arquivos": 60,
                      "raios": [[round(rnd.uniform(-30, 2), 2), round(rnd.uniform(-70, -40), 2), rnd.randint(0, 1199)] for _ in range(3000)]}


if not getattr(sys, "_pdea_e2e_thread", False):  # uma nova "coleta" a cada 20 s, como se o servidor estivesse coletando (a cada 5 min na vida real)
    sys._pdea_e2e_thread = True
    nova_coleta()
    coletor.primeira.set()

    def laco() -> None:
        while True:
            time.sleep(20)
            nova_coleta()

    threading.Thread(target=laco, daemon=True).start()
runpy.run_path(str(RAIZ / "app.py"), run_name="__main__")
