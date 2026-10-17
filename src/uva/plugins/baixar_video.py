"""Comando para analisar URLs de vídeo e orientar download seguro.

A interface atual de plugins da UVA retorna apenas texto; ela não pode anexar
arquivos, gravar mídia em disco ou executar extratores externos. Por isso este
plugin faz a parte segura que cabe ao módulo: valida a URL, identifica a
plataforma por um backend extensível e explica claramente as limitações.
"""

import re
from dataclasses import dataclass

from uva.plugins.api import PluginContext, command


@dataclass(frozen=True)
class VideoBackend:
    name: str
    domains: tuple[str, ...]
    status: str
    note: str


BACKENDS = (
    VideoBackend(
        name="Instagram",
        domains=("instagram.com",),
        status="Suporte condicional: Reels e posts públicos podem exigir backend dedicado.",
        note="O Instagram frequentemente bloqueia download direto, login obrigatório ou conteúdo privado.",
    ),
    VideoBackend(
        name="X/Twitter",
        domains=("x.com", "twitter.com"),
        status="Suporte condicional: vídeos públicos podem variar conforme a postagem.",
        note="O X pode exigir autenticação, limitar acesso regional ou entregar mídia por URLs temporárias.",
    ),
    VideoBackend(
        name="TikTok",
        domains=("tiktok.com",),
        status="Suporte condicional: vídeos públicos podem exigir backend dedicado.",
        note="Alguns vídeos usam proteção, login, região ou bloqueios anti-automação.",
    ),
    VideoBackend(
        name="YouTube",
        domains=("youtube.com", "youtu.be"),
        status="Suporte restrito: apenas conteúdo permitido e sem DRM.",
        note="DRM, conteúdo pago, privado, ao vivo ou protegido por termos da plataforma não deve ser baixado.",
    ),
    VideoBackend(
        name="Facebook",
        domains=("facebook.com", "fb.watch"),
        status="Suporte condicional: apenas vídeos públicos quando a plataforma permitir.",
        note="Conteúdo privado, grupos, login obrigatório e restrições regionais costumam impedir o download.",
    ),
    VideoBackend(
        name="Vimeo",
        domains=("vimeo.com",),
        status="Suporte condicional: depende das permissões definidas pelo autor.",
        note="Vídeos com privacidade, senha, incorporação restrita ou DRM não devem ser baixados.",
    ),
)


URL_RE = re.compile(r"^https?://([^/?#]+)(?:[/?#].*)?$", re.IGNORECASE)
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def clean_host(raw_host: str) -> str:
    host = raw_host.lower().strip()
    if "@" in host:
        host = host.split("@")[-1]
    if ":" in host:
        host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def extract_host(url: str) -> str | None:
    match = URL_RE.match(url.strip())
    if not match:
        return None
    return clean_host(match.group(1))


def is_private_host(host: str) -> bool:
    if host in ("localhost", "0.0.0.0") or host.endswith(".local"):
        return True
    if not IP_RE.match(host):
        return False
    parts = [int(part) for part in host.split(".")]
    if any(part > 255 for part in parts):
        return True
    first = parts[0]
    second = parts[1]
    return first == 10 or first == 127 or (first == 192 and second == 168) or (first == 172 and 16 <= second <= 31)


def matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def find_backend(host: str) -> VideoBackend | None:
    for backend in BACKENDS:
        for domain in backend.domains:
            if matches_domain(host, domain):
                return backend
    return None


def usage_message() -> str:
    return (
        "Uso: /baixar_video <url> ou /baixar <url>\n"
        "Envie uma URL pública de vídeo. Ex.: /baixar_video https://www.instagram.com/reel/abc123"
    )


@command(
    "baixar_video",
    "Analisa uma URL pública de vídeo e informa suporte/limitações de download seguro",
    aliases=("baixar", "download_video", "download"),
    usage="<url>",
    examples=("/baixar_video https://www.instagram.com/reel/abc123", "/baixar https://x.com/usuario/status/123"),
)
def download_video(ctx: PluginContext) -> str:
    if len(ctx.args) != 1:
        return usage_message()

    url = ctx.args[0].strip()
    host = extract_host(url)
    if host is None:
        return usage_message() + "\n\nA URL precisa começar com http:// ou https:// e não pode conter espaços."

    if is_private_host(host):
        return (
            "Não vou acessar endereços locais, privados ou internos.\n"
            "Envie uma URL pública de uma plataforma de vídeo."
        )

    backend = find_backend(host)
    if backend is None:
        platform = "plataforma não cadastrada"
        status = "Sem backend específico cadastrado para este domínio."
        note = "Se a URL for pública e permitida, um backend novo pode ser adicionado para esta plataforma."
    else:
        platform = backend.name
        status = backend.status
        note = backend.note

    return (
        f"🔎 URL analisada: {url}\n"
        f"🌐 Plataforma: {platform}\n"
        f"📌 Status: {status}\n\n"
        "⚠️ Limitações importantes:\n"
        "- Este plugin só consegue retornar texto; ele não pode anexar arquivos nem salvar vídeos.\n"
        "- Downloads só devem acontecer para conteúdo público, autorizado e sem DRM.\n"
        "- Termos de uso, privacidade, login obrigatório e bloqueios da plataforma podem impedir a extração.\n\n"
        f"ℹ️ Observação: {note}\n\n"
        "Resultado: não baixei o arquivo daqui. A URL foi validada para encaminhamento a um backend de mídia seguro quando ele estiver habilitado no núcleo da UVA."
    )
