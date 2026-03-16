# Athena

**A voice coworker that lives in your Mac's menu bar — always present, never in the way.**

Most AI tools make you stop what you're doing. You open a new tab, start a fresh chat, type out your context, and wait. Then you try to pick up where you left off. That context switch is the actual productivity killer.

Athena works the other way. One click and you're talking. She listens in real time, responds through your speakers, sees what's on your screen, and — when you ask her to do something that takes longer — she goes off and does it in the background while you keep working. When it's done, she tells you.

No chat window. No typing. No stopping.

---

## Demo

> **[Demo video — link coming]**

---

## What it does

- **Real-time voice conversation** — speak naturally, hear responses through your speakers, no latency gap
- **Screen awareness** — Athena sees what you're looking at and can reference it without you explaining it
- **Background workspace jobs** — "draft a summary of my recent emails and add it to the meeting invite" runs in the background while you keep working
- **Proactive notifications** — when a job finishes, Athena speaks up when you're silent, not mid-sentence
- **Semantic memory** — your preferences, decisions, and past context are retrieved by meaning, not keyword
- **Google Workspace integration** — Gmail, Drive, Docs, Calendar, Sheets, Slides via OAuth

---

## How it works

### Live Voice Loop

The tray app captures raw PCM audio and streams it over a WebSocket to the backend. The backend runs a bidirectional ADK session with Gemini Live, which handles both speech recognition and response audio generation in a single real-time stream. The response audio streams back to the tray app and plays through the system speakers without buffering.

When Athena decides a request needs background work, she calls `submit_workspace_job` — a typed ADK tool that hands off to the job pipeline while the live audio session stays open and responsive.

### Job Dispatch Pipeline

Background jobs flow through a DAG execution engine:

1. **Planner** decomposes the user's intent into discrete steps, checking a SQLite-backed skill library first for fast-path patterns learned from prior runs
2. **Execution engine** topologically sorts the steps and runs independent ones in parallel via `asyncio.gather`
3. Each step gets a fresh `LlmAgent` + `Runner` so state never leaks between steps
4. **WorkspaceCoordinator** runs headless ADK agents for each specialist (gmail, drive, docs, calendar, sheets, slides)
5. When a step finishes, its output is injected into dependent downstream steps as enriched context

After a successful multi-step job, the planner distills what worked into the skill library so future similar requests skip the decomposition step entirely.

### Semantic Retrieval

When workspace context is needed, Athena doesn't keyword-search — it embeds. Documents are chunked on section boundaries, embedded with `gemini-embedding-001` (768-dim), and stored in a local SQLite vector store. Retrieval uses cosine similarity. Everything runs locally; no external vector service required.

### Proactive Notifications

The WebSocket handler tracks `last_user_audio_at`. When a background job finishes, the result injector checks whether the user has been silent for at least `PROACTIVE_SILENCE_SECS` (default 1.5s). If silent, it injects the result immediately. If the user is speaking, the result queues and is delivered at the next silence window.

---

## Architecture

```
macOS Tray App (Rust/Tauri v2)
    │  WebSocket — PCM audio + JSON events + JPEG screen frames
    ▼
FastAPI Backend (Python / Google ADK)
    │
    ├─ ws.py ──────────────────────────────────────────────► Gemini Live API
    │   silence monitor, upstream/downstream                  (real-time audio)
    │
    ├─ LiveVoiceAgent (ADK)
    │   ├─ submit_workspace_job
    │   └─ lookup_recent_job_result
    │           │
    │           ▼
    │   JobDispatcher
    │           │
    │           ▼
    │   Orchestrator
    │       ├─ PlannerAgent ──────────────────────────────► Gemini Pro (plan)
    │       │   └─ SkillLibrary (SQLite, fast path)
    │       │           │
    │       │           ▼
    │       │   ExecutionEngine (parallel DAG)
    │       │       └─ LlmAgent per step ────────────────► Gemini Flash
    │       │
    │       └─ WorkspaceCoordinator (headless ADK)
    │               └─ Specialist Agents
    │                   ├─ gmail      ──► gogcli / GWS API
    │                   ├─ drive      ──► gogcli / GWS API
    │                   ├─ docs       ──► gogcli / GWS API
    │                   ├─ calendar   ──► gogcli / GWS API
    │                   ├─ sheets     ──► gogcli / GWS API
    │                   └─ slides     ──► slides-agent
    │
    ├─ SemanticRetrieval
    │   ├─ Embedder (gemini-embedding-001, 768-dim)
    │   └─ VectorStore (SQLite + numpy cosine similarity)
    │
    ├─ MemoryService (~/.athena/ — YAML profiles, sessions, commitments)
    ├─ ReflectionAgent (post-session summaries)
    └─ ResultInjector (silence-gated proactive delivery)
```

**Google Cloud services used:**
- **Cloud Run** — backend deployment (containerized, auto-scaling, HTTPS + WebSocket)
- **Gemini Live API** — real-time bidirectional audio (`gemini-2.5-flash-native-audio-preview`)
- **Vertex AI** — workspace agents and planner (`gemini-3.1-pro-preview`, `gemini-flash`)
- **Gemini Embeddings API** — semantic retrieval (`gemini-embedding-001`)
- **Secret Manager** — OAuth tokens and API keys in Cloud Run
- **Artifact Registry** — Docker image storage
- **Cloud Build** — container build pipeline

---

## Stack

| Layer | Technology |
|---|---|
| Tray app | Rust + Tauri v2 (macOS menu bar, audio I/O via cpal, screen capture) |
| Backend | Python 3.12 + FastAPI + Google ADK |
| Real-time audio | Gemini Live via ADK bidirectional streaming |
| Workspace agents | Google ADK `LlmAgent` + specialist tools |
| Workspace auth | gogcli (Gmail, Drive, Docs, Calendar, Sheets, Slides) |
| Slides editing | slides-agent |
| Planner | Gemini Pro (JSON structured output) |
| Embeddings | gemini-embedding-001 (768-dim, local SQLite vector store) |
| Memory | Filesystem YAML + SQLite (`~/.athena/`) |
| Tracing | LangSmith (google-adk integration) |
| Deployment | Google Cloud Run (multi-stage Docker: Go + Python) |

---

## Prerequisites

Install these once before anything else.

### System tools

```bash
# Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Rust toolchain
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Python 3.12+
brew install python@3.12

# uv (fast Python package manager)
pip install uv

# gcloud CLI
brew install --cask google-cloud-sdk

# gogcli (Google Workspace OAuth CLI)
brew install steipete/tap/gogcli
```

### API access

You need **one** of these two options:

**Option A — Google AI Studio (local dev, free):**
1. Go to [aistudio.google.com](https://aistudio.google.com) → Get API key
2. Keep it handy for the `.env` step below

**Option B — Vertex AI (Cloud Run / production):**
1. Create a Google Cloud project
2. Run: `gcloud auth login && gcloud auth application-default login`
3. Enable APIs:
```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  --project YOUR_PROJECT_ID
```

---

## Local Development

### 1. Backend

```bash
cd backend/adk-bidi

# Install dependencies
uv sync

# Configure environment
cp app/.env.example app/.env
```

Edit `app/.env` — for local dev, Option A (AI Studio) is simplest:

```bash
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=your_api_key_here
ATHENA_MODEL=gemini-2.5-flash-native-audio-preview-12-2025
LANGSMITH_API_KEY=lsv2_your_langsmith_key_here
LANGSMITH_PROJECT=athena-local
```

Start the server:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The backend listens at `http://localhost:8000`. WebSocket endpoint: `ws://localhost:8000/ws`.

Verify it's running:
```bash
curl http://localhost:8000/health
```

### 2. Google Workspace integration (optional)

Workspace features (Gmail, Calendar, Drive) require gogcli auth:

```bash
# Authenticate with your Google account
gog auth add you@gmail.com

# Verify
gog gmail threads --query "is:unread" --limit 3
```

### 2b. Advanced Google Slides editing (optional)

Athena's existing Slides create/read flows continue to use `gogcli`, but advanced
slide inspection and direct editing use `slides-agent`.

Install it into a Python 3.12-capable tool environment:

```bash
python3.12 -m pip install slides-agent
```

Authenticate once:

```bash
slides-agent auth login --credentials-file /path/to/client_secret.json
```

Optional environment variables:

```bash
export ATHENA_SLIDES_AGENT_BINARY="$(python3.12 -m site --user-base)/bin/slides-agent"
export ATHENA_SLIDES_AGENT_TIMEOUT_SECS=20
export SLIDES_AGENT_CREDENTIALS=/path/to/client_secret.json
export SLIDES_AGENT_TOKEN_FILE=$HOME/.config/slides-agent/token.json
```

Verify:

```bash
slides-agent auth status
slides-agent deck inspect --presentation-id <presentation_id>
```

### 3. Tray app

In a separate terminal:

```bash
cd apps/tray/src-tauri

# ATHENA_WS_URL defaults to ws://localhost:8000/ws if not set
cargo run
```

A status dot appears in your menu bar:
- **Gray** — idle / not connected
- **Yellow** — connecting
- **Green** — live, mic active

Click the icon to start a session. Speak naturally. Click again to end.

---

## Cloud Run Deployment

This deploys the backend to Google Cloud Run so the tray app works without a local server running.

### One-time setup

**Grant Cloud Run's service account the Vertex AI role:**
```bash
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

**Create the API key secret** (for AI Studio Live Voice):
```bash
# Get a Gemini API key from aistudio.google.com first, then:
echo -n "AIz..." | gcloud secrets create athena-api-key --data-file=- --project=YOUR_PROJECT_ID
```

**Authenticate gogcli:**
```bash
gog auth add you@gmail.com
```

### Deploy

```bash
export GOOGLE_CLOUD_PROJECT=your_project_id
export GOG_ACCOUNT=you@gmail.com
# export GOOGLE_CLOUD_LOCATION=us-central1  # optional, defaults to us-central1

cd backend/adk-bidi
bash deploy.sh
```

The script:
1. Enables required Google Cloud APIs
2. Creates an Artifact Registry repository
3. Uploads gogcli OAuth credentials to Secret Manager
4. Requires a LangSmith API key secret for tracing
5. Builds the Docker image via Cloud Build (multi-stage: Go for `gog` binary + Python app)
6. Deploys to Cloud Run with secrets injected as environment variables

At the end it prints the WebSocket URL:
```
WebSocket endpoint: wss://athena-backend-xxxx-uc.a.run.app/ws
```

### Connect the tray app to Cloud Run

```bash
cd apps/tray/src-tauri
ATHENA_WS_URL=wss://athena-backend-xxxx-uc.a.run.app/ws cargo run
```

Or export it permanently in your shell profile:
```bash
export ATHENA_WS_URL=wss://athena-backend-xxxx-uc.a.run.app/ws
```

### Rotating Google Workspace tokens

gogcli OAuth tokens expire periodically (Google Workspace security policy). When workspace features stop working, rotate the token:

```bash
# Re-authenticate locally
gog auth add you@gmail.com

# Upload fresh token to Secret Manager
gog auth tokens export you@gmail.com --out=/tmp/gog-tokens.json --overwrite && \
  gcloud secrets versions add gog-tokens --data-file=/tmp/gog-tokens.json --project=YOUR_PROJECT_ID && \
  rm /tmp/gog-tokens.json

# Force Cloud Run to pick up the new token (creates a new revision)
gcloud run services update athena-backend \
  --update-env-vars="DEPLOY_TIME=$(date -u +%Y%m%dT%H%M%S)" \
  --region=us-central1 \
  --project=YOUR_PROJECT_ID
```

---

## Environment Variables Reference

All variables go in `backend/adk-bidi/app/.env` for local dev. Cloud Run sets them automatically via `deploy.sh`.

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` | `TRUE` for Vertex AI, `FALSE` for AI Studio |
| `GOOGLE_API_KEY` | — | AI Studio API key (required when not using Vertex AI) |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project ID (required for Vertex AI) |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | GCP region |
| `ATHENA_MODEL` | — | Gemini Live model ID |
| `ATHENA_COORDINATOR_MODEL` | — | Model for WorkspaceCoordinator |
| `ATHENA_PLANNER_MODEL` | — | Model for PlannerAgent |
| `ATHENA_SPECIALIST_MODEL` | — | Model for specialist LlmAgents |
| `ATHENA_EMBED_MODEL` | `gemini-embedding-001` | Embedding model for semantic retrieval |
| `ATHENA_EMBED_DIM` | `768` | Embedding dimension |
| `LIVE_VOICE_FORCE_AI_STUDIO` | — | Force Live Voice to AI Studio even when Vertex AI is default |
| `LANGSMITH_API_KEY` | — | LangSmith API key for end-to-end tracing |
| `LANGSMITH_PROJECT` | `athena-local` | LangSmith project name |
| `REFLECTION_IDLE_SECS` | `120` | Seconds of idle before post-session memory reflection runs |
| `PROACTIVE_SILENCE_SECS` | `1.5` | Silence window before injecting a background job result |
| `GOG_ACCOUNT` | — | Google account email for workspace access |
| `GOG_KEYRING_BACKEND` | — | Set to `file` for Cloud Run (no system keyring in containers) |
| `GOG_KEYRING_PASSWORD` | — | Encryption password for file-based keyring |

**Dual-provider mode** (Cloud Run recommended): Set `GOOGLE_GENAI_USE_VERTEXAI=TRUE` for workspace agents and `LIVE_VOICE_FORCE_AI_STUDIO=TRUE` to route the real-time voice stream through AI Studio. This avoids Vertex AI's Live API region restrictions while keeping workspace calls on Vertex.

---

## Project Structure

```
athena/
├── apps/
│   └── tray/                       # macOS menu bar app (Rust/Tauri v2)
│       └── src-tauri/
│           ├── src/
│           │   ├── main.rs         # Tray setup, session management, menu
│           │   ├── ws.rs           # WebSocket client + audio/screen transport
│           │   ├── audio.rs        # Mic capture, speaker playback, VAD
│           │   └── screen.rs       # Screen capture (JPEG, 1 frame/sec)
│           └── Cargo.toml
│
└── backend/
    └── adk-bidi/                   # Python ADK streaming backend
        ├── app/
        │   ├── main.py             # FastAPI app + route registration
        │   ├── ws.py               # WebSocket handler (audio, silence monitor)
        │   ├── session_manager.py  # Wires agents, orchestrator, retrieval
        │   ├── adk_agents/
        │   │   ├── live_voice.py   # LiveVoiceAgent (Gemini Live, ADK)
        │   │   ├── workspace_coordinator.py  # Headless ADK runner
        │   │   └── specialists/    # gmail, drive, docs, calendar, sheets, slides
        │   ├── jobs/               # JobQueue, JobStore, JobDispatcher, ResultInjector
        │   ├── planner/            # PlannerAgent, ExecutionEngine, SkillLibrary
        │   ├── retrieval/          # Embedder, VectorStore, Chunker, Indexer
        │   ├── tools/              # WorkspaceBackend protocol + typed ADK tools
        │   ├── memory_service.py   # Filesystem memory store (~/.athena/)
        │   └── reflection_agent.py # Post-session summarization
        ├── Dockerfile              # Cloud Run image (multi-stage: Go + Python)
        ├── start.sh                # Container entrypoint (gog auth init)
        ├── deploy.sh               # Full Cloud Run deployment script
        └── pyproject.toml
```

---

## Troubleshooting

**Tray app shows gray / won't connect**
- Is the backend running? Check `curl http://localhost:8000/health`
- Is `ATHENA_WS_URL` pointing at the right address?

**"Connection reset without closing handshake" in logs**
- Normal. When the tray app closes, the WebSocket closes with a standard 1000 (Normal Closure) code — not an error.

**Workspace features return errors or empty results**
- Run `gog gmail threads` locally to confirm gogcli auth is working
- If expired, follow the [token rotation steps](#rotating-google-workspace-tokens) above

**`invalid_grant` / `invalid_rapt` on Cloud Run**
- Google Workspace security policy requires periodic re-authentication
- Follow the token rotation steps above

**Cloud Build fails: `requires go >= 1.25.x`**
- The `Dockerfile` uses `golang:1.25-alpine` — ensure it hasn't been reverted

**"No configuration change requested" when trying to restart Cloud Run**
- Use `--update-env-vars="DEPLOY_TIME=$(date -u +%Y%m%dT%H%M%S)"` to force a new revision without rebuilding

---

## Built for the Gemini Live Agent Challenge

Athena was built for the [Gemini Live Agent Challenge](https://geminiliveagentchallenge.devpost.com/) in the **Live Agents** category.

The core thesis: the text-box paradigm isn't just inconvenient — it breaks the flow of actual work. A live agent that can hear you, see your screen, run background jobs, and speak up when it has something to say is fundamentally different from a chatbot you query. That's what Athena is.

**#GeminiLiveAgentChallenge**
