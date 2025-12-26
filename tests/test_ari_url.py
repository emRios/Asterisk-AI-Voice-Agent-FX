import pytest

from src.core.ari_url import build_ari_base_url


class TestBuildAriBaseUrl:
    @pytest.mark.parametrize(
        "explicit,expected",
        [
            ("http://127.0.0.1:8088", "http://127.0.0.1:8088/ari"),
            ("http://127.0.0.1:8088/", "http://127.0.0.1:8088/ari"),
            ("http://127.0.0.1:8088/ari", "http://127.0.0.1:8088/ari"),
            ("http://127.0.0.1:8088/ari/", "http://127.0.0.1:8088/ari"),
            ("https://pbx.example.com:8089/base", "https://pbx.example.com:8089/base/ari"),
        ],
    )
    def test_explicit_ari_base_url_normalization(self, explicit, expected):
        """
        Cuando ari_base_url es explícito:
        - Se hace strip y rstrip("/") de la url
        - Se garantiza el sufijo "/ari" exactamente una vez
        """
        out = build_ari_base_url(
            ari_base_url=explicit,
            scheme=None,
            host="ignored",
            port=0,
        )
        assert out == expected

    def test_construct_from_parts_default_http(self):
        """
        Sin ari_base_url explícita:
        - scheme por defecto "http"
        - se construye {scheme}://{host}:{port}/ari
        """
        out = build_ari_base_url(
            ari_base_url=None,
            scheme=None,
            host="127.0.0.1",
            port=8088,
        )
        assert out == "http://127.0.0.1:8088/ari"

    def test_construct_from_parts_https(self):
        """
        Sin ari_base_url explícita:
        - si scheme='https', debe producir https://.../ari
        """
        out = build_ari_base_url(
            ari_base_url=None,
            scheme="https",
            host="pbx.example.com",
            port=8089,
        )
        assert out == "https://pbx.example.com:8089/ari"

    @pytest.mark.parametrize(
        "scheme,expected_scheme",
        [
            ("HTTP", "http"),
            (" HtTpS ", "https"),
            (" http  ", "http"),
        ],
    )
    def test_scheme_whitespace_and_case_normalization(self, scheme, expected_scheme):
        """
        Sin ari_base_url explícita:
        - scheme se normaliza con strip y lower()
        """
        out = build_ari_base_url(
            ari_base_url=None,
            scheme=scheme,
            host="host",
            port=1234,
        )
        assert out == f"{expected_scheme}://host:1234/ari"