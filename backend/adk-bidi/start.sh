#!/usr/bin/env bash
# Startup script for Cloud Run.
# Initializes gog (gogcli) authentication from Secret Manager env vars, then starts uvicorn.
set -euo pipefail

GOG_CONFIG_DIR="${HOME}/.config/gogcli"
SLIDES_AGENT_CONFIG_DIR="${HOME}/.config/slides-agent"

# ── gog auth setup ─────────────────────────────────────────────────────────────
#
# Cloud Run injects the two secrets as environment variables:
#   GOG_CREDENTIALS_JSON  — contents of credentials.json (OAuth client_id/secret)
#   GOG_TOKENS_JSON       — exported refresh token (from: gog auth tokens export)
#
# GOG_KEYRING_BACKEND=file + GOG_KEYRING_PASSWORD stores the token in an
# encrypted local file instead of a system keyring daemon (which isn't
# available in a container).
#
if [[ -n "${GOG_CREDENTIALS_JSON:-}" ]] && [[ -n "${GOG_TOKENS_JSON:-}" ]]; then
    mkdir -p "${GOG_CONFIG_DIR}"
    printf '%s' "${GOG_CREDENTIALS_JSON}" > "${GOG_CONFIG_DIR}/credentials.json"
    echo "[start] gog credentials written to ${GOG_CONFIG_DIR}/credentials.json"

    GOG_TOKENS_TMP="$(mktemp /tmp/gog-tokens.XXXXXX.json)"
    printf '%s' "${GOG_TOKENS_JSON}" > "${GOG_TOKENS_TMP}"

    if /usr/local/bin/gog auth tokens import "${GOG_TOKENS_TMP}" 2>&1; then
        echo "[start] gog tokens imported successfully"
    else
        echo "[start] WARNING: gog tokens import failed — workspace features may not work"
    fi

    rm -f "${GOG_TOKENS_TMP}"
else
    echo "[start] GOG_CREDENTIALS_JSON / GOG_TOKENS_JSON not set — workspace features (Gmail/Calendar/Drive) will be unavailable"
fi

# ── slides-agent auth setup ───────────────────────────────────────────────────
#
# Optional Cloud Run secrets:
#   SLIDES_AGENT_CREDENTIALS_JSON  — contents of client_secret.json
#   SLIDES_AGENT_TOKEN_JSON        — contents of slides-agent token.json
#
# Athena's slides-agent wrapper expects file paths via:
#   SLIDES_AGENT_CREDENTIALS
#   SLIDES_AGENT_TOKEN_FILE
#
if [[ -n "${SLIDES_AGENT_CREDENTIALS_JSON:-}" ]] && [[ -n "${SLIDES_AGENT_TOKEN_JSON:-}" ]]; then
    mkdir -p "${SLIDES_AGENT_CONFIG_DIR}"

    SLIDES_AGENT_CREDENTIALS_PATH="${SLIDES_AGENT_CREDENTIALS_PATH:-${SLIDES_AGENT_CONFIG_DIR}/client_secret.json}"
    SLIDES_AGENT_TOKEN_PATH="${SLIDES_AGENT_TOKEN_PATH:-${SLIDES_AGENT_CONFIG_DIR}/token.json}"

    printf '%s' "${SLIDES_AGENT_CREDENTIALS_JSON}" > "${SLIDES_AGENT_CREDENTIALS_PATH}"
    printf '%s' "${SLIDES_AGENT_TOKEN_JSON}" > "${SLIDES_AGENT_TOKEN_PATH}"
    chmod 600 "${SLIDES_AGENT_CREDENTIALS_PATH}" "${SLIDES_AGENT_TOKEN_PATH}"

    export SLIDES_AGENT_CREDENTIALS="${SLIDES_AGENT_CREDENTIALS_PATH}"
    export SLIDES_AGENT_TOKEN_FILE="${SLIDES_AGENT_TOKEN_PATH}"

    echo "[start] slides-agent credentials written to ${SLIDES_AGENT_CREDENTIALS_PATH}"
    echo "[start] slides-agent token written to ${SLIDES_AGENT_TOKEN_PATH}"
elif [[ -n "${SLIDES_AGENT_CREDENTIALS_JSON:-}" ]] || [[ -n "${SLIDES_AGENT_TOKEN_JSON:-}" ]]; then
    echo "[start] WARNING: partial slides-agent auth env detected — advanced Slides operations may not work"
else
    echo "[start] SLIDES_AGENT_CREDENTIALS_JSON / SLIDES_AGENT_TOKEN_JSON not set — advanced Slides edit/inspect features will be unavailable"
fi

if command -v slides-agent >/dev/null 2>&1; then
    export ATHENA_SLIDES_AGENT_BINARY="${ATHENA_SLIDES_AGENT_BINARY:-$(command -v slides-agent)}"
fi

# ── launch server ──────────────────────────────────────────────────────────────
echo "[start] starting uvicorn on port ${PORT:-8080}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
