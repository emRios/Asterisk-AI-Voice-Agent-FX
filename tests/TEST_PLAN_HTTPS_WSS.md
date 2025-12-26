# Plan de Pruebas y Matriz de Casos — HTTPS/WSS y Herramientas de Negocio

Objetivo
- Validar, únicamente mediante pruebas automatizadas en tests/, los cambios para:
  - Soporte http/https y ws/wss en cliente ARI, URL builder y engines.
  - Inyección segura de credenciales y transporte ARI vía configuración (ENV primero).
  - Tool de negocio de notificación de eventos a colas.
- Sin I/O de red real ni Redis real: usar mocks/stubs. Sin modificar código de producción.

Alcance
- Framework: pytest (con pytest-asyncio).
- Mocks: websockets.connect, aiohttp.ClientSession, ssl.create_default_context, cliente Redis.
- monkeypatch para variables de entorno y funciones/métodos.
- Cobertura sobre líneas y ramas críticas: http/https, ws/wss, TLS (CA e insecure), inyección desde ENV, validaciones del tool.

Componentes bajo prueba y referencias
- URL builder ARI: [python.build_ari_base_url()](src/core/ari_url.py:8)
- Config Asterisk: [python.AsteriskConfig](src/config.py:49)
- Inyección de credenciales/transportes: [python.inject_asterisk_credentials()](src/config/security.py:81)
- Cliente ARI:
  - Constructor: [python.ARIClient.__init__()](src/ari_client.py:33)
  - Conexión HTTP/WS: [python.ARIClient.connect()](src/ari_client.py:118)
  - WebSocket TLS: [python.ARIClient._connect_websocket()](src/ari_client.py:139)
- Engines (construcción base_url y creación ARIClient):
  - [src/engine.py](src/engine.py:150)
  - [src/engine_external_media.py](src/engine_external_media.py:120)
- Tool de negocio:
  - [python.CallEventNotification](src/tools/business/event_notify.py:62)
  - [python.CallEventNotification._publish_to_redis()](src/tools/business/event_notify.py:190)

Estructura de archivos de test propuesta
- [tests/test_ari_url.py](tests/test_ari_url.py)
- [tests/test_config_security_asterisk.py](tests/test_config_security_asterisk.py)
- [tests/test_engine_ari_integration.py](tests/test_engine_ari_integration.py)
- [tests/test_ari_client_urls_tls.py](tests/test_ari_client_urls_tls.py)
- [tests/tools/test_call_event_notify.py](tests/tools/test_call_event_notify.py)
- [tests/conftest.py](tests/conftest.py) (fixtures comunes)

Nota: Se mantienen en paralelo suites existentes (p.ej. tests/tools/business/test_event_notify_tool.py) sin modificarlas; estas nuevas suites se enfocan en escenarios y ramas introducidas por soporte https/wss y seguridad.

Matriz de Casos por Suite

1) tests/test_ari_url.py — [python.build_ari_base_url()](src/core/ari_url.py:8)
- ari_base_url explícito con y sin sufijo "/ari" → normaliza agregando sufijo si falta.
- Sin ari_base_url:
  - Scheme por defecto "http"; tolera mayúsculas/minúsculas y espacios.
  - Construcción: {scheme}://{host}:{port}/ari.
- Parametrización scheme=[http, https, None] × host × port.
- Aceptación: Retorna URL final sin trailing slash redundante y con "/ari" al final.

2) tests/test_config_security_asterisk.py — [python.inject_asterisk_credentials()](src/config/security.py:81), [python.AsteriskConfig](src/config.py:49)
- Sin variables de entorno:
  - Toma host/port/scheme/ari_base_url de YAML/defaults.
  - username/password deben quedar ausentes si ENV no los define.
- Con variables de entorno:
  - ASTERISK_HOST/PORT/SCHEME/ARI_BASE_URL sobrescriben transporte.
  - username/password siempre de ENV (ASTERISK_ARI_USERNAME|ARI_USERNAME, ASTERISK_ARI_PASSWORD|ARI_PASSWORD); ignora YAML si existiera.
- Composición con pydantic AsteriskConfig:
  - Tipos correctos; scheme "https" fluye hacia engines y ARI URL builder.
- Aceptación: Estructura final en config.asterisk consistente con prioridad ENV>YAML; credenciales solo desde ENV.

3) tests/test_engine_ari_integration.py — [src/engine.py](src/engine.py:150), [src/engine_external_media.py](src/engine_external_media.py:120)
- Patch [python.build_ari_base_url()](src/core/ari_url.py:8) para asertar argumentos provenientes de config.asterisk: ari_base_url, scheme, host, port.
- Patch al constructor [python.ARIClient.__init__()](src/ari_client.py:33) para verificar que recibe el base_url devuelto por build_ari_base_url; no realizar conexiones.
- Escenarios:
  - Solo http (scheme=http, sin ari_base_url).
  - Solo https (scheme=https, sin ari_base_url).
  - ari_base_url explícito (prioridad sobre host/port/scheme).
- Aceptación: Engine pasa correctamente el base_url esperado y no intenta conexiones reales.

4) tests/test_ari_client_urls_tls.py — [python.ARIClient.__init__()](src/ari_client.py:33), [python.ARIClient.connect()](src/ari_client.py:118), [python.ARIClient._connect_websocket()](src/ari_client.py:139)
- Constructor:
  - Para base_url con http → ws://.../events con query api_key/app/subscribe.
  - Para base_url con https → wss://.../events.
  - Inválidos: esquemas distintos de http/https → ValueError.
- _connect_websocket (mock websockets.connect, ssl.create_default_context):
  - Con wss y ARI_TLS_INSECURE=true → ssl_ctx.verify_mode=ssl.CERT_NONE y check_hostname=False.
  - Con ARI_TLS_CA_FILE=...pem → ssl_ctx.load_verify_locations llamado correctamente.
  - ping_interval/ping_timeout desde ENV.
- connect() (mock aiohttp.ClientSession.get y _connect_websocket):
  - status=200 → crea http_session, self.running=True, self._connected=True.
  - Excepción en WS → cierra http_session y re-lanza.
- Aceptación: URLs y parámetros TLS/WS producidos conforme a ENV y base_url, sin I/O real.

5) tests/tools/test_call_event_notify.py — [python.CallEventNotification](src/tools/business/event_notify.py:62), [python.CallEventNotification._publish_to_redis()](src/tools/business/event_notify.py:190)
- validate_parameters:
  - PURCHASE_INTENT_HIGH sin intent_score → ValueError.
  - intent_score fuera de [0,100] → ValueError (min y max).
  - priority fuera de {low, medium, high, critical} → ValueError.
- _generate_event_id: determinismo bajo timestamp fijo (patch datetime.utcnow).
- _build_payload:
  - Incluye caller_id y conversation_step si session disponible; elimina claves None.
- _publish_to_redis:
  - FakeRedis.xadd(...) devuelve id establecido.
  - Simula duplicado (redis.ResponseError) → retorna "DUPLICATE".
- execute:
  - backend=none → ignored.
  - enabled_event_types restringe ejecución (类型 no habilitado → ignored).
  - backend=redis → patch _publish_to_redis para devolver ID; verificar respuesta con status=success e id.
- Aceptación: Cobertura de validaciones, componer payload, manejo de duplicados y rutas de ejecución.

Fixtures y utilidades (tests/conftest.py)
- Fixture monkeypatch_env(dict) para set/unset de env en bloque.
- Stubs/Fakes:
  - FakeRedis con xadd(stream, payload, maxlen, approximate, id).
  - FakeAiohttpSession con get(...) que retorna respuesta con .status configurable y soporte async context manager.
  - Patch websockets.connect y ssl.create_default_context.
- AppConfig stub para instanciar Engine/ExternalMedia sin side-effects:
  - Solo campos mínimos para asterisk (host, port, scheme, ari_base_url, username, password, app_name).

Ejecución local
- Ejecutar todas las suites:
  - pytest -q
- Ejecutar con cobertura:
  - coverage run -m pytest
  - coverage report -m
- Variables de entorno relevantes (ejemplos de prueba):
  - ARI_TLS_INSECURE=true
  - ARI_TLS_CA_FILE=tests/fixtures/ca.pem
  - ARI_WS_PING_INTERVAL=5
  - ARI_WS_PING_TIMEOUT=10
  - ASTERISK_HOST, ASTERISK_PORT, ASTERISK_SCHEME, ARI_BASE_URL
  - ASTERISK_ARI_USERNAME / ARI_USERNAME, ASTERISK_ARI_PASSWORD / ARI_PASSWORD

Criterios de aceptación
- Suites y casos listados creados y ejecutables con pytest sin dependencias externas reales.
- Cobertura sobre ramas críticas (http/https y ws/wss, TLS).
- Credenciales exclusivamente desde ENV en inyección de seguridad.
- Tests documentan cualquier necesidad de seam con TODO en el propio test, sin tocar producción.

Notas y restricciones
- No editar código de producción. Sólo crear/editar contenido bajo tests/.
- Mocks reemplazarán cualquier operación de red o Redis.
- Donde sea necesario, parametrizar tests para maximizar cobertura de ramas.

Próximos entregables (en archivos de código de test)
- [tests/test_ari_url.py](tests/test_ari_url.py)
- [tests/test_config_security_asterisk.py](tests/test_config_security_asterisk.py)
- [tests/test_engine_ari_integration.py](tests/test_engine_ari_integration.py)
- [tests/test_ari_client_urls_tls.py](tests/test_ari_client_urls_tls.py)
- [tests/tools/test_call_event_notify.py](tests/tools/test_call_event_notify.py)
- [tests/conftest.py](tests/conftest.py)

Opcional — Configuración mínima CI (ejemplo)
- GitHub Actions job (pseudo):
  - set up Python 3.11
  - pip install -r requirements-dev.txt (pytest, pytest-asyncio, coverage, etc.)
  - run: coverage run -m pytest
  - run: coverage report -m