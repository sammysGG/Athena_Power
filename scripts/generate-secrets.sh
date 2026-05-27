#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${STACK_ROOT}/.env"

if [[ -f "${ENV_FILE}" ]]; then
    echo "Refusing to overwrite existing ${ENV_FILE}" >&2
    exit 1
fi

random_hex() {
    openssl rand -hex 32
}

random_password() {
    openssl rand -base64 24 | tr -d '\n'
}

cat > "${ENV_FILE}" <<EOF
SCADA_STACK_NAME=scada
SCADA_DOMAIN=scada.goathost.gg
SCADA_SITE_NAME=GoatHost Range SCADA
SCADA_HMI_USERNAME=admin
SCADA_HMI_PASSWORD=Cool2Pass
SCADA_BRIDGE_API_KEY=$(random_hex)
SCADA_SIMULATOR_API_KEY=$(random_hex)
CLOUDFLARE_TUNNEL_TOKEN=REPLACE_WITH_CLOUDFLARE_TUNNEL_TOKEN
SCADA_POLL_SECONDS=1.0
SCADA_HISTORY_LIMIT=900
SCADA_MODBUS_PORT=15020
SCADA_HMI_BIND_ADDRESS=127.0.0.1
SCADA_HMI_PUBLISHED_PORT=18080
EOF

chmod 600 "${ENV_FILE}"
echo "Created ${ENV_FILE}"
echo "Set CLOUDFLARE_TUNNEL_TOKEN before deploying."
