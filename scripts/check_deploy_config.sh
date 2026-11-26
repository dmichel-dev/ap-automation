#!/bin/sh
# Checks the server configuration with placeholder values (never .env):
# - docker-compose.yml + docker-compose.prod.yml merge without error;
# - Caddy accepts deploy/Caddyfile;
# - the exposure rules in scripts/check_deploy_config.py hold (only Caddy is public, and Caddy
#   never proxies a whole host).
# Run by the pre-commit hook and in CI.
set -eu
cd "$(dirname "$0")/.."

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# Every key of .env.example, plus the server-only keys the override requires
grep -E '^[A-Z0-9_]+=' .env.example > "$TMP/env"
printf 'RETIRED_HOSTS=old.example.com\nACME_EMAIL=ops@example.com\nDEMO_HOST=demo.example.com\n' >> "$TMP/env"

docker compose --env-file "$TMP/env" -f docker-compose.yml -f docker-compose.prod.yml \
  config --format json > "$TMP/compose.json"

CADDY_IMAGE=$(grep -oE 'caddy:[0-9.]+-alpine' docker-compose.prod.yml | head -1)
caddy() {
  docker run --rm --network none \
    -e RETIRED_HOSTS=old.example.com -e ACME_EMAIL=ops@example.com -e DEMO_HOST=demo.example.com \
    -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" \
    "$CADDY_IMAGE" caddy "$@" --config /etc/caddy/Caddyfile --adapter caddyfile
}
caddy validate 2>/dev/null >/dev/null || caddy validate
caddy adapt 2>/dev/null > "$TMP/caddy.json"

python3 scripts/check_deploy_config.py "$TMP/compose.json" "$TMP/caddy.json"
echo "deploy config OK"
