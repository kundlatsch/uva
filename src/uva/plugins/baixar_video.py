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
    status: dict[str, str]  # per language: {"en": ..., "pt": ...}
    note: dict[str, str]


BACKENDS = (
    VideoBackend(
        name="Instagram",
        domains=("instagram.com",),
        status={
            "en": "Conditional support: public Reels and posts may need a dedicated backend.",
            "pt": "Suporte condicional: Reels e posts públicos podem exigir backend dedicado.",
        },
        note={
            "en": "Instagram often blocks direct downloads, requires login or serves private content.",
            "pt": "O Instagram frequentemente bloqueia download direto, login obrigatório ou conteúdo privado.",
        },
    ),
    VideoBackend(
        name="X/Twitter",
        domains=("x.com", "twitter.com"),
        status={
            "en": "Conditional support: public videos may vary from post to post.",
            "pt": "Suporte condicional: vídeos públicos podem variar conforme a postagem.",
        },
        note={
            "en": "X may require authentication, restrict access by region or serve media through temporary URLs.",
            "pt": "O X pode exigir autenticação, limitar acesso regional ou entregar mídia por URLs temporárias.",
        },
    ),
    VideoBackend(
        name="TikTok",
        domains=("tiktok.com",),
        status={
            "en": "Conditional support: public videos may need a dedicated backend.",
            "pt": "Suporte condicional: vídeos públicos podem exigir backend dedicado.",
        },
        note={
            "en": "Some videos use protection, login, region locks or anti-automation blocks.",
            "pt": "Alguns vídeos usam proteção, login, região ou bloqueios anti-automação.",
        },
    ),
    VideoBackend(
        name="YouTube",
        domains=("youtube.com", "youtu.be"),
        status={
            "en": "Restricted support: only allowed, DRM-free content.",
            "pt": "Suporte restrito: apenas conteúdo permitido e sem DRM.",
        },
        note={
            "en": "DRM, paid, private, live or terms-protected content must not be downloaded.",
            "pt": "DRM, conteúdo pago, privado, ao vivo ou protegido por termos da plataforma não deve ser baixado.",
        },
    ),
    VideoBackend(
        name="Facebook",
        domains=("facebook.com", "fb.watch"),
        status={
            "en": "Conditional support: only public videos, when the platform allows it.",
            "pt": "Suporte condicional: apenas vídeos públicos quando a plataforma permitir.",
        },
        note={
            "en": "Private content, groups, required login and regional restrictions usually prevent the download.",
            "pt": "Conteúdo privado, grupos, login obrigatório e restrições regionais costumam impedir o download.",
        },
    ),
    VideoBackend(
        name="Vimeo",
        domains=("vimeo.com",),
        status={
            "en": "Conditional support: depends on the permissions set by the author.",
            "pt": "Suporte condicional: depende das permissões definidas pelo autor.",
        },
        note={
            "en": "Videos with privacy settings, a password, restricted embedding or DRM must not be downloaded.",
            "pt": "Vídeos com privacidade, senha, incorporação restrita ou DRM não devem ser baixados.",
        },
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


TEXTS = {
    "en": {
        "usage": (
            "Usage: /download_video <url> or /download <url>\n"
            "Send a public video URL. E.g.: /download_video https://www.instagram.com/reel/abc123"
        ),
        "bad_url": "The URL must start with http:// or https:// and can't contain spaces.",
        "private": (
            "I won't access local, private or internal addresses.\n"
            "Send a public URL from a video platform."
        ),
        "unknown_platform": "unregistered platform",
        "unknown_status": "No specific backend registered for this domain.",
        "unknown_note": "If the URL is public and allowed, a new backend can be added for this platform.",
        "report": (
            "🔎 URL analyzed: {url}\n"
            "🌐 Platform: {platform}\n"
            "📌 Status: {status}\n\n"
            "⚠️ Important limitations:\n"
            "- This plugin can only return text; it can't attach files or save videos.\n"
            "- Downloads should only happen for public, authorized, DRM-free content.\n"
            "- Terms of use, privacy, required login and platform blocks may prevent extraction.\n\n"
            "ℹ️ Note: {note}\n\n"
            "Result: I didn't download the file from here. The URL was validated to be handed to a safe media backend once it is enabled in UVA's core."
        ),
    },
    "pt": {
        "usage": (
            "Uso: /baixar_video <url> ou /baixar <url>\n"
            "Envie uma URL pública de vídeo. Ex.: /baixar_video https://www.instagram.com/reel/abc123"
        ),
        "bad_url": "A URL precisa começar com http:// ou https:// e não pode conter espaços.",
        "private": (
            "Não vou acessar endereços locais, privados ou internos.\n"
            "Envie uma URL pública de uma plataforma de vídeo."
        ),
        "unknown_platform": "plataforma não cadastrada",
        "unknown_status": "Sem backend específico cadastrado para este domínio.",
        "unknown_note": "Se a URL for pública e permitida, um backend novo pode ser adicionado para esta plataforma.",
        "report": (
            "🔎 URL analisada: {url}\n"
            "🌐 Plataforma: {platform}\n"
            "📌 Status: {status}\n\n"
            "⚠️ Limitações importantes:\n"
            "- Este plugin só consegue retornar texto; ele não pode anexar arquivos nem salvar vídeos.\n"
            "- Downloads só devem acontecer para conteúdo público, autorizado e sem DRM.\n"
            "- Termos de uso, privacidade, login obrigatório e bloqueios da plataforma podem impedir a extração.\n\n"
            "ℹ️ Observação: {note}\n\n"
            "Resultado: não baixei o arquivo daqui. A URL foi validada para encaminhamento a um backend de mídia seguro quando ele estiver habilitado no núcleo da UVA."
        ),
    },
}


def texts_for(lang: str) -> dict[str, str]:
    return TEXTS.get(lang, TEXTS["en"])


def pick(values: dict[str, str], lang: str) -> str:
    return values.get(lang) or values["en"]


def usage_message(lang: str = "pt") -> str:
    return texts_for(lang)["usage"]


@command(
    "baixar_video",
    {
        "en": "Analyzes a public video URL and reports safe-download support/limitations",
        "pt": "Analisa uma URL pública de vídeo e informa suporte/limitações de download seguro",
    },
    aliases=("baixar", "download_video", "download"),
    usage="<url>",
    examples=("/baixar_video https://www.instagram.com/reel/abc123", "/baixar https://x.com/usuario/status/123"),
)
def download_video(ctx: PluginContext) -> str:
    texts = texts_for(ctx.lang)
    if len(ctx.args) != 1:
        return texts["usage"]

    url = ctx.args[0].strip()
    host = extract_host(url)
    if host is None:
        return texts["usage"] + "\n\n" + texts["bad_url"]

    if is_private_host(host):
        return texts["private"]

    backend = find_backend(host)
    if backend is None:
        platform = texts["unknown_platform"]
        status = texts["unknown_status"]
        note = texts["unknown_note"]
    else:
        platform = backend.name
        status = pick(backend.status, ctx.lang)
        note = pick(backend.note, ctx.lang)

    return texts["report"].format(url=url, platform=platform, status=status, note=note)
