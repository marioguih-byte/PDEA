"""Alertas do PDEA por e-mail e/ou webhook (Teams, Slack, Discord, WhatsApp via gateway).

Executa uma verificação e termina — pensado para rodar a cada 30 min em um agendador
(GitHub Actions, cron, Task Scheduler). Só alerta na *transição* para o nível
configurado (ou acima), e rearma quando a capital volta a ficar abaixo dele.

Exemplos:
    python alertas.py --dry-run
    python alertas.py --modelo ecmwf_ifs025 --nivel Alto --antecedencia 6
    python alertas.py --nivel Moderado

Variáveis de ambiente (todas opcionais; sem elas, apenas imprime):
    ALERTA_WEBHOOK_URL                      URL do webhook (recebe JSON {"text": "...", "content": "..."})
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERTA_EMAIL_DE, ALERTA_EMAIL_PARA (vários, separados por vírgula)
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

import requests

from analise import carregar_gate, carregar_regioes, consolidar, rotulo_horario, series_por_unidade
from modelos import MODELOS, TZ_BRASILIA, buscar_modelo
from risco_raio import ORDEM, ParametrosRisco

ESTADO_PADRAO = Path("estado_alertas.json")


def avaliar(
    tabela,
    estado_anterior: dict[str, Any],
    nivel_minimo: str,
    antecedencia_h: int,
    series: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Decide quais capitais devem gerar alerta e devolve o novo estado.

    Uma capital alerta quando (a) já está no nível mínimo ou acima e antes não estava, ou
    (b) vai atingi-lo nas próximas ``antecedencia_h`` horas e ainda não havia sido avisada.
    """
    limite = ORDEM[nivel_minimo]
    novo_estado: dict[str, Any] = {}
    alertas: list[dict[str, Any]] = []
    for _, linha in tabela.iterrows():
        nome = linha["Capital"]
        nivel_agora = linha["Risco"]
        ordem_agora = ORDEM.get(nivel_agora, -1)
        anterior = estado_anterior.get(nome, {})
        ja_avisada = bool(anterior.get("avisada", False))

        pico = linha["Pico 24 h"]
        iminente = False
        if antecedencia_h > 0 and series is not None and ordem_agora < limite:
            rel = series[nome]["rel"][1 : antecedencia_h + 1]
            iminente = any(v is not None and v >= _score_minimo(limite) for v in rel)

        no_nivel = ordem_agora >= limite
        deve_alertar = (no_nivel or iminente) and not ja_avisada
        if deve_alertar:
            alertas.append(
                {
                    "unidade": nome,
                    "uf": linha["UF"],
                    "nivel": nivel_agora,
                    "score": None if linha["Score"] != linha["Score"] else float(linha["Score"]),
                    "tipo": "atingiu" if no_nivel else f"previsto em até {antecedencia_h} h",
                    "pico": None if pico != pico else float(pico),
                    "hora_pico": linha["Hora do pico"],
                }
            )
        novo_estado[nome] = {"nivel": nivel_agora, "avisada": (no_nivel or iminente) and (ja_avisada or deve_alertar)}
    return alertas, novo_estado


def _score_minimo(ordem_nivel: int) -> float:
    """Menor score que pertence ao nível de ordem informada."""
    from risco_raio import NIVEIS_RISCO

    return 0.0 if ordem_nivel == 0 else float(NIVEIS_RISCO[ordem_nivel - 1][0])


def montar_mensagem(alertas: list[dict[str, Any]], titulo: str, referencia: str) -> str:
    linhas = [f"⚡ {titulo} — referência {referencia}"]
    for a in sorted(alertas, key=lambda x: -(x["score"] or 0)):
        score = "—" if a["score"] is None else f"{a['score']:.0f}"
        pico = "" if a["pico"] is None else f" · pico {a['pico']:.0f} às {a['hora_pico']}"
        linhas.append(f"• {a['unidade']} ({a['uf']}): {a['nivel']} (score {score}) — {a['tipo']}{pico}")
    linhas.append("Heurística de CAPE/LI/CIN; não substitui alertas oficiais.")
    return "\n".join(linhas)


def enviar_webhook(url: str, texto: str) -> None:
    resposta = requests.post(url, json={"text": texto, "content": texto}, timeout=20)
    resposta.raise_for_status()


def enviar_email(texto: str, assunto: str) -> None:
    host = os.environ["SMTP_HOST"]
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = os.environ.get("ALERTA_EMAIL_DE", os.environ.get("SMTP_USER", "pdea@localhost"))
    msg["To"] = ", ".join(p.strip() for p in os.environ["ALERTA_EMAIL_PARA"].split(",") if p.strip())
    msg.set_content(texto)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as servidor:
        servidor.starttls()
        if os.environ.get("SMTP_USER"):
            servidor.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
        servidor.send_message(msg)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modelo", default="best_match", choices=list(MODELOS), help="modelo principal")
    ap.add_argument("--nivel", default="Alto", choices=["Moderado", "Alto", "Severo"], help="nível mínimo para alertar")
    ap.add_argument("--antecedencia", type=int, default=3, help="avisar também quando o nível for previsto em até N horas (0 = desliga)")
    ap.add_argument("--estado", type=Path, default=ESTADO_PADRAO, help="arquivo JSON com o estado anterior")
    ap.add_argument("--dry-run", action="store_true", help="não envia nada; só imprime e não grava o estado")
    args = ap.parse_args(argv)

    dados = buscar_modelo(args.modelo)  # inclui a chuva prevista, necessária ao escore CAPE × chuva
    if not dados.get("_extras", False):
        print("Aviso: sem dados de precipitação; o escore CAPE × chuva não pode ser calculado e nenhuma capital será avaliada.")
    series = series_por_unidade(dados, ParametrosRisco(), carregar_regioes(), carregar_gate())
    tabela = consolidar(dados, series, 0)

    anterior = json.loads(args.estado.read_text(encoding="utf-8")) if args.estado.exists() else {}
    alertas, novo_estado = avaliar(tabela, anterior, args.nivel, args.antecedencia, series)

    referencia = rotulo_horario(dados, 0)
    if not alertas:
        print(f"[{datetime.now(TZ_BRASILIA):%d/%m %H:%M}] Sem novos alertas (referência {referencia}).")
    else:
        if args.antecedencia > 0:
            titulo = f"PDEA — {len(alertas)} capital(is) em {args.nivel} ou acima (agora ou em até {args.antecedencia} h)"
        else:
            titulo = f"PDEA — {len(alertas)} capital(is) em {args.nivel} ou acima"
        texto = montar_mensagem(alertas, titulo, referencia)
        print(texto)
        if not args.dry_run:
            if os.environ.get("ALERTA_WEBHOOK_URL"):
                enviar_webhook(os.environ["ALERTA_WEBHOOK_URL"], texto)
                print("Webhook enviado.")
            if os.environ.get("SMTP_HOST") and os.environ.get("ALERTA_EMAIL_PARA"):
                enviar_email(texto, titulo)
                print("E-mail enviado.")

    if not args.dry_run:
        args.estado.write_text(json.dumps(novo_estado, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
