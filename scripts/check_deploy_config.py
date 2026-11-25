"""Exposure rules for the server configuration, checked on the merged compose file and Caddy's JSON.

1. Only Caddy publishes ports on a public interface; every other published port is bound to 127.0.0.1.
2. Caddy never proxies a whole host: every reverse_proxy route sits under a path matcher, so a
   public n8n webhook can only be exposed path by path, never the editor or its sign-in page.

Usage: check_deploy_config.py <compose config JSON> <Caddy JSON (caddy adapt)>
"""

import json
import sys
from typing import Any

PUBLIC_SERVICES = {"caddy"}


def public_port_errors(compose: dict[str, Any]) -> list[str]:
    """Published ports of non-proxy services that aren't bound to the loopback interface."""
    errors = []
    for name, service in compose.get("services", {}).items():
        if name in PUBLIC_SERVICES:
            continue
        for port in service.get("ports", []):
            if port.get("host_ip") != "127.0.0.1":
                errors.append(f"{name}: port {port.get('published')} is published beyond 127.0.0.1")
    return errors


def unscoped_proxy_errors(routes: list[dict[str, Any]], scoped: bool = False) -> list[str]:
    """reverse_proxy handlers reached without any path matcher on the way down."""
    errors = []
    for route in routes:
        route_scoped = scoped or any("path" in m for m in route.get("match", []))
        for handler in route.get("handle", []):
            if handler.get("handler") == "reverse_proxy" and not route_scoped:
                upstreams = [u.get("dial") for u in handler.get("upstreams", [])]
                errors.append(f"reverse_proxy to {upstreams} without a path matcher")
            errors += unscoped_proxy_errors(handler.get("routes", []), route_scoped)
    return errors


def caddy_errors(caddy: dict[str, Any]) -> list[str]:
    servers = caddy.get("apps", {}).get("http", {}).get("servers", {})
    return [e for server in servers.values() for e in unscoped_proxy_errors(server.get("routes", []))]


def main() -> int:
    with open(sys.argv[1]) as f:
        compose = json.load(f)
    with open(sys.argv[2]) as f:
        caddy = json.load(f)
    errors = public_port_errors(compose) + caddy_errors(caddy)
    for error in errors:
        print(f"exposure rule broken: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
