"""Cadastro de e-mail na entrada do painel, com aviso de privacidade e consentimento (LGPD).

Fluxo: o visitante informa o e-mail e marca o consentimento; o painel guarda o cadastro em ``cadastro/cadastros.csv`` (no
servidor) e envia uma notificação por e-mail ao responsável. O módulo não depende do Streamlit.

Configuração (``.streamlit/secrets.toml`` no Streamlit, seção ``[cadastro]``, ou variáveis de ambiente):

  destino      PDEA_CADASTRO_DESTINO     e-mail que recebe as notificações
  smtp_host    SMTP_HOST                 padrão smtp.gmail.com
  smtp_port    SMTP_PORT                 padrão 587 (STARTTLS)
  smtp_user    SMTP_USER                 conta que envia
  smtp_pass    SMTP_PASS                 senha (no Gmail, uma "senha de app"; nunca coloque no código)
  remetente    PDEA_CADASTRO_REMETENTE   opcional (padrão: smtp_user)
  contato      PDEA_CADASTRO_CONTATO     opcional: e-mail mostrado no aviso para pedir a remoção do cadastro
  ativo        PDEA_CADASTRO_ATIVO       "false" desliga a tela de cadastro (por exemplo, no desenvolvimento)

Cuidados: o e-mail do visitante só vai no CORPO da mensagem (nunca em cabeçalhos), o formato é validado (sem quebras de
linha), há um limite de notificações por hora e o arquivo CSV neutraliza fórmulas de planilha.
"""

from __future__ import annotations

import csv
import os
import re
import smtplib
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

ARQUIVO = Path(__file__).resolve().parent / "cadastro" / "cadastros.csv"
VERSAO_AVISO = "2026-10-08"
LIMITE_POR_HORA = 20
TENTATIVAS = 3
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}$")
TZ_BRASILIA = timezone(timedelta(hours=-3))


def normalizar(email: str) -> str:
    return (email or "").strip().lower()


def email_valido(email: str) -> bool:
    e = normalizar(email)
    return 0 < len(e) <= 254 and ".." not in e and not e.startswith(".") and bool(EMAIL_RE.match(e))


@dataclass(frozen=True)
class ConfigCadastro:
    destino: str = ""
    host: str = "smtp.gmail.com"
    porta: int = 587
    usuario: str = ""
    senha: str = ""
    remetente: str = ""
    contato: str = ""
    ativo: bool = True

    @property
    def pode_enviar(self) -> bool:
        return bool(self.destino and self.usuario and self.senha)


def _texto(valor: Any) -> str:
    return "" if valor is None else str(valor).strip()


def ler_config(secrets: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None) -> ConfigCadastro:
    """Lê a configuração dos Secrets (seção [cadastro]) e, na falta deles, das variáveis de ambiente."""
    s, e = dict(secrets or {}), (os.environ if env is None else env)

    def pega(chave_secret: str, var_env: str, padrao: str = "") -> str:
        return _texto(s.get(chave_secret)) or _texto(e.get(var_env)) or padrao

    ativo_txt = (_texto(s.get("ativo")) or _texto(e.get("PDEA_CADASTRO_ATIVO")) or "true").lower()
    try:
        porta = int(pega("smtp_port", "SMTP_PORT", "587"))
    except ValueError:
        porta = 587
    usuario = pega("smtp_user", "SMTP_USER")
    return ConfigCadastro(
        destino=normalizar(pega("destino", "PDEA_CADASTRO_DESTINO")),
        host=pega("smtp_host", "SMTP_HOST", "smtp.gmail.com"),
        porta=porta,
        usuario=usuario,
        senha=pega("smtp_pass", "SMTP_PASS"),
        remetente=pega("remetente", "PDEA_CADASTRO_REMETENTE") or usuario,
        contato=pega("contato", "PDEA_CADASTRO_CONTATO"),
        ativo=ativo_txt not in ("false", "0", "nao", "não", "off", "no"),
    )


def _celula_csv(valor: str) -> str:
    """Impede que o Excel interprete o conteúdo como fórmula."""
    return "'" + valor if valor[:1] in ("=", "+", "-", "@", "\t", "\r") else valor


class Cadastro:
    """Guarda os cadastros e notifica o responsável por e-mail (em segundo plano)."""

    def __init__(self, config: ConfigCadastro, arquivo: Optional[Path] = None, smtp_fabrica: Callable[..., Any] = smtplib.SMTP,
                 assincrono: bool = True, agora: Callable[[], float] = time.time, espera: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.arquivo = Path(arquivo) if arquivo else ARQUIVO  # resolvido na criação, para os testes poderem trocar o caminho
        self._smtp, self.assincrono, self._agora, self._espera = smtp_fabrica, assincrono, agora, espera
        self._lock = threading.Lock()
        self._envios: deque[float] = deque()
        self._suprimidos: list[str] = []
        self.ultimo_erro: Optional[str] = None
        self.enviados = 0
        self._vistos = self._carregar_vistos()

    # ------------------------------------------------------------ registro
    def _carregar_vistos(self) -> set[str]:
        try:
            with self.arquivo.open(encoding="utf-8", newline="") as f:
                return {normalizar(l["email"].lstrip("'")) for l in csv.DictReader(f) if l.get("email")}
        except (OSError, KeyError):
            return set()

    def total(self) -> int:
        return len(self._vistos)

    def registrar(self, email: str, consentiu: bool) -> tuple[bool, str]:
        """Devolve (ok, mensagem). Só aceita e-mail válido com consentimento marcado."""
        if not consentiu:
            return False, "Para entrar, marque que concorda com o uso do e-mail."
        if not email_valido(email):
            return False, "Informe um e-mail válido, por exemplo nome@exemplo.com."
        e = normalizar(email)
        with self._lock:
            if e in self._vistos:
                return True, "ja_cadastrado"  # já registrado: entra sem nova notificação
            self._gravar(e)
            self._vistos.add(e)
        if self.config.pode_enviar:
            if self.assincrono:
                threading.Thread(target=self._notificar, args=(e,), daemon=True, name="pdea-cadastro").start()
            else:
                self._notificar(e)
        return True, "ok"

    def _gravar(self, email: str) -> None:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        novo = not self.arquivo.exists()
        with self.arquivo.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if novo:
                w.writerow(["criado_em_utc", "email", "consentimento", "versao_do_aviso"])
            w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), _celula_csv(email), "sim", VERSAO_AVISO])

    # ------------------------------------------------------------ notificação
    def _pode_enviar_agora(self) -> bool:
        agora = self._agora()
        while self._envios and agora - self._envios[0] > 3600:
            self._envios.popleft()
        return len(self._envios) < LIMITE_POR_HORA

    def _montar(self, email: str, atrasados: list[str]) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = "PDEA: novo cadastro de e-mail"
        msg["From"] = self.config.remetente or self.config.usuario
        msg["To"] = self.config.destino
        hora = datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y %H:%M")
        corpo = f"Novo cadastro no PDEA\n\nE-mail: {email}\nData e hora (Brasília): {hora}\nTotal de cadastros no servidor: {self.total()}\n"
        if atrasados:
            corpo += (f"\nCadastros anteriores que não foram notificados por causa do limite de {LIMITE_POR_HORA} avisos por hora:\n"
                      + "\n".join(f"- {a}" for a in atrasados) + "\n")
        corpo += "\nTodos os cadastros ficam também em cadastro/cadastros.csv no servidor do painel (apagado se o app reiniciar).\n"
        msg.set_content(corpo)
        return msg

    def _notificar(self, email: str) -> bool:
        with self._lock:
            if not self._pode_enviar_agora():
                self._suprimidos.append(email)
                return False
            atrasados, self._suprimidos = self._suprimidos, []
        msg = self._montar(email, atrasados)
        for tentativa in range(1, TENTATIVAS + 1):
            try:
                with self._smtp(self.config.host, self.config.porta, timeout=20) as s:
                    s.starttls()
                    s.login(self.config.usuario, self.config.senha)
                    s.send_message(msg)
                with self._lock:
                    self._envios.append(self._agora())
                    self.enviados += 1
                    self.ultimo_erro = None
                return True
            except Exception as exc:  # noqa: BLE001 - falha de rede/credencial não pode derrubar o painel
                self.ultimo_erro = f"{type(exc).__name__}: {exc}"
                if tentativa < TENTATIVAS:
                    self._espera(2.0 * tentativa)
        with self._lock:
            self._suprimidos = atrasados + [email] + self._suprimidos  # tenta de novo junto com o próximo cadastro
        try:
            falhas = self.arquivo.with_name("falhas_de_envio.log")
            with falhas.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {email} {self.ultimo_erro}\n")
        except OSError:
            pass
        return False
