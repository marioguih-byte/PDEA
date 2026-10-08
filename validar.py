"""Verificações do PDEA.

Sem argumentos roda só os testes offline (cálculo, séries, tendência, heurística ampliada,
regiões, histórico, alertas, relatório).

Com ``--online`` consulta a API de verdade e imprime um relatório de sanidade dos dados
(cobertura, faixas de valores, horizonte, variáveis extras, camada GOES).
Com ``--online --modelos`` repete a checagem para todos os modelos (12 requisições) e mostra
quais devolvem CAPE, Lifted Index e CIN.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from analise import consolidar, horas_a_frente, series_por_unidade, tendencia
from modelos import MODELOS, TZ_BRASILIA, VARIAVEIS_HOURLY, buscar_modelo, horario_local
from risco_raio import ParametrosRisco as _ParametrosRisco, ajuste_extras, calcular_risco as _calcular_risco, pontuar_capexp

# Os testes abaixo da heurística original usam o método legado "pontos" (PDEA-H); o método principal
# (CAPE x chuva) é testado em testes_capexp().
def ParametrosRisco(**kw):  # noqa: N802
    return _ParametrosRisco(**{"metodo": "pontos", **kw})


def calcular_risco(cape, li, cin, parametros=None, extras=None, multiplicador_gate=None):
    return _calcular_risco(cape, li, cin, parametros or ParametrosRisco(), extras, multiplicador_gate)

from unidades import ESTACOES


def _dados_sinteticos(horas: int = 72, atual: int = 3) -> dict:
    """Série sintética: instabilidade crescente ao longo do dia, igual para todas as unidades."""
    tempos = [f"2026-08-25T{h % 24:02d}:00" if h < 24 else f"2026-08-{25 + h // 24}T{h % 24:02d}:00" for h in range(horas)]
    capes = [max(0, 4200 * (1 - abs(h - 30) / 30)) for h in range(horas)]
    lis = [4 - h * 0.25 if h < 30 else -3.5 + (h - 30) * 0.25 for h in range(horas)]
    cins = [-20.0] * horas
    serie = {"tempos": tempos, "cape": capes, "li": lis, "cin": cins, "idx_atual": atual}
    dados = {e["nome"]: serie for e in ESTACOES}
    dados["_hora_referencia"] = "03:00 (25/08)"
    return dados


def testes_offline() -> None:
    # Heurística original (resultado de referência) e parâmetros neutros.
    score, nivel, _ = calcular_risco(3000, -7, -20)
    assert score == 84.0 and nivel == "Severo", (score, nivel)
    assert calcular_risco(3000, -7, -20, ParametrosRisco())[0] == 84.0
    # Peso do CIN = 0 ignora a contribuição do CIN (CIN forte deixa de reduzir o score).
    assert calcular_risco(3000, -7, -250, ParametrosRisco(peso_cin=0))[0] == 64.0
    assert calcular_risco(3000, -7, -250)[0] == 34.0
    # Sensibilidade ao CAPE aumenta o score.
    assert calcular_risco(900, -7, -20, ParametrosRisco(fator_cape=2.0))[0] > calcular_risco(900, -7, -20)[0]
    assert calcular_risco(None, -7, -20)[1] == "Sem dados"

    horario = horario_local("2026-08-25T20:00")
    assert horario.tzinfo == TZ_BRASILIA
    assert horario.strftime("%H:%M (%d/%m)") == "20:00 (25/08)"

    # Séries, tendência e consolidação.
    dados = _dados_sinteticos()
    assert horas_a_frente(dados) == 68
    series = series_por_unidade(dados, ParametrosRisco())
    assert len(series) == len(ESTACOES)
    tabela = consolidar(dados, series, 0)
    assert len(tabela) == len(ESTACOES) and set(tabela["UF"]) >= {"RJ", "SP", "BA"}
    assert tendencia([10, 30, 50, 60, 20, 10, 5], 0)["seta"] == "▲"
    assert tendencia([60, 40, 30, 20, 10, 5, 0], 0)["seta"] == "▼"
    assert tendencia([30, 31, 32, 31, 30, 29, 30], 0)["seta"] == "▬"
    assert tendencia([None, 1], 0)["seta"] == ""
    alto = consolidar(dados, series, 27)  # perto do pico sintético
    assert alto["Score"].max() > tabela["Score"].max()

    # Alertas: alerta uma vez, não repete, rearma depois de cair.
    import alertas

    tab = consolidar(dados, series, 27)
    primeiro, estado = alertas.avaliar(tab, {}, "Alto", 0, series)
    segundo, estado2 = alertas.avaliar(tab, estado, "Alto", 0, series)
    assert primeiro and not segundo
    tab_baixa = tab.assign(Risco="Baixo")
    _, estado3 = alertas.avaliar(tab_baixa, estado2, "Alto", 0, series)
    de_novo, _ = alertas.avaliar(tab, estado3, "Alto", 0, series)
    assert len(de_novo) == len(primeiro)

    # Relatório: PNG e PDF válidos.
    from relatorio import gerar_pdf, gerar_png

    png = gerar_png(tab, "Teste", "sub")
    pdf = gerar_pdf(tab, "Teste", "sub")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and pdf[:5] == b"%PDF-"
    print("Testes offline: OK")


class _ClienteFalso:
    """Cliente HTTP de mentira: responde à consulta principal e à de variáveis extras."""

    def __init__(self, extras: str = "ok") -> None:
        self.extras = extras  # "ok" | "erro400" | "tempo_diferente"
        self.chamadas: list[str] = []

    class _Resp:
        def __init__(self, codigo: int, payload=None) -> None:
            self.status_code, self._payload, self.text = codigo, payload, "x"

        def json(self):
            return self._payload

    def get(self, url, params=None, timeout=None):
        self.chamadas.append(params["hourly"])
        n = 6
        tempos = [f"2026-08-25T{h:02d}:00" for h in range(n)]
        if params["hourly"] == VARIAVEIS_HOURLY:
            bloco = {"hourly": {"time": tempos, "cape": [100.0 * h for h in range(n)], "lifted_index": [1.0] * n,
                                "convective_inhibition": [-10.0] * n}}
        elif self.extras == "erro400":
            return self._Resp(400)
        else:
            m = n if self.extras == "ok" else n - 1
            bloco = {"hourly": {"time": tempos[:m], "precipitation": [1.0] * m, "wind_gusts_10m": [40.0] * m,
                                "freezing_level_height": [4800.0] * m, "temperature_850hPa": [18.0] * m,
                                "temperature_500hPa": [-8.0] * m}}
        return self._Resp(200, [bloco] * len(ESTACOES))


def testes_capexp() -> None:
    """Método principal: CAPE x chuva (Romps et al., 2014, 2018)."""
    import analise
    from risco_raio import ANCORAS_CAPEXP, ORDEM, classificar_risco

    # Âncoras: os limites dos níveis do painel caem exatamente nelas; a função é monótona e limitada.
    assert [s for _, s in ANCORAS_CAPEXP] == [0.0, 15.0, 35.0, 55.0, 75.0, 100.0]
    assert pontuar_capexp(0) == 0.0 and pontuar_capexp(10) == 0.0 and pontuar_capexp(1e9) == 100.0
    assert abs(pontuar_capexp(50) - 15.0) < 1e-9 and abs(pontuar_capexp(1000) - 55.0) < 1e-9
    xs = [10 ** (k / 20) for k in range(-20, 100)]
    ss = [pontuar_capexp(x) for x in xs]
    assert all(b >= a for a, b in zip(ss, ss[1:])) and min(ss) >= 0 and max(ss) <= 100
    # Níveis em exemplos físicos: sem chuva ou sem CAPE, nada; chuva forte com muito CAPE, Severo.
    P = _ParametrosRisco()
    assert P.metodo == "capexp" and P.usa_extras
    assert _calcular_risco(2500, -6, -10, P, {"precip": 0.0})[1] == "Nenhum"
    assert _calcular_risco(0, -6, -10, P, {"precip": 8.0})[1] == "Nenhum"
    assert _calcular_risco(1000, 0, 0, P, {"precip": 1.0})[:2] == (55.0, "Alto")
    assert _calcular_risco(2500, -6, -10, P, {"precip": 4.0})[1] == "Severo"
    # O LI e o CIN não entram no método principal (o CIN não ajudou nos dados do artigo).
    assert _calcular_risco(1500, -8, -250, P, {"precip": 2.0})[0] == _calcular_risco(1500, 3, 0, P, {"precip": 2.0})[0]
    # Sem precipitação do modelo, o produto não existe: "Sem dados" (não inventa risco).
    assert _calcular_risco(1500, -3, -20, P, None)[1] == "Sem dados" and _calcular_risco(None, 0, 0, P, {"precip": 1.0})[1] == "Sem dados"
    # Fator regional de CAPE (neutro por padrão) ajusta o produto.
    assert _calcular_risco(1000, 0, 0, _ParametrosRisco(fator_cape=2.0), {"precip": 1.0})[0] == pontuar_capexp(2000)
    # Série por capital: sobe com chuva e fica zerada sem chuva.
    seco, chuvoso = _dados_sinteticos(), _dados_sinteticos()
    for d, v in ((seco, 0.0), (chuvoso, 2.0)):
        for s in d.values():
            if isinstance(s, dict):
                s["precip"] = [v] * len(s["tempos"])
    s_seco, s_chuva = analise.series_por_unidade(seco, P), analise.series_por_unidade(chuvoso, P)
    nome = next(e["nome"] for e in ESTACOES if e["nome"] == "Fortaleza")
    interior = "Brasília"
    assert max(v for v in s_seco[nome]["rel"] if v is not None) == 0.0
    assert max(v for v in s_chuva[interior]["rel"] if v is not None) >= 75.0
    # Correção litorânea: capitais litorâneas (config) têm o produto multiplicado por 0,4; as do interior não.
    cc = analise.carregar_correcao_costeira()
    assert cc.fator == 0.4 and cc.ufs == frozenset({"AL", "BA", "CE", "ES", "MA", "PB", "PE", "RJ", "RN", "SC", "SE"})
    assert analise.fator_costeiro("CE") == 0.4 and analise.fator_costeiro("DF") == 1.0 and analise.fator_costeiro("RS") == 1.0
    assert analise.parametros_da_unidade(P, "CE").fator_produto == 0.4 and analise.parametros_da_unidade(P, "MG") is P
    assert _calcular_risco(2500, None, None, _ParametrosRisco(fator_produto=0.4), {"precip": 4.0})[0] == round(pontuar_capexp(4000), 1)
    assert _calcular_risco(2500, None, None, _ParametrosRisco(), {"precip": 4.0})[0] == round(pontuar_capexp(10000), 1)
    # com a mesma série, a litorânea fica abaixo da do interior; o método legado ignora o fator
    assert max(v for v in s_chuva[nome]["rel"] if v is not None) < max(v for v in s_chuva[interior]["rel"] if v is not None)
    assert _calcular_risco(1500, -3, -20, _ParametrosRisco(metodo="pontos", fator_produto=0.1))[0] == _calcular_risco(1500, -3, -20, _ParametrosRisco(metodo="pontos"))[0]
    ex_c = analise.explicar_hora(chuvoso[nome], 27, "CE", P, None, None)
    assert ex_c["fator_produto"] == 0.4 and abs(ex_c["produto_corrigido"] - 0.4 * ex_c["cape_x_chuva"]) < 1e-9
    ex = analise.explicar_hora(chuvoso[nome], 27, "CE", P, None, None)
    assert ex["metodo"] == "capexp" and abs(ex["cape_x_chuva"] - ex["cape_ef"] * 2.0) < 1e-9 and ex["score"] == s_chuva[nome]["rel"][27]
    sem_chuva = analise.explicar_hora({k: v for k, v in chuvoso[nome].items() if k != "precip"}, 27, "CE", P, None, None)
    assert sem_chuva["score"] is None and sem_chuva["sem_chuva"]
    # As 27 capitais, uma por UF, com coordenadas dentro do Brasil.
    assert len(ESTACOES) == 27 and len({e["uf"] for e in ESTACOES}) == 27
    assert all(-34 < e["lat"] < 6 and -75 < e["lon"] < -34 for e in ESTACOES)
    print("Testes do método CAPE x chuva e das capitais: OK")


def testes_glm_ao_vivo() -> None:
    """Coletor dos raios do GLM com um S3 simulado (arquivos NetCDF sintéticos); pula se netCDF4 não estiver instalado."""
    try:
        import netCDF4
    except ImportError:
        print("Testes do GLM ao vivo: PULADOS (instale netCDF4)")
        return
    import glm_ao_vivo as g
    from datetime import timezone

    def nc_bytes(pasta: Path, lats, lons) -> bytes:
        f = pasta / "x.nc"
        with netCDF4.Dataset(f, "w", format="NETCDF4") as ds:
            ds.createDimension("n", len(lats))
            for nome, vals in (("flash_lat", lats), ("flash_lon", lons)):
                ds.createVariable(nome, "f4", ("n",))[:] = vals
        return f.read_bytes()

    agora = datetime(2026, 10, 8, 18, 3, 40, tzinfo=timezone.utc)

    def chave(t_):
        doy = t_.timetuple().tm_yday
        return f"GLM-L2-LCFA/{t_.year}/{doy:03d}/{t_.hour:02d}/OR_GLM-L2-LCFA_G19_s{t_.year}{doy:03d}{t_:%H%M%S}0_e0_c0.nc"

    class Resp:
        def __init__(self, content=b"", code=200):
            self.content, self.status_code = content, code

        def raise_for_status(self):
            if self.status_code != 200:
                raise RuntimeError(self.status_code)

    class S3Falso:
        def __init__(self, arquivos, ruins=()):
            self.arq, self.ruins, self.baixados = arquivos, set(ruins), []

        def get(self, url, params=None, timeout=None):
            if params and "list-type" in params:
                itens = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in sorted(self.arq) if k.startswith(params["prefix"]))
                xml = f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{itens}<IsTruncated>false</IsTruncated></ListBucketResult>'
                return Resp(xml.encode())
            k = url.split(".amazonaws.com/")[1]
            self.baixados.append(k)
            return Resp(b"lixo" if k in self.ruins else self.arq[k])

    assert g.inicio_do_arquivo(chave(agora)) == agora.replace(microsecond=0) and g.inicio_do_arquivo("sem-padrao.nc") is None
    with tempfile.TemporaryDirectory() as p:
        pasta = Path(p)
        arquivos = {}
        t0 = agora.replace(second=0) - timedelta(minutes=50)
        while t0 <= agora:
            arquivos[chave(t0)] = nc_bytes(pasta, [-3.7, -23.5, 40.0, -3.7], [-38.5, -46.6, -100.0, -120.0])  # 2 no Brasil, 2 fora
            t0 += timedelta(seconds=20)
        ruim = chave(agora.replace(second=0) - timedelta(minutes=1))
        cli = S3Falso(arquivos, ruins=[ruim])
        a = g.AtualizadorGLM(caminho=pasta / "static" / "glm_flashes.txt", cliente=cli, janela_min=30)
        d = a.atualizar(agora)
        dentro = len([k for k in arquivos if g.inicio_do_arquivo(k) >= agora - timedelta(minutes=30)]) - 1  # menos o arquivo ruim
        assert d["arquivos"] == dentro and len(d["raios"]) == 2 * dentro
        assert all(-35 <= r[0] <= 7 and -75 <= r[1] <= -32 for r in d["raios"]) and 0 <= min(r[2] for r in d["raios"])
        assert max(r[2] for r in d["raios"]) <= 30 * 60 + 20
        antes = len(cli.baixados)
        for k in range(1, 16):
            arquivos[chave(agora + timedelta(seconds=20 * k))] = nc_bytes(pasta, [-8.0], [-35.0])
        d2 = a.atualizar(agora + timedelta(minutes=5))
        assert len(cli.baixados) - antes == 15 and cli.baixados.count(ruim) == 1  # incremental; o arquivo ruim não é refeito
        assert any(r[0] == -8.0 for r in d2["raios"]) and max(r[2] for r in d2["raios"]) <= 30 * 60 + 20
        a.gravar(d2)
        import json as _json

        assert _json.loads((pasta / "static" / "glm_flashes.txt").read_text(encoding="utf-8"))["gerado"] == d2["gerado"]
        assert _json.loads((pasta / "static" / "glm_meta.txt").read_text(encoding="utf-8"))["gerado"] == d2["gerado"]
        assert not list((pasta / "static").glob("*.tmp"))

        class Quebrado:
            def get(self, *args, **kw):
                raise OSError("sem rede")

        a2 = g.AtualizadorGLM(caminho=pasta / "static" / "glm_flashes.txt", cliente=Quebrado())
        a2.rodada()
        assert "OSError" in (a2.ultimo_erro or "")
        assert _json.loads((pasta / "static" / "glm_flashes.txt").read_text(encoding="utf-8"))["gerado"] == d2["gerado"]  # mantém o último bom
        # Raios de ANTES da abertura: a primeira coleta já reconstrói toda a janela padrão (20 min) a partir do S3.
        assert g.JANELA_MIN == 20 and g.INTERVALO_S == 300
        cli3 = S3Falso({k: v for k, v in arquivos.items() if g.inicio_do_arquivo(k) <= agora}, ruins=[ruim])
        a3 = g.AtualizadorGLM(caminho=pasta / "s3" / "glm_flashes.txt", cliente=cli3)  # janela padrão
        d3 = a3.atualizar(agora)
        dentro20 = len([k for k in cli3.arq if g.inicio_do_arquivo(k) >= agora - timedelta(minutes=20)]) - 1  # menos o ruim
        assert d3["janela_min"] == 20 and d3["arquivos"] == dentro20 and len(d3["raios"]) == 2 * dentro20
        assert max(r[2] for r in d3["raios"]) <= 20 * 60 + 20 and len(cli3.baixados) >= dentro20  # baixou toda a janela de uma vez
        # a primeira coleta é sinalizada e dados_atuais(aguardar_s) espera por ela (servidor recém-ligado)
        import time as _time

        class Lento(S3Falso):
            def get(self, url, params=None, timeout=None):
                _time.sleep(0.05)
                return super().get(url, params, timeout)

        a4 = g.AtualizadorGLM(caminho=pasta / "s4" / "glm_flashes.txt", cliente=Lento({}))
        g._UNICO = a4
        assert not a4.primeira.is_set() and g.dados_atuais() is None
        a4.iniciar()
        espera = g.dados_atuais(aguardar_s=15)
        assert a4.primeira.is_set() and espera is not None and espera["janela_min"] == 20
    print("Testes do GLM ao vivo (coleta incremental, janela de 20 min, antes da abertura, arquivo ruim, gravação atômica): OK")


def testes_cadastro() -> None:
    """Cadastro de e-mail: validação, consentimento, notificação por SMTP simulado, limite por hora, falhas e a tela do app."""
    import csv
    import os

    import cadastro as cd

    for ok in ("a@b.com", "nome.sobrenome+x@mail.com.br", " MAIUSCULA@Exemplo.COM "):
        assert cd.email_valido(ok), ok
    for ruim in ("", "a@b", "a b@c.com", "a@b..com", "a@b.com\nBcc: x@y.com", "a@b.com\r\nSubject: x", "@b.com", ".a@b.com", "a@@b.com", "a" * 300 + "@b.com"):
        assert not cd.email_valido(ruim), repr(ruim)

    # configuração: Secrets primeiro, depois variáveis de ambiente
    c = cd.ler_config({"destino": " Dono@Exemplo.com ", "smtp_user": "u@x.com", "smtp_pass": "s", "smtp_port": "465"}, env={"SMTP_HOST": "h.x"})
    assert c.destino == "dono@exemplo.com" and c.porta == 465 and c.host == "h.x" and c.remetente == "u@x.com" and c.pode_enviar and c.ativo
    assert not cd.ler_config({}, env={}).pode_enviar and cd.ler_config({"ativo": "false"}, env={}).ativo is False
    assert cd.ler_config({}, env={"PDEA_CADASTRO_ATIVO": "0"}).ativo is False and cd.ler_config({}, env={"SMTP_PORT": "xx"}).porta == 587

    class SMTPFalso:
        enviadas: list = []
        logins: list = []
        falhar = False

        def __init__(self, host, porta, timeout=None):
            self.host, self.porta = host, porta

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, s):
            SMTPFalso.logins.append((u, s))

        def send_message(self, msg):
            if SMTPFalso.falhar:
                raise OSError("rede fora")
            SMTPFalso.enviadas.append(msg)

    cfg = cd.ConfigCadastro(destino="dono@exemplo.com", usuario="envio@exemplo.com", senha="segredo", remetente="envio@exemplo.com")
    relogio = [1000.0]

    def novo(pasta, config=cfg):
        SMTPFalso.enviadas, SMTPFalso.logins, SMTPFalso.falhar = [], [], False
        return cd.Cadastro(config, arquivo=Path(pasta) / "c.csv", smtp_fabrica=SMTPFalso, assincrono=False,
                           agora=lambda: relogio[0], espera=lambda s: None)

    with tempfile.TemporaryDirectory() as p:
        cad = novo(p)
        assert cad.registrar("a@b.com", False)[0] is False and cad.registrar("ruim", True)[0] is False and not SMTPFalso.enviadas
        assert cad.registrar("Maria@Exemplo.com", True) == (True, "ok")
        m = SMTPFalso.enviadas[0]
        assert m["To"] == "dono@exemplo.com" and m["From"] == "envio@exemplo.com" and "novo cadastro" in m["Subject"].lower()
        assert "maria@exemplo.com" in m.get_content() and "maria@exemplo.com" not in " ".join(f"{k}: {v}" for k, v in m.items()).lower()
        assert SMTPFalso.logins == [("envio@exemplo.com", "segredo")]
        assert cad.registrar("maria@exemplo.com", True) == (True, "ja_cadastrado") and len(SMTPFalso.enviadas) == 1  # sem duplicar aviso
        assert cad.registrar("+x@b.com", True)[0]
        linhas = list(csv.reader((Path(p) / "c.csv").open(encoding="utf-8")))
        assert linhas[0] == ["criado_em_utc", "email", "consentimento", "versao_do_aviso"] and linhas[1][1] == "maria@exemplo.com"
        assert linhas[2][1] == "'+x@b.com" and all(l[2] == "sim" for l in linhas[1:])  # neutraliza fórmula de planilha
        assert novo(p).total() == 2 and novo(p).registrar("maria@exemplo.com", True)[1] == "ja_cadastrado"  # lê o arquivo existente

    with tempfile.TemporaryDirectory() as p:  # limite de avisos por hora: o excedente segue no CSV e vai junto no próximo aviso
        cad = novo(p)
        for i in range(cd.LIMITE_POR_HORA + 2):
            cad.registrar(f"u{i}@exemplo.com", True)
        assert len(SMTPFalso.enviadas) == cd.LIMITE_POR_HORA and cad.total() == cd.LIMITE_POR_HORA + 2
        relogio[0] += 3700
        cad.registrar("depois@exemplo.com", True)
        corpo = SMTPFalso.enviadas[-1].get_content()
        assert "depois@exemplo.com" in corpo and f"u{cd.LIMITE_POR_HORA}@exemplo.com" in corpo and f"u{cd.LIMITE_POR_HORA + 1}@exemplo.com" in corpo

    with tempfile.TemporaryDirectory() as p:  # falha de SMTP: o visitante entra, o erro é registrado e o e-mail vai no próximo aviso
        cad = novo(p)
        SMTPFalso.falhar = True
        assert cad.registrar("falha@exemplo.com", True)[0] is True and cad.ultimo_erro and "OSError" in cad.ultimo_erro
        assert "falha@exemplo.com" in (Path(p) / "falhas_de_envio.log").read_text(encoding="utf-8")
        SMTPFalso.falhar = False
        cad.registrar("volta@exemplo.com", True)
        assert "falha@exemplo.com" in SMTPFalso.enviadas[-1].get_content()

    with tempfile.TemporaryDirectory() as p:  # sem SMTP configurado: só guarda
        cad = novo(p, cd.ConfigCadastro())
        assert cad.registrar("so@guardar.com", True)[0] and not SMTPFalso.enviadas and cad.total() == 1

    # tela de cadastro no app
    from streamlit.testing.v1 import AppTest
    import modelos

    antigos = {k: os.environ.get(k) for k in ("PDEA_CADASTRO_ATIVO", "PDEA_CADASTRO_CONTATO", "SMTP_USER", "SMTP_PASS", "PDEA_CADASTRO_DESTINO")}
    original = (cd.ARQUIVO, modelos.buscar_modelo)
    try:
        with tempfile.TemporaryDirectory() as p:
            cd.ARQUIVO = Path(p) / "app.csv"
            for k in ("SMTP_USER", "SMTP_PASS", "PDEA_CADASTRO_DESTINO"):
                os.environ.pop(k, None)
            os.environ["PDEA_CADASTRO_ATIVO"], os.environ["PDEA_CADASTRO_CONTATO"] = "true", "contato@exemplo.com"
            modelos.buscar_modelo = lambda *a, **k: (_ for _ in ()).throw(modelos.ErroBuscaModelo("simulado"))
            def novo_app():
                return AppTest.from_file("app.py", default_timeout=60).run()

            at = novo_app()
            assert not at.exception and len(at.text_input) == 1 and len(at.checkbox) == 1
            assert not any("CAPITAIS" in str(c.value) for c in at.caption)  # o painel não aparece antes do cadastro
            at = novo_app(); at.text_input[0].set_value("ruim"); at.checkbox[0].check(); at.button[0].click().run()
            assert at.error and "cadastro_ok" not in at.session_state  # e-mail inválido
            at = novo_app(); at.text_input[0].set_value("pessoa@exemplo.com"); at.button[0].click().run()
            assert at.error and "cadastro_ok" not in at.session_state  # sem consentimento não entra
            at = novo_app(); at.text_input[0].set_value("pessoa@exemplo.com"); at.checkbox[0].check(); at.button[0].click().run()
            assert "cadastro_ok" in at.session_state and not at.exception
            assert "pessoa@exemplo.com" in (Path(p) / "app.csv").read_text(encoding="utf-8")
            desligado = AppTest.from_file("app.py", default_timeout=60)
            os.environ["PDEA_CADASTRO_ATIVO"] = "false"
            desligado.run()
            assert not desligado.exception and len(desligado.text_input) <= 1 and not any("Bem-vindo" in str(s.value) for s in desligado.subheader)
    finally:
        cd.ARQUIVO, modelos.buscar_modelo = original
        for k, val in antigos.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    print("Testes do cadastro de e-mail (validação, consentimento, SMTP simulado, limite, falhas, tela): OK")


def testes_mapa_alcance() -> None:
    """Anéis de 30, 50 e 100 km ao redor de cada capital: vermelho, laranja e amarelo; sem capturar o mouse."""
    import re

    import app as pdea_app
    import analise

    dados = _dados_sinteticos()
    for s in dados.values():
        if isinstance(s, dict):
            s["precip"] = [1.0] * len(s["tempos"])
    tab = analise.consolidar(dados, analise.series_por_unidade(dados, _ParametrosRisco()), 0)

    def mapa(alcance: bool) -> str:
        return pdea_app.criar_mapa(tab, "OpenStreetMap", pdea_app.CORES_NIVEL, True, "12:00", "Best Match", True, False, 0.6, False, alcance).get_root().render()

    h = mapa(True)
    assert pdea_app.ALCANCES_KM == ((30, "#e11d1d"), (50, "#ff8a00"), (100, "#ffd400"))
    assert len(re.findall(r"L\.circle\(", h)) == 3 * len(ESTACOES)
    for km, cor in pdea_app.ALCANCES_KM:
        blocos = [b for b in re.findall(r"L\.circle\(.*?\)\.addTo", h, flags=re.S) if re.search(r'"radius": %d\b' % (km * 1000), b)]
        assert len(blocos) == len(ESTACOES) and all(f'"color": "{cor}"' in b and '"interactive": false' in b for b in blocos), km
    assert all(x in h for x in ("Alcance ao redor da capital", "30 km", "50 km", "100 km"))
    assert "L.circle(" not in mapa(False) and "Alcance ao redor da capital" not in mapa(False)
    print("Testes do alcance de 30, 50 e 100 km (vermelho, laranja, amarelo): OK")


def testes_ampliados() -> None:
    # Heurística ampliada: pontos limitados, sem efeito com peso 0 ou sem dados extras.
    assert ajuste_extras(None) == 0 and ajuste_extras({}) == 0
    assert ajuste_extras({"precip": 6.0}) == 7.0 and ajuste_extras({"rajada": 55.0}) == 4.0
    assert ajuste_extras({"gradiente": 28.0}) == 4.0 and ajuste_extras({"nivel0": 3800.0}) == 2.0
    assert ajuste_extras({"nivel0": 5600.0}) == -2.0
    assert ajuste_extras({"precip": 20.0, "rajada": 90.0, "gradiente": 35.0, "nivel0": 3000.0}) == 20.0  # limite superior
    extras = {"precip": 6.0, "rajada": 55.0, "gradiente": 28.0, "nivel0": 4800.0}
    base = calcular_risco(1500, -3, -20)[0]
    assert calcular_risco(1500, -3, -20, ParametrosRisco(), extras)[0] == base  # peso 0: ignora os extras
    ampliada = calcular_risco(1500, -3, -20, ParametrosRisco(peso_extras=1.0), extras)[0]
    assert ampliada == min(100, base + 15.0), (base, ampliada)
    assert calcular_risco(1500, -3, -20, ParametrosRisco(peso_extras=1.0), None)[0] == base  # sem extras: igual
    assert calcular_risco(1500, -3, -20, ParametrosRisco(peso_extras=2.0), extras)[0] > ampliada

    # Fatores regionais (config_regioes.json é neutro) e extras dentro das séries.
    from analise import carregar_regioes, consolidar, series_por_unidade

    regioes = carregar_regioes()
    assert len(regioes) == 27 and all(v == (1.0, 1.0) for v in regioes.values())
    dados = _dados_sinteticos()
    neutro = series_por_unidade(dados, ParametrosRisco(), regioes)
    forte = series_por_unidade(dados, ParametrosRisco(), {**regioes, "RJ": (2.0, 1.0)})
    reduc = "Rio de Janeiro"
    assert sum(v for v in forte[reduc]["rel"] if v is not None) > sum(v for v in neutro[reduc]["rel"] if v is not None)
    sp = next(e["nome"] for e in ESTACOES if e["uf"] == "SP")
    assert forte[sp]["rel"] == neutro[sp]["rel"]  # UF sem ajuste não muda

    # Sem energia (CAPE < 300): LI e baixa inibição não somam pontos altos (ar estável não vira "Moderado").
    assert calcular_risco(200, -3, -10)[0] == 5.0 and calcular_risco(0, -8, 0)[0] == 5.0
    assert calcular_risco(350, -3, -10)[0] == 10 + 22 + 20  # com energia, a regra é a original
    assert calcular_risco(200, 1.0, -10)[0] == 5.0

    # Gate de chuva (Nordeste): score reduzido só quando não há chuva prevista na janela; sem dado, não aplica.
    from analise import carregar_gate, explicar_hora, janela_frente, multiplicador_do_gate

    assert janela_frente([0, 0, 2, 0, 0, 0], 2) == [2, 2, 2, 0, 0, 0] and janela_frente([None, None], 1) == [None, None]
    from analise import GatePrecipitacao

    assert carregar_gate().ufs == frozenset()  # no PDEA o gate vem desligado: a chuva já faz parte do escore (CAPE x chuva)
    gate = GatePrecipitacao(ufs=frozenset({"CE", "RN", "PE", "SE", "BA"}), limiar_mm_h=1.0, janela_h=3, multiplicador=0.5)
    assert {"CE", "RN", "PE", "SE", "BA"} <= set(gate.ufs) and "RJ" not in gate.ufs and gate.multiplicador == 0.5
    assert multiplicador_do_gate(0.0, "CE", gate) == 0.5 and multiplicador_do_gate(0.5, "CE", gate) == 0.5
    assert multiplicador_do_gate(1.5, "CE", gate) is None
    assert multiplicador_do_gate(None, "CE", gate) is None and multiplicador_do_gate(0.0, "RJ", gate) is None
    assert calcular_risco(1500, -3, -30, multiplicador_gate=0.5)[0] == 0.5 * (22 + 22 + 10)
    seco = _dados_sinteticos()
    for s in seco.values():
        if isinstance(s, dict):
            s["precip"] = [0.0] * len(s["tempos"])
    chuvoso = {n: ({**s, "precip": [1.0] * len(s["tempos"])} if isinstance(s, dict) else s) for n, s in seco.items()}
    ce = next(e["nome"] for e in ESTACOES if e["uf"] == "CE")
    rj = next(e["nome"] for e in ESTACOES if e["uf"] == "RJ")
    from analise import gate_relativo

    assert gate_relativo(seco[ce_nome_g := next(e["nome"] for e in ESTACOES if e["uf"] == "CE")], "CE", gate)[0] is True
    assert gate_relativo(chuvoso[ce_nome_g], "CE", gate)[0] is False
    assert gate_relativo(seco[ce_nome_g], "RJ", gate)[0] is None
    s_seco = series_por_unidade(seco, ParametrosRisco(), None, gate)
    s_chuva = series_por_unidade(chuvoso, ParametrosRisco(), None, gate)
    s_sem = series_por_unidade(seco, ParametrosRisco(), None, None)
    assert s_seco[rj]["rel"] == s_sem[rj]["rel"]  # fora do Nordeste o gate não age
    tab_seca = consolidar(seco, s_seco, 27)
    assert tab_seca.loc[tab_seca["Capital"] == ce, "Ajuste chuva"].iloc[0] == "reduzido"
    assert tab_seca.loc[tab_seca["Capital"] == rj, "Ajuste chuva"].iloc[0] == ""
    assert s_chuva[ce]["rel"] == s_sem[ce]["rel"]  # com chuva prevista, o score fica igual
    k = 27
    assert abs(s_seco[ce]["rel"][k] - 0.5 * s_sem[ce]["rel"][k]) <= 0.06  # sem chuva: metade
    explic = explicar_hora(seco[ce], k, "CE", ParametrosRisco(), None, gate)
    assert explic["gate_aplicado"] and abs(explic["score"] - s_seco[ce]["rel"][k]) < 1e-9
    assert explic["pts_cape"] + explic["pts_li"] + explic["pts_cin"] == explic["subtotal"]
    sem_dado = explicar_hora({k_: v for k_, v in seco[ce].items() if k_ != "precip"}, k, "CE", ParametrosRisco(), None, gate)
    assert sem_dado["gate_sem_dado"] and not sem_dado["gate_aplicado"] and sem_dado["score"] == s_sem[ce]["rel"][k]

    # Consulta com extras: sucesso, erro 400 (não derruba o núcleo) e eixo de tempo diferente.
    ok = buscar_modelo("best_match", _ClienteFalso("ok"))
    assert ok["_extras"] is True and ok[ESTACOES[0]["nome"]]["precip"] == [1.0] * 6
    ruim = buscar_modelo("best_match", _ClienteFalso("erro400"))
    assert ruim["_extras"] is False and "400" in ruim["_erro_extras"] and ruim[ESTACOES[0]["nome"]]["cape"][5] == 500.0
    desalinhado = buscar_modelo("best_match", _ClienteFalso("tempo_diferente"))
    assert desalinhado["_extras"] is False and "precip" not in desalinhado[ESTACOES[0]["nome"]]
    assert buscar_modelo("best_match", _ClienteFalso(), extras=False)["_erro_extras"] == "desligadas"
    tab = consolidar(ok, series_por_unidade(ok, ParametrosRisco(peso_extras=1.0)), 0)
    assert tab["Precip. (mm/h)"].iloc[0] == 1.0 and abs(tab["Gradiente 850–500 (°C)"].iloc[0] - 26.0) < 1e-9

    # Histórico: grava uma vez por hora cheia, consulta, exporta e limpa.
    import historico

    with tempfile.TemporaryDirectory() as pasta:
        banco = Path(pasta) / "h.sqlite"
        agora = datetime.now(TZ_BRASILIA)
        h = _dados_sinteticos(horas=60, atual=3)
        h["_obtido_em"] = agora.isoformat()
        assert historico.registrar(h, "best_match", banco) is True
        assert historico.registrar(h, "best_match", banco) is False  # mesma hora cheia
        h2 = {**h, "_obtido_em": (agora + timedelta(hours=1)).isoformat()}
        assert historico.registrar(h2, "best_match", banco) is True
        info = historico.status(banco)
        assert info["execucoes"] == 2 and info["linhas"] == 2 * len(ESTACOES) * 49, info
        assert historico.registrar(h, "gfs_seamless", banco) is True and historico.status(banco)["modelos"] == ["best_match", "gfs_seamless"]
        # linhas de "valido" no passado distante não existem (dados sintéticos de 2026-08-25): consulta por período amplo
        saida = Path(pasta) / "x.csv"
        assert historico.exportar_csv(saida, "best_match", caminho=banco) == 2 * len(ESTACOES) * 49
        cabecalho = saida.read_text(encoding="utf-8-sig").splitlines()[0]
        assert cabecalho.startswith("modelo;execucao;unidade;valido;horas") and cabecalho.endswith("score")
        assert len(historico.evolucao_previsao(reduc, "best_match", "2026-08-26T00:00", caminho=banco)) == 2
        # O score recalculado do histórico usa o mesmo método do painel (CAPE x chuva).
        banco2 = Path(pasta) / "g.sqlite"
        seco_h = {n: ({**s, "precip": [0.0] * len(s["tempos"])} if isinstance(s, dict) else s) for n, s in _dados_sinteticos(60, 3).items()}
        seco_h["_obtido_em"] = agora.isoformat()
        assert historico.registrar(seco_h, "best_match", banco2)
        ce_nome = next(e["nome"] for e in ESTACOES if e["uf"] == "CE")
        r_ce = historico.serie_realizada(ce_nome, "best_match", 3650, caminho=banco2)
        assert len(r_ce) == 1 and r_ce["score"].iloc[0] == 0.0  # sem chuva prevista, CAPE x chuva = 0
        assert historico.limpar(0, banco) > 0 and historico.status(banco)["linhas"] == 0

    # Camadas do mapa: divisas e URL do GOES.
    import json

    estados = json.loads((Path(__file__).resolve().parent / "dados" / "brasil_estados.geojson").read_text(encoding="utf-8"))
    assert len(estados["features"]) == 27
    import app_camadas

    assert app_camadas.url_goes("GOES-East_ABI_Band13_Clean_Infrared").endswith("/{z}/{y}/{x}.png")
    print("Testes da heurística ampliada, regiões, histórico e camadas: OK")


def _cobertura(dados: dict) -> dict:
    """% de valores não nulos por variável e faixas de valores (ignora as chaves que começam com _)."""
    resumo: dict = {}
    for campo, rotulo in (("cape", "CAPE"), ("li", "LI"), ("cin", "CIN"), ("precip", "precip"), ("rajada", "rajada"),
                          ("nivel0", "nível 0°C"), ("t850", "T850"), ("t500", "T500")):
        valores = [v for nome, s in dados.items() if not nome.startswith("_") for v in s.get(campo, [])]
        if not valores:
            continue
        validos = [v for v in valores if v is not None]
        resumo[rotulo] = {
            "validos_pct": round(100 * len(validos) / len(valores), 1),
            "min": min(validos) if validos else None,
            "max": max(validos) if validos else None,
        }
    return resumo


def teste_online(todos_os_modelos: bool = False) -> int:
    """Relatório de sanidade com dados reais. Devolve o número de problemas encontrados."""
    from analise import horas_a_frente, series_por_unidade
    import requests

    problemas = 0
    dados = buscar_modelo("best_match")
    nomes = [n for n in dados if not n.startswith("_")]
    print(f"\n[best_match] {len(nomes)} unidades · referência {dados['_hora_referencia']} · obtido em {dados['_obtido_em']}")
    if len(nomes) != len(ESTACOES) or any(e["nome"] not in dados for e in ESTACOES):
        print("  PROBLEMA: nem todas as unidades vieram na resposta.")
        problemas += 1
    horizonte = horas_a_frente(dados)
    print(f"  horizonte disponível a partir de agora: +{horizonte} h" + ("" if horizonte >= 48 else "  (PROBLEMA: esperado ≥ 48 h)"))
    problemas += int(horizonte < 48)
    serie = dados[ESTACOES[0]["nome"]]
    ref = horario_local(serie["tempos"][serie["idx_atual"]])
    desvio = abs((datetime.now(TZ_BRASILIA) - ref).total_seconds()) / 3600
    print(f"  hora de referência vs. relógio: {desvio:.1f} h de diferença" + ("" if desvio <= 1.5 else "  (PROBLEMA: fuso/horário?)"))
    problemas += int(desvio > 1.5)
    for rotulo, r in _cobertura(dados).items():
        aviso = "" if r["validos_pct"] >= 50 else "  <-- poucos dados"
        print(f"  {rotulo:9s} válidos {r['validos_pct']:5.1f}%  faixa [{r['min']}, {r['max']}]{aviso}")
        problemas += int(rotulo in ("CAPE", "LI", "CIN") and r["validos_pct"] < 50)
    print(f"  variáveis extras: {'OK' if dados['_extras'] else 'INDISPONÍVEIS — ' + str(dados.get('_erro_extras'))}")
    niveis = consolidar_resumo(dados, series_por_unidade)
    print(f"  níveis agora: {niveis}")

    if todos_os_modelos:
        print("\nCobertura por modelo (CAPE / LI / CIN / extras / horas à frente):")
        for modelo_id, (nome, _, _) in MODELOS.items():
            try:
                d = buscar_modelo(modelo_id)
            except Exception as erro:  # um modelo fora do ar não deve impedir o relatório dos demais
                print(f"  {nome:28s} ERRO: {str(erro)[:80]}")
                continue
            c = _cobertura(d)
            def pct(k):
                return f"{c[k]['validos_pct']:5.1f}%" if k in c else "   — "
            print(f"  {nome:28s} {pct('CAPE')} {pct('LI')} {pct('CIN')}  extras {'sim' if d['_extras'] else 'não'}  +{horas_a_frente(d)} h")

    print("\nCamada GOES (NASA GIBS):")
    import app_camadas

    url = app_camadas.url_goes().format(z=3, y=3, x=2)
    try:
        r = requests.get(url, timeout=20)
        tipo = r.headers.get("content-type", "")
        ok = r.status_code == 200 and tipo.startswith("image/")
        print(f"  {r.status_code} {tipo} {len(r.content)} bytes" + ("" if ok else "  <-- PROBLEMA: confira o nome da camada em app_camadas.GOES_CAMADA"))
        problemas += int(not ok)
    except requests.RequestException as erro:
        print(f"  não foi possível consultar: {erro}")
        problemas += 1
    print(f"\n{'Tudo certo.' if problemas == 0 else f'{problemas} ponto(s) de atenção acima.'}")
    return problemas


def consolidar_resumo(dados: dict, series_por_unidade) -> dict:
    from analise import consolidar

    tabela = consolidar(dados, series_por_unidade(dados), 0)
    return tabela["Risco"].value_counts().to_dict()


if __name__ == "__main__":
    pd.set_option("display.width", 160)
    testes_offline()
    testes_capexp()
    testes_glm_ao_vivo()
    testes_cadastro()
    testes_mapa_alcance()
    testes_ampliados()
    if "--online" in sys.argv:
        from modelos import ErroBuscaModelo

        try:
            sys.exit(1 if teste_online(todos_os_modelos="--modelos" in sys.argv) else 0)
        except ErroBuscaModelo as erro:
            print(f"\nNão foi possível consultar a API: {erro}")
            sys.exit(2)
