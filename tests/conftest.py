import os
import ssl
import types
from types import SimpleNamespace
from datetime import datetime
from typing import Any, Dict, Optional

import pytest


# ============== ENV HELPERS ==============

@pytest.fixture
def set_env(monkeypatch):
    """
    Aplica variables de entorno dentro del test.
    Uso:
        set_env({"FOO": "bar", "BAZ": None})  # set FOO, unset BAZ
    """
    def _apply(env_map: Dict[str, Optional[str]]):
        for k, v in env_map.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
    return _apply


# ============== SSL / WEBSOCKETS PATCHES ==============

@pytest.fixture
def patch_ssl_default_context(monkeypatch):
    """
    Parchea ssl.create_default_context y retorna un objeto con:
      - created: lista de contextos creados
      - FakeSSLContext: clase de contexto con flags check_hostname y verify_mode
    """
    created = []

    class FakeSSLContext:
        def __init__(self, cafile: Optional[str] = None):
            self.cafile = cafile
            self.verify_mode = ssl.CERT_REQUIRED
            self.check_hostname = True
            self.load_verify_locations_called = False
            self.loaded_cafile = None

        def load_verify_locations(self, cafile: Optional[str] = None):
            self.load_verify_locations_called = True
            self.loaded_cafile = cafile

    def fake_create_default_context(cafile: Optional[str] = None):
        ctx = FakeSSLContext(cafile=cafile)
        created.append(ctx)
        return ctx

    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)
    return SimpleNamespace(created=created, FakeSSLContext=FakeSSLContext)


@pytest.fixture
def patch_websockets_connect(monkeypatch):
    """
    Parchea websockets.connect para capturar parámetros y evitar I/O real.
    Retorna un objeto con:
      - last_call: dict con los últimos argumentos usados
      - connect: coroutine que simula la conexión (devuelve un dummy)
    """
    import websockets

    class ConnectSpy:
        def __init__(self):
            self.last_call: Dict[str, Any] = {}

        async def connect(self, url: str, *, ping_interval=None, ping_timeout=None, ssl=None):
            self.last_call = {
                "url": url,
                "ping_interval": ping_interval,
                "ping_timeout": ping_timeout,
                "ssl": ssl,
            }
            # Objeto dummy para asignar a client.websocket
            return SimpleNamespace(closed=False)

    spy = ConnectSpy()
    monkeypatch.setattr(websockets, "connect", spy.connect)
    return spy


# ============== AIOHTTP PATCH ==============

@pytest.fixture
def patch_aiohttp_client_session(monkeypatch):
    """
    Parchea aiohttp.ClientSession para evitar I/O real. Controla el status de respuesta vía:
        patch.default_status = 200
    Retorna:
      - patch: objeto con default_status y created (instancias creadas)
    """
    import aiohttp

    class FakeResponse:
        def __init__(self, status: int):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClientSession:
        def __init__(self, auth=None):
            self.auth = auth
            self.closed = False

        # Importante: retornar un objeto que implemente el protocolo de async context manager,
        # NO una coroutine (para que "async with session.get(...)" funcione sin await).
        def get(self, url: str):
            return FakeResponse(patch.default_status)

        async def close(self):
            self.closed = True

    patch = SimpleNamespace(default_status=200, created=[])

    def factory(auth=None):
        sess = FakeClientSession(auth=auth)
        patch.created.append(sess)
        return sess

    monkeypatch.setattr(aiohttp, "ClientSession", factory)
    return patch


# ============== REDIS FAKES ==============

@pytest.fixture
def fake_redis():
    """
    Fake Redis client con soporte xadd y simulación de duplicados via atributo raise_duplicate.
    Uso:
        r = fake_redis()
        r._xadd_result = "1720000000-1"
        r.raise_duplicate = True  # para simular ResponseError por ID duplicado
    """
    import redis

    class FakeRedis:
        def __init__(self):
            self._xadd_result = "1-0"
            self.raise_duplicate = False
            self.last_xadd = None

        async def xadd(self, stream: str, payload: Dict[str, Any], maxlen: int, approximate: bool, id: str):
            self.last_xadd = {
                "stream": stream,
                "payload": payload,
                "maxlen": maxlen,
                "approximate": approximate,
                "id": id,
            }
            if self.raise_duplicate:
                raise redis.ResponseError("ERR The ID specified in XADD is equal or smaller than the target stream top item (duplicate)")
            return self._xadd_result

    return FakeRedis()


@pytest.fixture
def fake_redis_client_factory(fake_redis):
    """
    Retorna una coroutine factory para imitar context.get_redis_client()
    Uso:
        client_coro = fake_redis_client_factory
        client = await client_coro()  # -> fake_redis
    """
    async def _get_client():
        return fake_redis
    return _get_client


# ============== TOOL CONTEXT STUBS ==============

@pytest.fixture
def tool_execution_context_stub(fake_redis_client_factory):
    """
    Crea un contexto mínimo compatible con CallEventNotification.execute().
    - logger: usa print wrappers
    - call_id: "call-123"
    - get_config: retorna dict básico con sección tools
    - session_store: None por defecto
    - get_redis_client: coroutine que retorna fake redis
    """
    class Logger:
        def info(self, msg, **kw):  # pragma: no cover
            print("INFO", msg, kw)
        def warning(self, msg, **kw):  # pragma: no cover
            print("WARN", msg, kw)
        def error(self, msg, **kw):  # pragma: no cover
            print("ERROR", msg, kw)

    context = types.SimpleNamespace()
    context.logger = Logger()
    context.call_id = "call-123"
    context.provider_name = "unittest"
    context.agent_id = "agent-1"

    base_tool_config = {
        "tools": {
            "CallEventNotification": {
                "queue_backend": "redis",
                "enabled_event_types": [
                    "PURCHASE_INTENT_HIGH",
                    "TRANSFER_REQUESTED",
                    "HARD_REJECTION",
                    "SOFT_REJECTION",
                ],
                "redis": {
                    "stream_name": "call_events",
                    "max_stream_length": 10000,
                }
            }
        }
    }

    context.get_config = lambda: base_tool_config
    context.session_store = None
    context.get_redis_client = fake_redis_client_factory
    return context


# ============== TIME FREEZE (datetime.utcnow) ==============

@pytest.fixture
def freeze_utcnow(monkeypatch):
    """
    Parchea datetime.utcnow en el módulo src.tools.business.event_notify para producir un timestamp fijo.
    """
    fixed = datetime(2025, 1, 1, 0, 0, 0)

    class FixedDatetime:
        @classmethod
        def utcnow(cls):
            return fixed

    # event_notify hace "from datetime import datetime"
    monkeypatch.setattr("src.tools.business.event_notify.datetime", FixedDatetime, raising=False)
    return fixed


# ============== APP CONFIG STUB PARA ENGINES ==============

@pytest.fixture
def app_config_stub():
    """
    Crea un stub mínimo de AppConfig para instanciar Engine sin efectos laterales.
    Atributos:
        - asterisk: host, port, scheme, ari_base_url, username, password, app_name
    """
    def _factory(
        host="127.0.0.1",
        port=8088,
        scheme="http",
        ari_base_url=None,
        username="user",
        password="pass",
        app_name="ai-voice-agent",
    ):
        asterisk = SimpleNamespace(
            host=host,
            port=port,
            scheme=scheme,
            ari_base_url=ari_base_url,
            username=username,
            password=password,
            app_name=app_name,
        )
        # Proveer atributos mínimos utilizados por Engine.__init__
        audio_transport = "audiosocket"  # valor simbólico aceptado por StreamingPlaybackManager
        audiosocket = SimpleNamespace(format="ulaw")
        streaming = SimpleNamespace(
            # Diagnostics/taps
            diag_enable_taps=False,
            diag_pre_secs=0,
            diag_post_secs=0,
            diag_out_dir="",
            # Streaming timing and buffers (required attrs accessed directamente por Engine)
            jitter_buffer_ms=100,
            keepalive_interval_ms=1000,
            connection_timeout_ms=5000,
            fallback_timeout_ms=10000,
            chunk_size_ms=200,
            min_start_ms=0,
            low_watermark_ms=0,
            provider_grace_ms=0,
            logging_level="info",
            # Otros usados condicionalmente
            egress_swap_mode="auto",
            continuous_stream=True,
            sample_rate=8000,
            # Normalizer config (dict esperado por Engine)
            normalizer={"enabled": True, "target_rms": 1400, "max_gain_db": 9.0},
        )
        cfg = SimpleNamespace(
            asterisk=asterisk,
            audio_transport=audio_transport,
            audiosocket=audiosocket,
            streaming=streaming,
        )

        # Proveer método .dict() compatible con Engine -> TransportOrchestrator (espera dicts con .get)
        def _dict():
            return {
                "asterisk": {
                    "host": host,
                    "port": port,
                    "scheme": scheme,
                    "ari_base_url": ari_base_url,
                    "username": username,
                    "password": password,
                    "app_name": app_name,
                },
                "audio_transport": audio_transport,
                "audiosocket": {
                    "format": audiosocket.format,
                },
                "streaming": {
                    "diag_enable_taps": streaming.diag_enable_taps,
                    "diag_pre_secs": streaming.diag_pre_secs,
                    "diag_post_secs": streaming.diag_post_secs,
                    "diag_out_dir": streaming.diag_out_dir,
                    "jitter_buffer_ms": streaming.jitter_buffer_ms,
                    "keepalive_interval_ms": streaming.keepalive_interval_ms,
                    "connection_timeout_ms": streaming.connection_timeout_ms,
                    "fallback_timeout_ms": streaming.fallback_timeout_ms,
                    "chunk_size_ms": streaming.chunk_size_ms,
                    "min_start_ms": streaming.min_start_ms,
                    "low_watermark_ms": streaming.low_watermark_ms,
                    "provider_grace_ms": streaming.provider_grace_ms,
                    "logging_level": streaming.logging_level,
                    "egress_swap_mode": streaming.egress_swap_mode,
                    "continuous_stream": streaming.continuous_stream,
                    "sample_rate": streaming.sample_rate,
                    "normalizer": streaming.normalizer,
                },
            }
        setattr(cfg, "dict", _dict)

        return cfg
    return _factory


# ============== SHIMS PARA IMPORTS LEGADOS ==============
# Algunos módulos importan utilidades desde 'src.utils.*' (rutas legadas que pueden no existir).
# Inyectamos shims en sys.modules al cargar conftest (antes de importar tests):
# - src.utils.ari.build_ari_base_url -> redirige a src.core.ari_url.build_ari_base_url
# - src.utils.audio_capture.AudioCaptureManager -> stub para satisfacer import en engine
# - src.utils.email_validator.EmailValidator -> stub para satisfacer import en tools
#
# No modifica código de producción.
import sys as _sys
import types as _types
try:
    from src.core.ari_url import build_ari_base_url as _build_ari_base_url

    # Asegurar paquete padre 'src.utils' con __path__ para permitir submódulos
    _utils_pkg = _sys.modules.get("src.utils")
    if _utils_pkg is None or not isinstance(_utils_pkg, _types.ModuleType):
        _utils_pkg = _types.ModuleType("src.utils")
        # Marcar como paquete
        setattr(_utils_pkg, "__path__", [])
        _sys.modules["src.utils"] = _utils_pkg
    else:
        # Si ya existe pero no es paquete, convertirlo en paquete asignando __path__
        if not hasattr(_utils_pkg, "__path__"):
            setattr(_utils_pkg, "__path__", [])

    # Submódulo 'ari' con build_ari_base_url
    _ari_mod = _types.ModuleType("src.utils.ari")
    _ari_mod.build_ari_base_url = _build_ari_base_url
    setattr(_utils_pkg, "ari", _ari_mod)
    _sys.modules["src.utils.ari"] = _ari_mod

    # Submódulo 'audio_capture' con clase stub AudioCaptureManager
    _audio_mod = _types.ModuleType("src.utils.audio_capture")
    class AudioCaptureManager:  # stub
        def __init__(self, base_dir=None, keep_files=False, *args, **kwargs):
            self.base_dir = base_dir
            self.keep_files = keep_files
        # Métodos no usados en estas suites, definidos para compatibilidad
        def start(self):  # pragma: no cover
            return None
        def stop(self):  # pragma: no cover
            return None
    _audio_mod.AudioCaptureManager = AudioCaptureManager
    setattr(_utils_pkg, "audio_capture", _audio_mod)
    _sys.modules["src.utils.audio_capture"] = _audio_mod

    # Submódulo 'email_validator' con clase stub EmailValidator
    _email_mod = _types.ModuleType("src.utils.email_validator")
    class EmailValidator:  # stub
        @staticmethod
        def is_valid(email: str) -> bool:
            # Validación mínima para no romper imports; no se usa en estas suites
            return isinstance(email, str) and "@" in email
    _email_mod.EmailValidator = EmailValidator
    setattr(_utils_pkg, "email_validator", _email_mod)
    _sys.modules["src.utils.email_validator"] = _email_mod

except Exception:
    # Permitir que el import de conftest no falle en entornos parciales
    pass