"""Optional trust configuration for every outbound HTTPS connection.

Off by default: without ``TLS_CA_BUNDLE`` and ``TLS_USE_SYSTEM_TRUST_STORE`` the libraries keep
their own defaults (the ``certifi`` bundle). Behind a TLS-intercepting corporate proxy the proxy
re-signs traffic with a corporate CA that ``certifi`` does not know, so either the OS trust
store (which already trusts that CA) or an exported PEM bundle is configured instead.

Certificate verification is never disabled here or anywhere else (a test greps the sources).
"""

import ssl
from functools import lru_cache
from pathlib import Path

import truststore

from app.config import Settings, get_settings
from app.errors import ConfigurationError
from app.logging import get_logger

logger = get_logger(__name__)


def build_ssl_context(settings: Settings) -> ssl.SSLContext | None:
    """The SSL context the settings ask for, or ``None`` for the libraries' defaults.

    The system trust store wins over a bundle when both are set: it is a superset in practice
    and it is the option that survives Python's strict CA checks.

    Raises:
        ConfigurationError: the bundle is missing, unreadable or not accepted by OpenSSL.
    """
    if settings.tls_use_system_trust_store:
        logger.info("tls_trust", source="system_trust_store")
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    bundle = settings.tls_ca_bundle
    if bundle is None:
        return None
    context = _load_bundle(bundle)
    logger.info("tls_trust", source="ca_bundle")
    return context


def _load_bundle(bundle: Path) -> ssl.SSLContext:
    # The path is logged, the file content never is.
    if not bundle.is_file():
        msg = f"TLS_CA_BUNDLE points to a missing file: {bundle}"
        raise ConfigurationError(msg)
    try:
        context = ssl.create_default_context(cafile=str(bundle))
    except (ssl.SSLError, OSError) as error:
        msg = (
            f"TLS_CA_BUNDLE {bundle} could not be loaded ({type(error).__name__}); it must be a "
            "PEM file with CA certificates. If the file is valid but still fails, set "
            "TLS_USE_SYSTEM_TRUST_STORE=true to use the OS trust store instead"
        )
        raise ConfigurationError(msg) from None
    # Python 3.13 turned on OpenSSL's strict RFC 5280 checks, which reject many corporate CAs
    # (e.g. "Basic Constraints of CA cert not marked critical"). The chain, expiry and host name
    # are still verified; only that extra strictness (Python <= 3.12 behaviour) is dropped.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


@lru_cache(maxsize=1)
def get_ssl_context() -> ssl.SSLContext | None:
    """The process-wide context built from :func:`~app.config.get_settings` (cached).

    Raises:
        ConfigurationError: see :func:`build_ssl_context`.
    """
    return build_ssl_context(get_settings())
