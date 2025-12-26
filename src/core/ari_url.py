from __future__ import annotations

from typing import Optional


def build_ari_base_url(
    *,
    ari_base_url: Optional[str],
    scheme: Optional[str],
    host: str,
    port: int,
) -> str:
    """
    Construye la base URL de ARI sin hardcodear HTTP.

    Prioridad:
    1) ari_base_url explícito (si existe)
    2) {scheme}://{host}:{port}/ari

    Normaliza:
    - sin trailing slash
    - garantiza sufijo /ari
    """
    if ari_base_url:
        url = str(ari_base_url).strip().rstrip("/")
        if not url.endswith("/ari"):
            url = f"{url}/ari"
        return url

    s = (scheme or "http").strip().lower()
    return f"{s}://{host}:{port}/ari"