import os
import pytest

from src.config.security import inject_asterisk_credentials
from src.config import AsteriskConfig


class TestInjectAsteriskCredentials:
    def test_without_env_uses_yaml_defaults_and_no_credentials(self, set_env):
        """
        Sin variables de entorno:
        - Debe mantener transporte desde YAML/defaults
        - username/password deben quedar ausentes (None)
        - app_name toma default "asterisk-ai-voice-agent"
        """
        set_env({
            "ASTERISK_HOST": None,
            "ASTERISK_PORT": None,
            "ASTERISK_SCHEME": None,
            "ARI_BASE_URL": None,
            "ASTERISK_ARI_USERNAME": None,
            "ARI_USERNAME": None,
            "ASTERISK_ARI_PASSWORD": None,
            "ARI_PASSWORD": None,
        })
        config_data = {
            "asterisk": {
                "host": "10.0.0.5",
                "port": 8088,
                "scheme": "http",
                "ari_base_url": None,
                "username": "yaml_user_should_be_ignored",
                "password": "yaml_pass_should_be_ignored",
                # app_name ausente -> default en inject_asterisk_credentials
            }
        }

        inject_asterisk_credentials(config_data)

        a = config_data["asterisk"]
        assert a["host"] == "10.0.0.5"
        assert a["port"] == 8088
        assert a["scheme"] == "http"
        assert a["ari_base_url"] is None
        # credenciales exclusivamente desde ENV -> None si no vienen de ENV
        assert a["username"] is None
        assert a["password"] is None
        assert a["app_name"] == "asterisk-ai-voice-agent"

        # NOTA: No podemos construir AsteriskConfig sin username/password válidos (pydantic requiere str).
        # Esto se valida en el siguiente test con ENV definidos.

    def test_env_overrides_transport_and_credentials(self, set_env):
        """
        Con variables de entorno:
        - ASTERISK_HOST/PORT/SCHEME/ARI_BASE_URL sobrescriben transporte de YAML
        - username/password SIEMPRE desde ENV (ignora YAML)
        - Composición con AsteriskConfig para validar tipos y scheme https
        """
        set_env({
            "ASTERISK_HOST": "pbx.example.com",
            "ASTERISK_PORT": "8089",
            "ASTERISK_SCHEME": "https",
            "ARI_BASE_URL": "https://pbx.example.com:8089/ari",
            "ASTERISK_ARI_USERNAME": "envuser",
            "ASTERISK_ARI_PASSWORD": "envpass",
            "ARI_USERNAME": None,  # redundantes desactivados
            "ARI_PASSWORD": None,
        })

        config_data = {
            "asterisk": {
                "host": "yaml-host",
                "port": 8088,
                "scheme": "http",
                "ari_base_url": None,
                "username": "yaml_user_should_be_ignored",
                "password": "yaml_pass_should_be_ignored",
                "app_name": "ai-voice-agent",
            }
        }

        inject_asterisk_credentials(config_data)
        a = config_data["asterisk"]

        assert a["host"] == "pbx.example.com"
        assert a["port"] == 8089
        assert a["scheme"] == "https"
        assert a["ari_base_url"] == "https://pbx.example.com:8089/ari"
        assert a["username"] == "envuser"
        assert a["password"] == "envpass"
        assert a["app_name"] == "ai-voice-agent"

        # Validación de tipos y campos con pydantic
        cfg = AsteriskConfig(**a)
        assert cfg.host == "pbx.example.com"
        assert cfg.port == 8089
        assert cfg.scheme == "https"
        assert cfg.ari_base_url == "https://pbx.example.com:8089/ari"
        assert isinstance(cfg.username, str)
        assert isinstance(cfg.password, str)

    def test_env_username_password_fallback_aliases(self, set_env):
        """
        Debe aceptar alias ARI_USERNAME/ARI_PASSWORD si no están definidos ASTERISK_ARI_USERNAME/PASSWORD.
        """
        set_env({
            "ASTERISK_HOST": "127.0.0.1",
            "ASTERISK_PORT": "8088",
            "ASTERISK_SCHEME": "http",
            "ARI_BASE_URL": None,
            "ASTERISK_ARI_USERNAME": None,
            "ASTERISK_ARI_PASSWORD": None,
            "ARI_USERNAME": "alias_user",
            "ARI_PASSWORD": "alias_pass",
        })

        config_data = {
            "asterisk": {
                "host": "yaml-host",
                "port": 8088,
                "scheme": "http",
                "username": "yaml_user_should_be_ignored",
                "password": "yaml_pass_should_be_ignored",
            }
        }

        inject_asterisk_credentials(config_data)
        a = config_data["asterisk"]

        assert a["username"] == "alias_user"
        assert a["password"] == "alias_pass"

        # Comprobamos que AsteriskConfig acepta la composición
        cfg = AsteriskConfig(**a)
        assert cfg.username == "alias_user"
        assert cfg.password == "alias_pass"