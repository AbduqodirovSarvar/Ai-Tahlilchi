"""Tests for app.tls and its wiring: trust is configurable and verification is never disabled."""

import http.server
import ssl
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import aiohttp
import httpx
import pytest
import trustme
import truststore
from aiogram.client.session.aiohttp import AiohttpSession
from google import genai

from app.clients.gemini import _http_options
from app.clients.http import ProviderConfig, create_http_client
from app.config import Settings, get_settings
from app.errors import ConfigurationError
from app.main import TrustedAiohttpSession, build_bot_session
from app.sports.history.download import create_client
from app.tls import build_ssl_context, get_ssl_context

SRC = Path(__file__).resolve().parents[1] / "src"
# Substrings that switch certificate verification off in the HTTP stacks the project uses.
FORBIDDEN = (
    "verify=False",
    "ssl=False",
    "CERT_NONE",
    "check_hostname = False",
    "_create_unverified",
)


def make_settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def ca_file(ca: trustme.CA, tmp_path: Path) -> Path:
    path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(path))
    return path


@pytest.fixture
def https_server(ca: trustme.CA) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002, ARG002
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(context)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def configured(ca_file: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TLS_CA_BUNDLE", str(ca_file))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestBuildSslContext:
    def test_defaults_to_library_behaviour(self) -> None:
        assert build_ssl_context(make_settings()) is None

    def test_missing_bundle_is_a_configuration_error(self, tmp_path: Path) -> None:
        settings = make_settings(tls_ca_bundle=tmp_path / "missing.pem")
        with pytest.raises(ConfigurationError, match="missing file"):
            build_ssl_context(settings)

    def test_garbage_bundle_points_to_the_system_trust_store(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pem"
        bad.write_text("-----BEGIN CERTIFICATE-----\nnot a cert\n-----END CERTIFICATE-----\n")
        with pytest.raises(ConfigurationError, match="TLS_USE_SYSTEM_TRUST_STORE") as caught:
            build_ssl_context(make_settings(tls_ca_bundle=bad))
        assert "not a cert" not in str(caught.value)

    def test_valid_bundle_gives_a_verifying_context(self, ca_file: Path) -> None:
        context = build_ssl_context(make_settings(tls_ca_bundle=ca_file))
        assert context is not None
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname
        # Strict RFC 5280 checks are off (corporate CAs fail them); verification itself is on.
        assert not context.verify_flags & ssl.VERIFY_X509_STRICT

    def test_system_trust_store_uses_truststore(self) -> None:
        context = build_ssl_context(make_settings(tls_use_system_trust_store=True))
        assert isinstance(context, truststore.SSLContext)
        assert context.verify_mode is ssl.CERT_REQUIRED

    def test_system_trust_store_wins_over_a_bundle(self, tmp_path: Path) -> None:
        settings = make_settings(
            tls_use_system_trust_store=True, tls_ca_bundle=tmp_path / "missing.pem"
        )
        assert build_ssl_context(settings) is not None

    @pytest.mark.usefixtures("configured")
    def test_env_variables_are_read(self) -> None:
        assert get_ssl_context() is not None


class TestWiring:
    def test_bot_session_is_default_when_unconfigured(self) -> None:
        assert build_bot_session() is None

    @pytest.mark.usefixtures("configured")
    def test_bot_session_gets_the_context(self) -> None:
        session = build_bot_session()
        assert isinstance(session, TrustedAiohttpSession)
        assert session._connector_init["ssl"] is get_ssl_context()

    async def test_provider_client_uses_default_trust_when_unconfigured(self) -> None:
        client = create_http_client(_provider(), "k")
        await client.aclose()

    @pytest.mark.usefixtures("configured")
    async def test_provider_client_receives_the_context(self, https_server: str) -> None:
        client = create_http_client(_provider(https_server), "k")
        try:
            assert (await client.get("")).text == "ok"
        finally:
            await client.aclose()

    @pytest.mark.usefixtures("configured")
    async def test_football_data_client_receives_the_context(self, https_server: str) -> None:
        client = create_client()
        try:
            assert (await client.get(https_server)).text == "ok"
        finally:
            await client.aclose()

    @pytest.mark.usefixtures("configured")
    def test_gemini_options_carry_the_context(self) -> None:
        options = _http_options()
        assert options is not None
        assert options.client_args == {"verify": get_ssl_context()}
        assert options.async_client_args is not None
        assert options.async_client_args["ssl"] is get_ssl_context()

    def test_gemini_options_absent_by_default(self) -> None:
        assert _http_options() is None

    @pytest.mark.usefixtures("configured")
    def test_gemini_sdk_accepts_the_options(self) -> None:
        genai.Client(api_key="k", http_options=_http_options())


def _provider(base_url: str = "https://example.invalid") -> ProviderConfig:
    return ProviderConfig(name="t", base_url=base_url, auth_header="x-key", requests_per_minute=60)


class TestLoopback:
    """A real TLS handshake against a local server signed by a throwaway CA."""

    async def test_httpx_trusts_only_the_configured_ca(
        self, https_server: str, ca_file: Path
    ) -> None:
        context = build_ssl_context(make_settings(tls_ca_bundle=ca_file))
        async with httpx.AsyncClient(verify=context or True) as trusted:
            assert (await trusted.get(https_server)).text == "ok"
        async with httpx.AsyncClient() as default:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await default.get(https_server)

    async def test_aiogram_session_trusts_the_configured_ca(
        self, https_server: str, ca_file: Path
    ) -> None:
        context = build_ssl_context(make_settings(tls_ca_bundle=ca_file))
        assert context is not None
        trusted = TrustedAiohttpSession(context)
        default = AiohttpSession()
        try:
            async with (await trusted.create_session()).get(https_server) as response:
                assert await response.text() == "ok"
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await (await default.create_session()).get(https_server)
        finally:
            await trusted.close()
            await default.close()


@pytest.mark.skipif(sys.platform != "win32", reason="OS trust store is exercised on Windows")
async def test_system_trust_store_rejects_an_unknown_ca(https_server: str) -> None:
    context = build_ssl_context(make_settings(tls_use_system_trust_store=True))
    async with httpx.AsyncClient(verify=context or True) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(https_server)


def test_sources_never_disable_verification() -> None:
    offenders = [
        f"{path.relative_to(SRC)}: {pattern}"
        for path in SRC.rglob("*.py")
        for pattern in FORBIDDEN
        if pattern in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
