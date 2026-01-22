# Asterisk-AI-Voice-Agent-FX/src/providers/prompt_metadata_injector.py

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class ContactMetadata:
    title: str = ""
    name: str = ""


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


class PromptMetadataInjector:
    """
    Resuelve metadata de contacto (title/name) desde session_store y aplica sustitución
    de placeholders a textos antes de enviarlos a VoxBridge.

    Placeholders soportados:
      - {title}
      - {name}

    Modo seguro:
      - sanitiza valores (longitud, llaves, saltos de línea)
      - evita KeyError usando format_map con _SafeDict
      - si format falla, devuelve el texto original
    """

    def __init__(
        self,
        default_title: str = "Sr",
        default_name: str = "Cliente",
        max_len: int = 80,
    ) -> None:
        self.default_title = default_title
        self.default_name = default_name
        self.max_len = max_len

    def sanitize(self, v: Any) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        s = s[: self.max_len]
        # Evita romper format() y reduce inyección trivial
        s = s.replace("{", "").replace("}", "")
        s = s.replace("\n", " ").replace("\r", " ")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    async def resolve_contact(self, session_store: Any, call_id: Optional[str]) -> ContactMetadata:
        """
        Intenta extraer __title/__name (o equivalentes) de la sesión asociada al call_id.

        No asume un schema único: prueba metadata dict, variables dict, y atributos directos.
        """
        if not session_store or not call_id:
            return ContactMetadata()

        session = None
        try:
            # Este método aparece en tu código actual (se usa en otros flujos del provider)
            session = await session_store.get_by_call_id(call_id)
        except Exception:
            session = None

        if not session:
            return ContactMetadata()

        title = ""
        name = ""

        # A) metadata dict (si existe)
        md = getattr(session, "metadata", None)
        if isinstance(md, dict):
            title = title or md.get("title") or md.get("__title") or md.get("CONTACT_TITLE") or ""
            name = name or md.get("name") or md.get("__name") or md.get("CONTACT_NAME") or ""

        # B) diccionarios alternativos de variables
        for attr in ("variables", "vars", "channel_vars", "channel_variables"):
            d = getattr(session, attr, None)
            if isinstance(d, dict):
                title = title or d.get("__title") or d.get("CONTACT_TITLE") or d.get("title") or ""
                name = name or d.get("__name") or d.get("CONTACT_NAME") or d.get("name") or ""

        # C) atributos directos
        for a in ("contact_title", "CONTACT_TITLE", "__title", "title"):
            if not title and hasattr(session, a):
                title = getattr(session, a) or ""
        for a in ("contact_name", "CONTACT_NAME", "__name", "name"):
            if not name and hasattr(session, a):
                name = getattr(session, a) or ""

        return ContactMetadata(
            title=self.sanitize(title),
            name=self.sanitize(name),
        )

    def apply(self, text: str, contact: ContactMetadata, use_defaults: bool = True) -> str:
        """
        Aplica placeholders {title}/{name} a 'text'.
        Si use_defaults=True, rellena con defaults si están vacíos.
        """
        if not text or not isinstance(text, str):
            return text

        title = contact.title
        name = contact.name

        if use_defaults:
            if not title:
                title = self.default_title
            if not name:
                name = self.default_name

        values: Dict[str, str] = {
            "title": self.sanitize(title),
            "name": self.sanitize(name),
        }

        try:
            return text.format_map(_SafeDict(values))
        except Exception:
            # No rompas la llamada por un template malformado
            return text

    async def resolve_and_apply(
        self,
        session_store: Any,
        call_id: Optional[str],
        *,
        think_prompt: str,
        greeting: Optional[str] = None,
        use_defaults: bool = True,
    ) -> Tuple[str, Optional[str], ContactMetadata]:
        """
        Helper para DeepgramProvider:
          - resuelve contact metadata
          - aplica a think_prompt y greeting
          - retorna (think_prompt_aplicado, greeting_aplicado, contact)
        """
        contact = await self.resolve_contact(session_store, call_id)
        applied_prompt = self.apply(think_prompt, contact, use_defaults=use_defaults)
        applied_greeting = self.apply(greeting, contact, use_defaults=use_defaults) if greeting else greeting
        return applied_prompt, applied_greeting, contact
