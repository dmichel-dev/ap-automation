#!/bin/sh
# Checks that the server configuration is valid: docker-compose.yml + docker-compose.prod.yml
# merge without error, and Caddy accepts deploy/Caddyfile. Uses placeholder values, never .env.
# Run by the pre-commit hook and in CI.
set -eu
cd "$(dirname "$0")/.."

ENV_FILE=$(mktemp)
trap 'rm -f "$ENV_FILE"' EXIT
# Every key of .env.example, plus the server-only keys the override requires
grep -E '^[A-Z0-9_]+=' .env.example > "$ENV_FILE"
printf 'N8N_PUBLIC_HOST=n8n.example.com\nACME_EMAIL=ops@example.com\n' >> "$ENV_FILE"

docker compose --env-file "$ENV_FILE" -f docker-compose.yml -f docker-compose.prod.yml config --quiet

CADDY_IMAGE=$(grep -oE 'caddy:[0-9.]+-alpine' docker-compose.prod.yml | head -1)
docker run --rm --network none \
  -e N8N_PUBLIC_HOST=n8n.example.com -e ACME_EMAIL=ops@example.com \
  -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  "$CADDY_IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

echo "deploy config OK"
