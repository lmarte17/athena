#!/usr/bin/env bash
# Deploy Athena backend to Cloud Run using Vertex AI.
#
# Prerequisites (one-time setup):
#   1. gcloud CLI installed and authenticated:
#        gcloud auth login
#        gcloud auth application-default login
#
#   2. Required APIs enabled on your project:
#        gcloud services enable \
#          run.googleapis.com \
#          cloudbuild.googleapis.com \
#          artifactregistry.googleapis.com \
#          aiplatform.googleapis.com \
#          secretmanager.googleapis.com \
#          --project "$GOOGLE_CLOUD_PROJECT"
#
#   3. The Cloud Run service account needs the "Vertex AI User" role:
#        gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
#          --member="serviceAccount:$(gcloud projects describe $GOOGLE_CLOUD_PROJECT \
#            --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
#          --role="roles/aiplatform.user"
#
#   4. gog (gogcli) must be installed locally (used to export your OAuth token):
#        brew install steipete/tap/gogcli
#        gog auth add you@gmail.com
#
#   5. Optional: slides-agent auth for advanced Slides editing in Cloud Run:
#        python3.12 -m pip install slides-agent
#        slides-agent auth login --credentials-file /path/to/client_secret.json
#
# Usage:
#   export GOOGLE_CLOUD_PROJECT=your-project-id
#   export GOG_ACCOUNT=you@gmail.com
#   export GOOGLE_CLOUD_LOCATION=us-central1   # optional, defaults to us-central1
#   cd backend/adk-bidi
#   bash deploy.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
GOG_ACCOUNT="${GOG_ACCOUNT:-}"
SERVICE="athena-backend"
AR_REPO="athena"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${SERVICE}"

SECRET_CREDENTIALS="gog-credentials"
SECRET_TOKENS="gog-tokens"
SECRET_SLIDES_AGENT_CREDENTIALS="slides-agent-credentials"
SECRET_SLIDES_AGENT_TOKEN="slides-agent-token"
SECRET_API_KEY="athena-api-key"
SECRET_LANGSMITH_API_KEY="athena-langsmith-api-key"
SECRET_NBCLI_TOKEN="athena-nbcli-token"
NBCLI_URL="${NBCLI_URL:-}"
# Password used to encrypt the gog token file on disk (file-based keyring).
# Override via env var if desired.
GOG_KEYRING_PASSWORD="${GOG_KEYRING_PASSWORD:-athena-cloudrun-keyring}"
# ──────────────────────────────────────────────────────────────────────────────

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: GOOGLE_CLOUD_PROJECT is not set."
  echo "  export GOOGLE_CLOUD_PROJECT=your-project-id"
  exit 1
fi

if [[ -z "$GOG_ACCOUNT" ]]; then
  echo "ERROR: GOG_ACCOUNT is not set (your Google Workspace email)."
  echo "  export GOG_ACCOUNT=you@gmail.com"
  exit 1
fi

# Change to the directory containing this script so the build context is correct.
cd "$(dirname "$0")"

echo "Project:     ${PROJECT}"
echo "Region:      ${REGION}"
echo "Image:       ${IMAGE}"
echo "gog account: ${GOG_ACCOUNT}"
echo ""

# ── Enable required APIs ──────────────────────────────────────────────────────
echo "→ Enabling required APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT}" --quiet

# ── Artifact Registry repo ────────────────────────────────────────────────────
echo ""
echo "→ Ensuring Artifact Registry repository exists..."
gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT}" 2>/dev/null \
  && echo "  Created repo '${AR_REPO}'" \
  || echo "  Repo '${AR_REPO}' already exists, skipping."

# ── gog secrets ───────────────────────────────────────────────────────────────
# We store two secrets in Secret Manager:
#   gog-credentials  — OAuth client_id/secret (credentials.json)
#   gog-tokens       — Your refresh token exported from local gog keyring
#
# The container's start.sh imports these at boot so gog CLI can auth
# without a system keyring daemon.

COMPUTE_SA="$(gcloud projects describe "${PROJECT}" \
  --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

_ensure_secret() {
  local name="$1"
  local local_path="$2"
  local description="$3"

  if gcloud secrets describe "${name}" --project="${PROJECT}" &>/dev/null; then
    echo "  Secret '${name}' already exists — skipping creation."
    echo "  (To rotate: gcloud secrets versions add ${name} --data-file=<file> --project=${PROJECT})"
  else
    if [[ ! -f "${local_path}" ]]; then
      echo ""
      echo "ERROR: Secret '${name}' not found in Secret Manager, and local file missing:"
      echo "  ${local_path}"
      echo ""
      echo "  ${description}"
      exit 1
    fi
    echo "  Creating secret '${name}' from ${local_path}..."
    gcloud secrets create "${name}" \
      --data-file="${local_path}" \
      --project="${PROJECT}"
  fi

  # Grant the Cloud Run default compute SA read access.
  gcloud secrets add-iam-policy-binding "${name}" \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${PROJECT}" \
    --condition=None \
    2>/dev/null || true
}

_maybe_seed_secret() {
  local name="$1"
  local local_path="$2"
  local description="$3"

  if gcloud secrets describe "${name}" --project="${PROJECT}" &>/dev/null; then
    echo "  Secret '${name}' already exists — skipping creation."
    gcloud secrets add-iam-policy-binding "${name}" \
      --member="serviceAccount:${COMPUTE_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --project="${PROJECT}" \
      --condition=None \
      2>/dev/null || true
    return 0
  fi

  if [[ -f "${local_path}" ]]; then
    echo "  Creating secret '${name}' from ${local_path}..."
    gcloud secrets create "${name}" \
      --data-file="${local_path}" \
      --project="${PROJECT}"
    gcloud secrets add-iam-policy-binding "${name}" \
      --member="serviceAccount:${COMPUTE_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --project="${PROJECT}" \
      --condition=None \
      2>/dev/null || true
    return 0
  fi

  echo "  WARNING: Secret '${name}' not found and local file missing: ${local_path}"
  if [[ -n "${description}" ]]; then
    echo "           ${description}"
  fi
  return 1
}

echo ""
echo "→ Setting up gog secrets in Secret Manager..."

# credentials.json is at a known macOS path; adjust GOG_CREDENTIALS_LOCAL if needed.
GOG_CREDENTIALS_LOCAL="${HOME}/Library/Application Support/gogcli/credentials.json"
_ensure_secret "${SECRET_CREDENTIALS}" "${GOG_CREDENTIALS_LOCAL}" \
  "This is your gogcli OAuth client credentials file. It should exist if you ran 'gog auth add'."

# Export your refresh token to a temp file and upload it.
if gcloud secrets describe "${SECRET_TOKENS}" --project="${PROJECT}" &>/dev/null; then
  echo "  Secret '${SECRET_TOKENS}' already exists — skipping export."
  echo "  (To rotate: gog auth tokens export ${GOG_ACCOUNT} --out=/tmp/gog-tokens.json --overwrite && gcloud secrets versions add ${SECRET_TOKENS} --data-file=/tmp/gog-tokens.json --project=${PROJECT})"
else
  echo "  Exporting gog refresh token for ${GOG_ACCOUNT}..."
  GOG_TOKENS_TMP="$(mktemp /tmp/gog-tokens.XXXXXX.json)"
  trap 'rm -f "${GOG_TOKENS_TMP}"' EXIT

  if ! gog auth tokens export "${GOG_ACCOUNT}" --out="${GOG_TOKENS_TMP}" --overwrite 2>&1; then
    echo "ERROR: Failed to export gog tokens."
    echo "  Make sure you have run: gog auth add ${GOG_ACCOUNT}"
    exit 1
  fi

  if [[ ! -s "${GOG_TOKENS_TMP}" ]]; then
    echo "ERROR: gog auth tokens export produced an empty file."
    exit 1
  fi

  _ensure_secret "${SECRET_TOKENS}" "${GOG_TOKENS_TMP}" ""
fi

echo ""
echo "→ Setting up slides-agent secrets in Secret Manager (optional)..."

SLIDES_AGENT_CREDENTIALS_LOCAL="${SLIDES_AGENT_CREDENTIALS_LOCAL:-${GOG_CREDENTIALS_LOCAL}}"
SLIDES_AGENT_TOKEN_LOCAL="${SLIDES_AGENT_TOKEN_LOCAL:-${HOME}/.config/slides-agent/token.json}"

SLIDES_AGENT_READY=false
slides_agent_credentials_ready=false
slides_agent_token_ready=false

if _maybe_seed_secret \
  "${SECRET_SLIDES_AGENT_CREDENTIALS}" \
  "${SLIDES_AGENT_CREDENTIALS_LOCAL}" \
  "Set SLIDES_AGENT_CREDENTIALS_LOCAL if your slides-agent client_secret.json is elsewhere."; then
  slides_agent_credentials_ready=true
fi

if _maybe_seed_secret \
  "${SECRET_SLIDES_AGENT_TOKEN}" \
  "${SLIDES_AGENT_TOKEN_LOCAL}" \
  "Run 'slides-agent auth login --credentials-file <client_secret.json>' locally first, or set SLIDES_AGENT_TOKEN_LOCAL."; then
  slides_agent_token_ready=true
fi

if [[ "${slides_agent_credentials_ready}" == true && "${slides_agent_token_ready}" == true ]]; then
  SLIDES_AGENT_READY=true
  echo "  slides-agent auth secrets are ready."
else
  echo "  slides-agent auth secrets are incomplete — advanced Slides edit/inspect commands will be unavailable in Cloud Run."
fi

# Ensure the ATHENA API KEY secret exists (you must create this secret manually in Secret Manager)
# Example: echo -n "AIz..." | gcloud secrets create athena-api-key --data-file=-
if ! gcloud secrets describe "${SECRET_API_KEY}" --project="${PROJECT}" &>/dev/null; then
  echo ""
  echo "ERROR: Secret '${SECRET_API_KEY}' not found in Secret Manager."
  echo "  Please create it first so the Live Voice agent can authenticate to AI Studio:"
  echo "  echo -n \"your_api_key\" | gcloud secrets create ${SECRET_API_KEY} --data-file=- --project=${PROJECT}"
  exit 1
fi
gcloud secrets add-iam-policy-binding "${SECRET_API_KEY}" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project="${PROJECT}" \
  --condition=None \
  2>/dev/null || true

if ! gcloud secrets describe "${SECRET_LANGSMITH_API_KEY}" --project="${PROJECT}" &>/dev/null; then
  echo ""
  echo "ERROR: Secret '${SECRET_LANGSMITH_API_KEY}' not found in Secret Manager."
  echo "  Please create it first so Athena can send traces to LangSmith:"
  echo "  echo -n \"lsv2_...\" | gcloud secrets create ${SECRET_LANGSMITH_API_KEY} --data-file=- --project=${PROJECT}"
  exit 1
fi
gcloud secrets add-iam-policy-binding "${SECRET_LANGSMITH_API_KEY}" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project="${PROJECT}" \
  --condition=None \
  2>/dev/null || true

# ── NetBox (optional) ─────────────────────────────────────────────────────────
NBCLI_READY=false
if [[ -n "${NBCLI_URL}" ]]; then
  if gcloud secrets describe "${SECRET_NBCLI_TOKEN}" --project="${PROJECT}" &>/dev/null; then
    echo ""
    echo "→ NetBox secret '${SECRET_NBCLI_TOKEN}' found — netbox_specialist will be enabled."
    gcloud secrets add-iam-policy-binding "${SECRET_NBCLI_TOKEN}" \
      --member="serviceAccount:${COMPUTE_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --project="${PROJECT}" \
      --condition=None \
      2>/dev/null || true
    NBCLI_READY=true
  else
    echo ""
    echo "  WARNING: NBCLI_URL is set but secret '${SECRET_NBCLI_TOKEN}' not found in Secret Manager."
    echo "  To enable the netbox_specialist on Cloud Run:"
    echo "    echo -n \"your_netbox_token\" | gcloud secrets create ${SECRET_NBCLI_TOKEN} --data-file=- --project=${PROJECT}"
    echo "  Skipping NetBox — other specialists will still work."
  fi
fi

# ── Build ─────────────────────────────────────────────────────────────────────
echo ""
echo "→ Building image with Cloud Build..."
gcloud builds submit . \
  --tag "${IMAGE}" \
  --project "${PROJECT}"

# ── Deploy ────────────────────────────────────────────────────────────────────
echo ""
echo "→ Deploying to Cloud Run..."

ENV_VARS="GOOGLE_GENAI_USE_VERTEXAI=TRUE,LIVE_VOICE_FORCE_AI_STUDIO=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},ATHENA_MODEL=gemini-2.5-flash-native-audio-preview-12-2025,GOG_ACCOUNT=${GOG_ACCOUNT},GOG_KEYRING_BACKEND=file,GOG_KEYRING_PASSWORD=${GOG_KEYRING_PASSWORD},LANGSMITH_PROJECT=athena-cloudrun,ATHENA_TRACE_ENV=cloudrun,ATHENA_TRACE_CAPTURE_THOUGHTS=TRUE,ATHENA_SLIDES_AGENT_BINARY=/usr/local/bin/slides-agent"

if [[ -n "${NBCLI_URL}" ]]; then
  ENV_VARS="${ENV_VARS},NBCLI_URL=${NBCLI_URL}"
fi

SECRET_BINDINGS="GOG_CREDENTIALS_JSON=${SECRET_CREDENTIALS}:latest,GOG_TOKENS_JSON=${SECRET_TOKENS}:latest,GOOGLE_API_KEY=${SECRET_API_KEY}:latest,LANGSMITH_API_KEY=${SECRET_LANGSMITH_API_KEY}:latest"

if [[ "${SLIDES_AGENT_READY}" == true ]]; then
  SECRET_BINDINGS="${SECRET_BINDINGS},SLIDES_AGENT_CREDENTIALS_JSON=${SECRET_SLIDES_AGENT_CREDENTIALS}:latest,SLIDES_AGENT_TOKEN_JSON=${SECRET_SLIDES_AGENT_TOKEN}:latest"
fi

if [[ "${NBCLI_READY}" == true ]]; then
  SECRET_BINDINGS="${SECRET_BINDINGS},NBCLI_TOKEN=${SECRET_NBCLI_TOKEN}:latest"
fi

gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --project "${PROJECT}" \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars "${ENV_VARS}" \
  --set-secrets "${SECRET_BINDINGS}" \
  --timeout=3600 \
  --session-affinity \
  --min-instances=0 \
  --max-instances=5 \
  --memory=512Mi \
  --cpu=1

# ── Print endpoint ────────────────────────────────────────────────────────────
SERVICE_URL=$(gcloud run services describe "${SERVICE}" \
  --region="${REGION}" \
  --project="${PROJECT}" \
  --format="value(status.url)")

WS_URL="${SERVICE_URL/https:\/\//wss://}/ws"

echo ""
echo "✓ Deployed successfully."
echo "  Service URL:        ${SERVICE_URL}"
echo "  WebSocket endpoint: ${WS_URL}"
echo ""
echo "To point the Tray app at this backend, launch it with:"
echo "  ATHENA_WS_URL=${WS_URL} cargo tauri dev"
echo ""
echo "Or export it in your shell before running:"
echo "  export ATHENA_WS_URL=${WS_URL}"
echo ""
echo "To rotate gog tokens (e.g. after re-auth):"
echo "  gog auth tokens export ${GOG_ACCOUNT} --out=/tmp/gog-tokens.json --overwrite && \\"
echo "    gcloud secrets versions add ${SECRET_TOKENS} --data-file=/tmp/gog-tokens.json --project=${PROJECT} && \\"
echo "    rm /tmp/gog-tokens.json"
