"""Configuration of one review-page listener, read from the environment when the process starts.

A listener is either:
  internal  reached only through an SSH tunnel to 127.0.0.1; every role may sign in
  public    behind the reverse proxy (HTTPS); only demo accounts may sign in

The mode is fixed when the process starts, never taken from a request. The default is "public", so
a missing setting can only make the listener stricter. Settings that contradict the mode stop the
process at startup.
"""

from functools import cached_property
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings

Listener = Literal["internal", "public"]
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _origin_parts(origin: str) -> tuple[str, str]:
    """(scheme, host[:port]) of an origin such as https://review.example.com, or ValueError."""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise ValueError(f"not an origin (scheme://host[:port]): {origin!r}")
    return parts.scheme, parts.netloc


def _is_loopback(netloc: str) -> bool:
    host = netloc.rsplit(":", 1)[0] if not netloc.endswith("]") else netloc
    return host in LOOPBACK_HOSTS


class WebSettings(BaseSettings):
    ap_database_url: SecretStr
    file_storage_dir: str = "/data/files"
    review_listener: Listener = "public"
    review_origins: str  # comma-separated origins the page is served from, e.g. https://review.example.com
    review_trusted_proxies: str = ""  # public only: proxy addresses whose X-Forwarded-For is used
    # Confidence badge (display only). CONFIDENCE_THRESHOLD is the core's review threshold, so red always
    # means "below the threshold the core routes on"; green starts at REVIEW_CONFIDENCE_HIGH.
    confidence_threshold: float = 0.85
    review_confidence_high: float = 0.95

    @cached_property
    def origins(self) -> tuple[str, ...]:
        return tuple(o.strip().rstrip("/") for o in self.review_origins.split(",") if o.strip())

    @cached_property
    def hosts(self) -> frozenset[str]:
        """Host header values accepted (the host[:port] of each origin)."""
        return frozenset(_origin_parts(o)[1] for o in self.origins)

    @cached_property
    def trusted_proxies(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.review_trusted_proxies.split(",") if p.strip())

    @model_validator(mode="after")
    def _consistent(self) -> "WebSettings":
        if not self.origins:
            raise ValueError("REVIEW_ORIGINS is empty")
        parts = [_origin_parts(o) for o in self.origins]
        if self.review_listener == "internal":
            if not all(_is_loopback(netloc) for _, netloc in parts):
                raise ValueError("the internal listener serves loopback origins only (localhost, 127.0.0.1)")
            if self.trusted_proxies:
                raise ValueError(
                    "the internal listener never sits behind a proxy: REVIEW_TRUSTED_PROXIES must be empty"
                )
        elif not all(scheme == "https" for scheme, _ in parts):
            raise ValueError("the public listener serves HTTPS origins only")
        for p in self.trusted_proxies:
            ip_address(p)  # ValueError if not an IP address
        if not 0 < self.confidence_threshold <= self.review_confidence_high <= 1:
            raise ValueError("confidence bands: 0 < CONFIDENCE_THRESHOLD <= REVIEW_CONFIDENCE_HIGH <= 1")
        return self
