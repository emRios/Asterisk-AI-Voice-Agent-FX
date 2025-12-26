import pytest
from types import SimpleNamespace

import src.engine_external_media as ext_module
from src.engine_external_media import ExternalMediaEngine


def make_config(
    *,
    host="127.0.0.1",
    port=8088,
    scheme="http",
    ari_base_url=None,
    username="user",
    password="pass",
    app_name="ai-voice-agent",
    rtp_host="127.0.0.1",
    rtp_port=18080,
):
    """
    Crea un AppConfig stub mínimo para ExternalMediaEngine sin efectos reales.
    Incluye:
      - asterisk: host, port, scheme, ari_base_url, username, password, app_name
      - rtp: host, port, codec, port_range
      - providers: vacío para evitar inicialización real
      - llm: None (no usado)
    """
    asterisk = SimpleNamespace(
        host=host,
        port=port,
        scheme=scheme,
        ari_base_url=ari_base_url,
        username=username,
        password=password,
        app_name=app_name,
    )
    rtp = SimpleNamespace(
        host=rtp_host,
        port=rtp_port,
        codec="ulaw",
        port_range=None,
    )
    # Nota: providers vacío evita LocalProvider/DeepgramProvider reales
    return SimpleNamespace(asterisk=asterisk, rtp=rtp, providers={}, llm=None)


class DummyARIClient:
    """
    ARIClient fake para ExternalMediaEngine.start():
     - captura base_url recibido
     - no realiza I/O real
    """
    def __init__(self, *, username=None, password=None, base_url=None, app_name=None, **_kw):
        self.username = username
        self.password = password
        self.base_url = base_url
        self.app_name = app_name
        self.connected = False
        self.handlers = {}

    async def connect(self):
        self.connected = True

    async def start_listening(self):
        # No-op para evitar tareas background reales
        return None

    def add_event_handler(self, event_type, handler):
        self.handlers[event_type] = handler


class DummyRTPServer:
    """
    RTPServer fake que no abre sockets reales y captura parámetros.
    """
    def __init__(self, host, port, engine_callback, codec, port_range=None):
        self.host = host
        self.port = port
        self.engine_callback = engine_callback
        self.codec = codec
        self.port_range = port_range
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_external_media_engine_builds_base_url_http(monkeypatch):
    """
    Verifica que ExternalMediaEngine:
    - Pasa a build_ari_base_url los valores de config.asterisk (scheme=http, sin ari_base_url)
    - Construye ARIClient con el base_url retornado por build_ari_base_url
    - No realiza I/O real (RTP/WS parcheados)
    """
    captured = {"builder_args": None, "ari_base_url": None}

    def fake_builder(*, ari_base_url, scheme, host, port):
        captured["builder_args"] = dict(
            ari_base_url=ari_base_url, scheme=scheme, host=host, port=port
        )
        return "http://h:8088/ari"

    monkeypatch.setattr(ext_module, "build_ari_base_url", fake_builder)
    monkeypatch.setattr(ext_module, "ARIClient", DummyARIClient)
    monkeypatch.setattr(ext_module, "RTPServer", DummyRTPServer)

    cfg = make_config(scheme="http", ari_base_url=None, host="127.0.0.1", port=8088)
    eng = ExternalMediaEngine(cfg)

    await eng.start()

    assert captured["builder_args"] == {
        "ari_base_url": None,
        "scheme": "http",
        "host": "127.0.0.1",
        "port": 8088,
    }
    assert isinstance(eng.ari_client, DummyARIClient)
    assert eng.ari_client.base_url == "http://h:8088/ari"
    # Handlers registrados
    assert "StasisStart" in eng.ari_client.handlers
    assert "ChannelDestroyed" in eng.ari_client.handlers
    assert "PlaybackFinished" in eng.ari_client.handlers

    # Asegura que RTP server inició y luego detenemos para limpiar
    assert eng.rtp_server.started is True
    await eng.stop()
    assert eng.rtp_server.stopped is True


@pytest.mark.asyncio
async def test_external_media_engine_builds_base_url_https(monkeypatch):
    """
    ExternalMediaEngine con scheme=https y sin ari_base_url debe pedir HTTPS al builder.
    """
    captured = {"builder_args": None}

    def fake_builder(*, ari_base_url, scheme, host, port):
        captured["builder_args"] = dict(
            ari_base_url=ari_base_url, scheme=scheme, host=host, port=port
        )
        return "https://h:8089/ari"

    monkeypatch.setattr(ext_module, "build_ari_base_url", fake_builder)
    monkeypatch.setattr(ext_module, "ARIClient", DummyARIClient)
    monkeypatch.setattr(ext_module, "RTPServer", DummyRTPServer)

    cfg = make_config(scheme="https", ari_base_url=None, host="pbx.example.com", port=8089)
    eng = ExternalMediaEngine(cfg)
    await eng.start()

    assert captured["builder_args"] == {
        "ari_base_url": None,
        "scheme": "https",
        "host": "pbx.example.com",
        "port": 8089,
    }
    assert eng.ari_client.base_url == "https://h:8089/ari"
    await eng.stop()


@pytest.mark.asyncio
async def test_external_media_engine_uses_explicit_ari_base_url(monkeypatch):
    """
    ExternalMediaEngine con ari_base_url explícito debe respetar la prioridad del valor explícito.
    """
    captured = {"builder_args": None}

    def fake_builder(*, ari_base_url, scheme, host, port):
        captured["builder_args"] = dict(
            ari_base_url=ari_base_url, scheme=scheme, host=host, port=port
        )
        # Retorna el mismo valor explícito (normalización podría ocurrir aquí)
        return ari_base_url

    monkeypatch.setattr(ext_module, "build_ari_base_url", fake_builder)
    monkeypatch.setattr(ext_module, "ARIClient", DummyARIClient)
    monkeypatch.setattr(ext_module, "RTPServer", DummyRTPServer)

    explicit = "https://pbx.example.com:8089/ari"
    cfg = make_config(
        scheme="http",  # debería ser ignorado por explicit
        ari_base_url=explicit,
        host="ignored-host",
        port=9999,
    )
    eng = ExternalMediaEngine(cfg)
    await eng.start()

    assert captured["builder_args"] == {
        "ari_base_url": explicit,
        "scheme": "http",
        "host": "ignored-host",
        "port": 9999,
    }
    assert eng.ari_client.base_url == explicit
    await eng.stop()


# TODO: Si ExternalMediaEngine.start() añadiera más dependencias con efectos secundarios,
# se recomienda introducir puntos de inyección (seams) para facilitar testeo, por ejemplo:
# - Factorys para ARIClient/RTPServer
# - Flags de "dry-run" para evitar crear tareas background.
# Estas notas documentan el seam deseable sin modificar el código de producción.