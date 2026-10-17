from uva.plugins.baixar_video import download_video
from uva.plugins.testing import run


def test_usage_without_url():
    reply = run(download_video)
    assert reply.startswith("Uso: /baixar_video <url>")
    assert "/baixar <url>" in reply


def test_invalid_url():
    reply = run(download_video, "instagram.com/reel/abc123")
    assert reply.startswith("Uso:")
    assert "http:// ou https://" in reply


def test_rejects_private_host():
    reply = run(download_video, "http://127.0.0.1/video.mp4")
    assert "endereços locais" in reply
    assert "URL pública" in reply


def test_instagram_url_is_recognized_with_clear_limitations():
    reply = run(download_video, "https://www.instagram.com/reel/abc123")
    assert "Plataforma: Instagram" in reply
    assert "não baixei o arquivo" in reply
    assert "sem DRM" in reply
    assert "backend de mídia seguro" in reply


def test_x_url_is_recognized():
    reply = run(download_video, "https://x.com/usuario/status/123")
    assert "Plataforma: X/Twitter" in reply
    assert "autenticação" in reply


def test_unknown_public_platform_gets_extensible_backend_message():
    reply = run(download_video, "https://example.com/video.mp4")
    assert "plataforma não cadastrada" in reply
    assert "backend novo" in reply


def test_english_replies():
    assert run(download_video, lang="en").startswith("Usage: /download_video <url>")
    assert "http:// or https://" in run(download_video, "x.com/a", lang="en")
    assert "local, private or internal" in run(download_video, "http://10.0.0.1/v", lang="en")
    reply = run(download_video, "https://www.instagram.com/reel/abc123", lang="en")
    assert "Platform: Instagram" in reply
    assert "dedicated backend" in reply
    assert "I didn't download the file" in reply
    assert "unregistered platform" in run(download_video, "https://example.com/v.mp4", lang="en")
