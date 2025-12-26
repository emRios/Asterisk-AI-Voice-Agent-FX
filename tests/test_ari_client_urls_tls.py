import ssl
import pytest
import websockets

from src.ari_client import ARIClient


class TestARIClientInitUrls:
    def test_init_http_ws_urls(self):
        """
        base_url http debe producir:
        - http_url = http://host:port/ari
        - ws_url con esquema ws:// y query api_key/app/subscribe
        """
        client = ARIClient("user", "pass", "http://pbx.local:8088/ari", "my app")
        assert client.http_url == "http://pbx.local:8088/ari"
        assert client.ws_url.startswith("ws://pbx.local:8088/ari/events")
        assert "api_key=user:pass" in client.ws_url
        assert "app=my%20app" in client.ws_url
        assert "subscribeAll=true" in client.ws_url
        assert "subscribe=ChannelAudioFrame" in client.ws_url

    def test_init_https_wss_urls(self):
        """
        base_url https debe producir wss://.../events
        """
        client = ARIClient("u", "p", "https://host.example:8089/ari", "app")
        assert client.http_url == "https://host.example:8089/ari"
        assert client.ws_url.startswith("wss://host.example:8089/ari/events")

    def test_init_invalid_scheme_raises(self):
        """
        Esquemas distintos de http/https deben fallar.
        """
        with pytest.raises(ValueError):
            ARIClient("u", "p", "ftp://host:21/ari", "app")
        with pytest.raises(ValueError):
            ARIClient("u", "p", "ws://host:8088/ari", "app")


class TestARIClientConnectWebsocketTLS:
    @pytest.mark.asyncio
    async def test_connect_websocket_tls_insecure(self, set_env, patch_ssl_default_context, patch_websockets_connect):
        """
        Con wss y ARI_TLS_INSECURE=true:
        - ssl_ctx.verify_mode = CERT_NONE
        - ssl_ctx.check_hostname = False
        - ping_interval/ping_timeout desde ENV
        """
        set_env({
            "ARI_TLS_INSECURE": "true",
            "ARI_TLS_CA_FILE": None,
            "ARI_WS_PING_INTERVAL": "5",
            "ARI_WS_PING_TIMEOUT": "7",
        })

        client = ARIClient("u", "p", "https://pbx.example:8089/ari", "app")
        await client._connect_websocket()

        # Verifica parámetros con los que se llamó websockets.connect
        call = patch_websockets_connect.last_call
        assert call["url"].startswith("wss://pbx.example:8089/ari/events")
        assert call["ping_interval"] == 5.0
        assert call["ping_timeout"] == 7.0

        # Verifica contexto SSL creado y con flags inseguros
        assert len(patch_ssl_default_context.created) == 1
        ctx = patch_ssl_default_context.created[0]
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False

    @pytest.mark.asyncio
    async def test_connect_websocket_tls_with_ca_file(self, set_env, patch_ssl_default_context, patch_websockets_connect):
        """
        Con ARI_TLS_CA_FILE definido:
        - ssl.create_default_context debe construirse con cafile=valor
        """
        ca_path = "tests/fixtures/ca.pem"
        set_env({
            "ARI_TLS_INSECURE": None,
            "ARI_TLS_CA_FILE": ca_path,
            "ARI_WS_PING_INTERVAL": "10",
            "ARI_WS_PING_TIMEOUT": "20",
        })

        client = ARIClient("u", "p", "https://secure.example:8089/ari", "app")
        await client._connect_websocket()

        call = patch_websockets_connect.last_call
        assert call["url"].startswith("wss://secure.example:8089/ari/events")
        assert call["ping_interval"] == 10.0
        assert call["ping_timeout"] == 20.0

        assert len(patch_ssl_default_context.created) == 1
        ctx = patch_ssl_default_context.created[0]
        # Nuestro fixture registra el cafile con el que se creó el contexto
        assert ctx.cafile == ca_path


class TestARIClientConnectLifecycle:
    @pytest.mark.asyncio
    async def test_connect_success_sets_state(
        self,
        set_env,
        patch_aiohttp_client_session,
        patch_websockets_connect,
    ):
        """
        connect():
        - realiza GET a http_url/asterisk/info (status=200)
        - llama _connect_websocket y deja running=True y is_connected=True
        - crea http_session y websocket
        """
        set_env({
            "ARI_WS_PING_INTERVAL": "3",
            "ARI_WS_PING_TIMEOUT": "9",
        })

        client = ARIClient("u", "p", "http://pbx.local:8088/ari", "app")
        await client.connect()

        assert client.running is True
        assert client.is_connected is True
        assert client.http_session is not None
        assert client.websocket is not None

        # Verifica que se haya creado la sesión con auth básica
        assert len(patch_aiohttp_client_session.created) == 1
        sess = patch_aiohttp_client_session.created[0]
        assert hasattr(sess, "auth")

    @pytest.mark.asyncio
    async def test_connect_ws_error_closes_http_session(
        self,
        patch_aiohttp_client_session,
        monkeypatch,
    ):
        """
        Si falla la conexión WS:
        - http_session debe cerrarse y limpiarse (None)
        - se debe propagar la excepción
        """
        async def raise_connect(*args, **kwargs):
            raise RuntimeError("ws failed")

        monkeypatch.setattr(websockets, "connect", raise_connect)

        client = ARIClient("u", "p", "http://pbx.local:8088/ari", "app")
        with pytest.raises(RuntimeError):
            await client.connect()

        # debe haberse cerrado la sesión http (Engine limpia a None en excepción)
        assert client.http_session is None
        assert client.running is False
        assert client.is_connected is False