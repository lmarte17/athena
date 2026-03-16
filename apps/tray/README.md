# Athena Tray App (Phase 0)

This folder now includes the core real-time voice path:

1. Tauri v2 tray app scaffold
2. Menu bar tray icon (no popover) with readiness badge
3. Reconnecting WebSocket client to `ws://localhost:8000/ws`
4. Session toggle (click once = start session, click again = stop session)
5. Mic capture + PCM streaming to backend
6. Streaming PCM playback from backend
7. Barge-in support (mic stays active while Athena is speaking)
8. Continuous screen capture (1 frame/sec) while session is open

## Run

From this directory:

```bash
cd src-tauri
cargo run
```

Optional:
- Set `ATHENA_OUTPUT_PCM_RATE` if backend PCM output rate differs (default client assumption: `24000`).

## Current behavior

- Left-click tray icon:
  - first click starts backend connection and turns talking on
  - second click stops the session (WS + reconnect loop + mic stream)
- Right-click menu: includes `Quit`.
- Tray icon shows readiness via a top-right badge:
  - gray: idle (not started)
  - yellow: connecting / not ready
  - green: live / ready
- Microphone audio is captured and streamed as raw PCM (`16kHz`, `16-bit`, mono).
- Binary audio from backend is played in real time (resampled to device output rate).
- Primary-display screen frames are captured once per second and sent as `image/jpeg` events.
- WebSocket loop auto-reconnects every 2 seconds after disconnect/error.
- Server events are handled (`status`, transcripts, `interrupted`, `turn_complete`, `error`).

## Next milestones

- In-tray transcript surface
