import pytest

from types import SimpleNamespace

import src.engine as engine_module
from src.engine import Engine


class DummyStreamingPlaybackManager:
    def __init__(self, *args, **kwargs):
        # Accept any args to avoid coupling with production signature
        # Provide minimal attributes/methods used by Engine.__init__()
        pass

    def set_transport(self, audio_transport=None, audiosocket_format=None):
        # No-op; Engine may call this to pre-seed formats
        return None


class TestEngineAriIntegration:
    def test_engine_builds_base_url_http(self, app_config_stub, monkeypatch):
        """
        Verifica que Engine:
        - Pase a build_ari_base_url los valores de config.asterisk (scheme=http, sin ari_base_url)
        - Construiya ARIClient con el base_url retornado por build_ari_base_url
        - No intente iniciar conexiones (solo constructor)
        """
        captured = {"builder_args": None, "ari_base_url": None}

        def fake_builder(*, ari_base_url, scheme, host, port):
            captured["builder_args"] = dict(
                ari_base_url=ari_base_url, scheme=scheme, host=host, port=port
            )
            return "http://h:8088/ari"

        class DummyARIClient:
            def __init__(self, *, username=None, password=None, base_url=None, app_name=None, **_kw):
                captured["ari_base_url"] = base_url
                # Engine espera poder setear .engine
                self.engine = None
                # Handlers para eventos ARI
                self._handlers = {}

            def on_event(self, event_type, handler):
                self._handlers.setdefault(event_type, []).append(handler)

            # Alias por compatibilidad si Engine usara add_event_handler en algún punto
            def add_event_handler(self, event_type, handler):
                self.on_event(event_type, handler)

        monkeypatch.setattr(engine_module, "build_ari_base_url", fake_builder)
        monkeypatch.setattr(engine_module, "ARIClient", DummyARIClient)
        # Avoid requiring config.audio_transport and other internals
        monkeypatch.setattr(engine_module, "StreamingPlaybackManager", DummyStreamingPlaybackManager)

        cfg = app_config_stub(
            host="127.0.0.1",
            port=8088,
            scheme="http",
            ari_base_url=None,
            username="user",
            password="pass",
            app_name="ai-voice-agent",
        )

        eng = Engine(cfg)

        assert captured["builder_args"] == {
            "ari_base_url": None,
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 8088,
        }
        assert captured["ari_base_url"] == "http://h:8088/ari"
        assert hasattr(eng.ari_client, "engine")

    def test_engine_builds_base_url_https(self, app_config_stub, monkeypatch):
        """
        Engine con scheme=https y sin ari_base_url debe pedir a builder HTTPS.
        """
        captured = {"builder_args": None, "ari_base_url": None}

        def fake_builder(*, ari_base_url, scheme, host, port):
            captured["builder_args"] = dict(
                ari_base_url=ari_base_url, scheme=scheme, host=host, port=port
            )
            return "https://h:8089/ari"

        class DummyARIClient:
            def __init__(self, *, username=None, password=None, base_url=None, app_name=None, **_kw):
                captured["ari_base_url"] = base_url
                self.engine = None
                self._handlers = {}

            def on_event(self, event_type, handler):
                self._handlers.setdefault(event_type, []).append(handler)

            def add_event_handler(self, event_type, handler):
                self.on_event(event_type, handler)

        monkeypatch.setattr(engine_module, "build_ari_base_url", fake_builder)
        monkeypatch.setattr(engine_module, "ARIClient", DummyARIClient)
        # Avoid requiring config.audio_transport and other internals
        monkeypatch.setattr(engine_module, "StreamingPlaybackManager", DummyStreamingPlaybackManager)

        cfg = app_config_stub(
            host="pbx.example.com",
            port=8089,
            scheme="https",
            ari_base_url=None,
            username="user",
            password="pass",
            app_name="ai-voice-agent",
        )

        Engine(cfg)

        assert captured["builder_args"] == {
            "ari_base_url": None,
            "scheme": "https",
            "host": "pbx.example.com",
            "port": 8089,
        }
        assert captured["ari_base_url"] == "https://h:8089/ari"

    def test_engine_uses_explicit_ari_base_url(self, app_config_stub, monkeypatch):
        """
        Engine con ari_base_url explícito debe pasarlo al builder y respetar prioridad de ese valor.
        """
        captured = {"builder_args": None, "ari_base_url": None}

        def fake_builder(*, ari_base_url, scheme, host, port):
            captured["builder_args"] = dict(
                ari_base_url=ari_base_url, scheme=scheme, host=host, port=port
            )
            # El builder también puede normalizar/retornar mismo valor:
            return ari_base_url

        class DummyARIClient:
            def __init__(self, *, username=None, password=None, base_url=None, app_name=None, **_kw):
                captured["ari_base_url"] = base_url
                self.engine = None
                self._handlers = {}

            def on_event(self, event_type, handler):
                self._handlers.setdefault(event_type, []).append(handler)

            def add_event_handler(self, event_type, handler):
                self.on_event(event_type, handler)

        monkeypatch.setattr(engine_module, "build_ari_base_url", fake_builder)
        monkeypatch.setattr(engine_module, "ARIClient", DummyARIClient)
        # Avoid requiring config.audio_transport and other internals
        monkeypatch.setattr(engine_module, "StreamingPlaybackManager", DummyStreamingPlaybackManager)

        explicit = "https://pbx.example.com:8089/ari"
        cfg = app_config_stub(
            host="should-be-ignored-host",
            port=9999,
            scheme="http",
            ari_base_url=explicit,
            username="user",
            password="pass",
            app_name="ai-voice-agent",
        )

        Engine(cfg)

        assert captured["builder_args"] == {
            "ari_base_url": explicit,
            "scheme": "http",
            "host": "should-be-ignored-host",
            "port": 9999,
        }
        assert captured["ari_base_url"] == explicit


# NOTA: Las pruebas de Engine External Media se agregan en un módulo adicional
# una vez confirmado el nombre exacto de su clase pública.
# TODO: ext-engine: añadir pruebas similares parcheando src.engine_external_media.build_ari_base_url
# y src.engine_external_media.ARIClient.