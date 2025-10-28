"""
Foundational pipeline abstractions for composing STT, LLM, and TTS components.

This module defines lightweight async interfaces that individual adapters
(local server, Deepgram, OpenAI, Google, etc.) can implement. The
`PipelineOrchestrator` (see orchestrator.py) uses these contracts to build
per-call pipelines that the Engine can drive without being tied to any single
provider.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Any, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import websockets
except ImportError:
    websockets = None


class Component(ABC):
    """Base class for all pipeline components."""

    async def start(self) -> None:
        """Warm up component resources (optional)."""

    async def stop(self) -> None:
        """Release resources (optional)."""

    async def open_call(self, call_id: str, options: Dict[str, Any]) -> None:
        """Prepare per-call state (optional)."""

    async def close_call(self, call_id: str) -> None:
        """Release per-call state (optional)."""

    def _auto_detect_credentials(self, options: Dict[str, Any]) -> Optional[str]:
        """Smart credential detection from options or environment.
        
        Auto-finds API keys based on component naming patterns:
        - deepgram_stt -> DEEPGRAM_API_KEY
        - openai_llm -> OPENAI_API_KEY
        - google_tts -> GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS
        - local_* -> No credentials needed
        """
        # Check explicit api_key in options first
        if options.get("api_key"):
            return options["api_key"]
        
        # Derive provider name from component_key
        component_key = getattr(self, "component_key", "")
        if not component_key:
            return None
        
        # Extract provider prefix (e.g., "deepgram" from "deepgram_stt")
        provider_prefix = component_key.split("_")[0].upper()
        
        # Local components don't need API keys
        if provider_prefix == "LOCAL":
            return None
        
        # Try common API key patterns
        for suffix in ["_API_KEY", "_KEY"]:
            env_var = f"{provider_prefix}{suffix}"
            api_key = os.getenv(env_var)
            if api_key:
                return api_key
        
        # Special case: Google service account
        if provider_prefix == "GOOGLE":
            creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if creds_file:
                return "service_account"  # Placeholder indicating creds exist
        
        return None
    
    def _extract_base_url(self, options: Dict[str, Any]) -> Optional[str]:
        """Extract base URL from options, handling various naming patterns."""
        for key in ["base_url", "ws_url", "url", "endpoint"]:
            if key in options and options[key]:
                return options[key]
        return None
    
    async def _test_websocket_connection(self, url: str, api_key: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]:
        """Test websocket connectivity with smart header detection."""
        if not websockets:
            return {"healthy": False, "error": "websockets library not available", "details": {}}
        
        headers = []
        if api_key and api_key != "service_account":
            # Auto-detect authorization pattern based on URL
            if "deepgram.com" in url:
                headers.append(("Authorization", f"Token {api_key}"))
            elif "openai.com" in url:
                headers.append(("Authorization", f"Bearer {api_key}"))
                headers.append(("OpenAI-Beta", "realtime=v1"))
            else:
                headers.append(("Authorization", f"Bearer {api_key}"))
        
        try:
            websocket = await asyncio.wait_for(
                websockets.connect(url, extra_headers=headers if headers else None),
                timeout=timeout
            )
            await websocket.close()
            return {"healthy": True, "error": None, "details": {"endpoint": url, "protocol": "websocket"}}
        except asyncio.TimeoutError:
            return {"healthy": False, "error": f"Connection timeout after {timeout}s", "details": {"endpoint": url}}
        except Exception as exc:
            error_str = str(exc)
            if "401" in error_str or "Unauthorized" in error_str:
                detail = "Invalid API key"
            elif "400" in error_str or "Bad Request" in error_str:
                detail = f"Invalid request parameters: {error_str}"
            elif "timeout" in error_str.lower():
                detail = "Connection timeout - service unreachable"
            elif "refused" in error_str.lower():
                detail = "Connection refused - service not running"
            else:
                detail = f"Connection failed: {error_str}"
            return {"healthy": False, "error": detail, "details": {"endpoint": url, "exception": error_str}}
    
    async def _test_http_connection(self, url: str, api_key: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]:
        """Test HTTP/HTTPS connectivity with smart header detection."""
        if not aiohttp:
            return {"healthy": False, "error": "aiohttp library not available", "details": {}}
        
        headers = {}
        if api_key and api_key != "service_account":
            # Auto-detect authorization pattern based on URL
            if "deepgram.com" in url:
                headers["Authorization"] = f"Token {api_key}"
            elif "openai.com" in url:
                headers["Authorization"] = f"Bearer {api_key}"
            elif "googleapis.com" in url or "generativelanguage.googleapis.com" in url:
                headers["Authorization"] = f"Bearer {api_key}"
            else:
                headers["Authorization"] = f"Bearer {api_key}"
        
        # Build test endpoint
        test_endpoint = url
        if "openai.com/v1" in url and not url.endswith("/models"):
            test_endpoint = f"{url.rstrip('/')}/models"
        elif "deepgram.com" in url and not url.endswith("/projects"):
            test_endpoint = f"{url.rstrip('/')}/v1/projects"
        elif "generativelanguage.googleapis.com" in url:
            test_endpoint = f"{url.rstrip('/')}/models"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(test_endpoint, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 401:
                        return {"healthy": False, "error": "Invalid API key", "details": {"status": 401, "endpoint": test_endpoint}}
                    elif resp.status == 403:
                        return {"healthy": False, "error": "API key lacks required permissions", "details": {"status": 403, "endpoint": test_endpoint}}
                    elif resp.status == 429:
                        # Rate limited but key is valid
                        return {"healthy": True, "error": None, "details": {"status": 429, "endpoint": test_endpoint, "note": "Rate limited but API key valid"}}
                    elif resp.status >= 400:
                        text = await resp.text()
                        return {"healthy": False, "error": f"API error: HTTP {resp.status}", "details": {"status": resp.status, "endpoint": test_endpoint, "response": text[:200]}}
                    else:
                        return {"healthy": True, "error": None, "details": {"status": resp.status, "endpoint": test_endpoint, "protocol": "https"}}
        except asyncio.TimeoutError:
            return {"healthy": False, "error": f"Connection timeout after {timeout}s", "details": {"endpoint": test_endpoint}}
        except Exception as exc:
            return {"healthy": False, "error": f"Connection failed: {str(exc)}", "details": {"endpoint": test_endpoint, "exception": str(exc)}}
    
    async def validate_connectivity(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """Smart generic validation that auto-detects URLs, credentials, and protocols.
        
        This default implementation works for 90% of adapters:
        - Auto-finds API keys from environment based on component name
        - Extracts base_url from options (supports base_url, ws_url, url, endpoint)
        - Protocol-aware testing (websocket vs HTTP)
        - Smart header detection based on service domain
        
        Returns dict with:
            - healthy: bool - Whether component is ready
            - error: str - Error message if unhealthy
            - details: Dict[str, Any] - Additional diagnostic info
        
        Adapters can override for custom validation logic.
        """
        # 1. Extract base URL from options
        base_url = self._extract_base_url(options)
        
        # Local components without URLs are considered healthy
        component_key = getattr(self, "component_key", "")
        if component_key.startswith("local_") and not base_url:
            # Local components default to localhost websocket
            base_url = "ws://127.0.0.1:8765/ws"
        
        if not base_url:
            return {"healthy": False, "error": "No base_url/ws_url configured in options", "details": {"checked_keys": ["base_url", "ws_url", "url", "endpoint"]}}
        
        # 2. Auto-detect credentials
        api_key = self._auto_detect_credentials(options)
        
        # Check if credentials are required but missing
        if not component_key.startswith("local_") and not api_key:
            provider_prefix = component_key.split("_")[0].upper() if component_key else "UNKNOWN"
            return {"healthy": False, "error": f"No API credentials found (checked {provider_prefix}_API_KEY env var)", "details": {"component": component_key}}
        
        # 3. Protocol-based testing
        if base_url.startswith(("ws://", "wss://")):
            return await self._test_websocket_connection(base_url, api_key)
        elif base_url.startswith(("http://", "https://")):
            return await self._test_http_connection(base_url, api_key)
        else:
            return {"healthy": False, "error": f"Unknown protocol in URL: {base_url}", "details": {"url": base_url}}


class STTComponent(Component):
    """Speech-to-text component."""

    @abstractmethod
    async def transcribe(
        self,
        call_id: str,
        audio_pcm16: bytes,
        sample_rate_hz: int,
        options: Dict[str, Any],
    ) -> str:
        """Return a transcript for the provided PCM16 audio buffer."""


class LLMComponent(Component):
    """Language model component."""

    @abstractmethod
    async def generate(
        self,
        call_id: str,
        transcript: str,
        context: Dict[str, Any],
        options: Dict[str, Any],
    ) -> str:
        """Generate a response given transcript + context."""


class TTSComponent(Component):
    """Text-to-speech component."""

    @abstractmethod
    async def synthesize(
        self,
        call_id: str,
        text: str,
        options: Dict[str, Any],
    ) -> AsyncIterator[bytes]:
        """Yield audio frames (μ-law or PCM) for the supplied text."""


