use std::process::Command as StdCommand;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use tauri::AppHandle;
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio::sync::watch;
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::Message;

use crate::audio::AudioPlayback;
use crate::{apply_connection_status, ConnectionStatus};

// ── Cloud Run auth helpers ─────────────────────────────────────────────────────

/// Returns true for any non-localhost wss:// URL (i.e. Cloud Run / remote).
fn is_remote_url(url: &str) -> bool {
    let host = url
        .trim_start_matches("wss://")
        .trim_start_matches("ws://")
        .split('/')
        .next()
        .unwrap_or("");
    !host.is_empty() && !host.starts_with("localhost") && !host.starts_with("127.")
}

/// Fetches a short-lived Google identity token via `gcloud`. Runs in a
/// blocking thread so it doesn't stall the async runtime.
async fn fetch_gcloud_token() -> Option<String> {
    tokio::task::spawn_blocking(|| {
        let output = StdCommand::new("gcloud")
            .args(["auth", "print-identity-token"])
            .output()
            .map_err(|e| eprintln!("gcloud not found: {e}"))
            .ok()?;

        if output.status.success() {
            let token = String::from_utf8(output.stdout).ok()?.trim().to_string();
            if token.is_empty() { None } else { Some(token) }
        } else {
            eprintln!(
                "gcloud token error: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            );
            None
        }
    })
    .await
    .ok()
    .flatten()
}

const DEFAULT_WS_URL: &str = "ws://localhost:8000/ws";
const RECONNECT_DELAY: Duration = Duration::from_secs(2);

fn backend_ws_url() -> String {
    std::env::var("ATHENA_WS_URL").unwrap_or_else(|_| DEFAULT_WS_URL.to_string())
}

#[allow(dead_code)]
#[derive(Debug, Clone)]
pub enum OutboundMessage {
    Audio(Vec<u8>),
    Text(String),
    Image {
        data_base64: String,
        mime_type: String,
    },
    ActivityStart,
    ActivityEnd,
}

#[derive(Clone, Default)]
pub struct WsTransport {
    outbound: Arc<RwLock<Option<UnboundedSender<OutboundMessage>>>>,
}

impl WsTransport {
    pub fn set_sender(&self, sender: UnboundedSender<OutboundMessage>) {
        if let Ok(mut lock) = self.outbound.write() {
            *lock = Some(sender);
        }
    }

    pub fn clear_sender(&self) {
        if let Ok(mut lock) = self.outbound.write() {
            *lock = None;
        }
    }

    pub fn send_audio(&self, bytes: Vec<u8>) {
        self.send(OutboundMessage::Audio(bytes));
    }

    #[allow(dead_code)]
    pub fn send_text(&self, text: String) {
        self.send(OutboundMessage::Text(text));
    }

    #[allow(dead_code)]
    pub fn send_image(&self, data_base64: String, mime_type: String) {
        self.send(OutboundMessage::Image {
            data_base64,
            mime_type,
        });
    }

    pub fn send_activity_start(&self) {
        self.send(OutboundMessage::ActivityStart);
    }

    pub fn send_activity_end(&self) {
        self.send(OutboundMessage::ActivityEnd);
    }

    fn send(&self, message: OutboundMessage) {
        let sender = self
            .outbound
            .read()
            .ok()
            .and_then(|lock| lock.as_ref().cloned());

        if let Some(sender) = sender {
            let _ = sender.send(message);
        }
    }
}

#[derive(Clone)]
pub struct WsClientController {
    stop_tx: watch::Sender<bool>,
}

impl WsClientController {
    pub fn stop(&self) {
        let _ = self.stop_tx.send(true);
    }
}

pub fn spawn_ws_client(
    app: AppHandle,
    status: Arc<RwLock<ConnectionStatus>>,
    talk_enabled: Arc<AtomicBool>,
    transport: WsTransport,
    playback: AudioPlayback,
    athena_playing: Arc<AtomicBool>,
    session_id: Arc<RwLock<Option<String>>>,
) -> WsClientController {
    let (stop_tx, mut stop_rx) = watch::channel(false);
    let ws_url = backend_ws_url();
    eprintln!("backend ws: {ws_url}");

    tauri::async_runtime::spawn(async move {
        loop {
            if stop_requested(&stop_rx) {
                break;
            }

            update_status(&app, &status, &talk_enabled, ConnectionStatus::Connecting);

            // Every websocket connection gets a fresh backend session.
            let connect_url = ws_url.clone();

            // For remote (Cloud Run) URLs, attach a gcloud identity token.
            let auth_token = if is_remote_url(&ws_url) {
                fetch_gcloud_token().await
            } else {
                None
            };

            let connect_result = tokio::select! {
                _ = wait_for_stop(&mut stop_rx) => break,
                result = async {
                    if let Some(token) = auth_token {
                        eprintln!("ws: using gcloud identity token");
                        let mut req = connect_url
                            .as_str()
                            .into_client_request()
                            .expect("invalid ws url");
                        req.headers_mut().insert(
                            http::header::AUTHORIZATION,
                            http::HeaderValue::from_str(&format!("Bearer {token}"))
                                .expect("invalid token header value"),
                        );
                        connect_async(req).await
                    } else {
                        connect_async(connect_url.as_str()).await
                    }
                } => result,
            };

            match connect_result {
                Ok((stream, _response)) => {
                    update_status(&app, &status, &talk_enabled, ConnectionStatus::Live);
                    let (mut ws_write, mut ws_read) = stream.split();
                    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<OutboundMessage>();
                    transport.set_sender(out_tx);
                    let mut should_stop = false;

                    loop {
                        tokio::select! {
                            _ = wait_for_stop(&mut stop_rx) => {
                                should_stop = true;
                                break;
                            }
                            outbound = out_rx.recv() => {
                                match outbound {
                                    Some(OutboundMessage::Audio(bytes)) => {
                                        if ws_write.send(Message::Binary(bytes.into())).await.is_err() {
                                            break;
                                        }
                                    }
                                    Some(OutboundMessage::Text(text)) => {
                                        let payload = json!({
                                            "type": "text",
                                            "text": text,
                                        }).to_string();
                                        if ws_write.send(Message::Text(payload.into())).await.is_err() {
                                            break;
                                        }
                                    }
                                    Some(OutboundMessage::Image { data_base64, mime_type }) => {
                                        let payload = json!({
                                            "type": "image",
                                            "data": data_base64,
                                            "mime_type": mime_type,
                                        }).to_string();
                                        if ws_write.send(Message::Text(payload.into())).await.is_err() {
                                            break;
                                        }
                                    }
                                    Some(OutboundMessage::ActivityStart) => {
                                        let payload = json!({"type": "activity_start"}).to_string();
                                        if ws_write.send(Message::Text(payload.into())).await.is_err() {
                                            break;
                                        }
                                    }
                                    Some(OutboundMessage::ActivityEnd) => {
                                        let payload = json!({"type": "activity_end"}).to_string();
                                        if ws_write.send(Message::Text(payload.into())).await.is_err() {
                                            break;
                                        }
                                    }
                                    None => break,
                                }
                            }
                            inbound = ws_read.next() => {
                                match inbound {
                                    Some(Ok(Message::Binary(bytes))) => {
                                        athena_playing.store(true, Ordering::Relaxed);
                                        playback.push_pcm_bytes(&bytes);
                                    }
                                    Some(Ok(Message::Text(text))) => {
                                        handle_server_event(
                                            &app,
                                            &status,
                                            &talk_enabled,
                                            &playback,
                                            &athena_playing,
                                            &session_id,
                                            &text,
                                        );
                                    }
                                    Some(Ok(Message::Close(_))) => break,
                                    Some(Ok(_)) => {}
                                    Some(Err(err)) => {
                                        eprintln!("ws read error: {err}");
                                        break;
                                    }
                                    None => break,
                                }
                            }
                        }
                    }

                    transport.clear_sender();
                    playback.clear();
                    athena_playing.store(false, Ordering::Relaxed);
                    if let Ok(mut lock) = session_id.write() {
                        *lock = None;
                    }

                    if should_stop {
                        break;
                    }
                }
                Err(err) => {
                    eprintln!("ws connect error: {err}");
                    if let Ok(mut lock) = session_id.write() {
                        *lock = None;
                    }
                }
            }

            tokio::select! {
                _ = wait_for_stop(&mut stop_rx) => break,
                _ = sleep(RECONNECT_DELAY) => {}
            }
        }

        transport.clear_sender();
        playback.clear();
        if let Ok(mut lock) = session_id.write() {
            *lock = None;
        }
    });

    WsClientController { stop_tx }
}

fn stop_requested(stop_rx: &watch::Receiver<bool>) -> bool {
    *stop_rx.borrow()
}

async fn wait_for_stop(stop_rx: &mut watch::Receiver<bool>) {
    if *stop_rx.borrow() {
        return;
    }

    loop {
        if stop_rx.changed().await.is_err() {
            return;
        }
        if *stop_rx.borrow() {
            return;
        }
    }
}

fn handle_server_event(
    app: &AppHandle,
    status: &Arc<RwLock<ConnectionStatus>>,
    talk_enabled: &Arc<AtomicBool>,
    playback: &AudioPlayback,
    athena_playing: &Arc<AtomicBool>,
    session_id: &Arc<RwLock<Option<String>>>,
    raw_text: &str,
) {
    let parsed = match serde_json::from_str::<serde_json::Value>(raw_text) {
        Ok(value) => value,
        Err(err) => {
            eprintln!("ws text parse error: {err}; payload={raw_text}");
            return;
        }
    };

    let event_type = parsed.get("type").and_then(|v| v.as_str()).unwrap_or_default();

    match event_type {
        "status" => {
            let raw_status = parsed.get("status").and_then(|v| v.as_str()).unwrap_or_default();
            if raw_status == "connected" {
                if let Some(id) = parsed.get("session_id").and_then(|v| v.as_str()) {
                    if let Ok(mut lock) = session_id.write() {
                        *lock = Some(id.to_string());
                    }
                    eprintln!("ws: session_id={id}");
                }
                update_status(app, status, talk_enabled, ConnectionStatus::Live);
            }
        }
        "working" => {
            if let Some(hint) = parsed.get("hint").and_then(|v| v.as_str()) {
                eprintln!("athena working: {hint}");
            }
            update_status(app, status, talk_enabled, ConnectionStatus::Working);
        }
        "ready" => {
            update_status(app, status, talk_enabled, ConnectionStatus::Live);
        }
        "transcript_in" => {
            if let Some(text) = parsed.get("text").and_then(|v| v.as_str()) {
                println!("user: {text}");
            }
        }
        "transcript_out" => {
            if let Some(text) = parsed.get("text").and_then(|v| v.as_str()) {
                println!("athena: {text}");
            }
        }
        "interrupted" => {
            athena_playing.store(false, Ordering::Relaxed);
            playback.clear();
            println!("athena interrupted");
        }
        "turn_complete" => {
            // Don't flip athena_playing here — defer to when the audio queue
            // actually drains.  The server sends turn_complete before the last
            // buffered PCM chunk has been played, so setting false too early
            // would start the VAD cooldown while Athena's voice is still
            // coming out of the speakers, causing echo to trigger speech
            // detection and create phantom turns.
            playback.signal_turn_complete();
            println!("turn complete");
        }
        "error" => {
            let err = parsed
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown backend error");
            eprintln!("backend error: {err}");
        }
        _ => {
            println!("ws event: {raw_text}");
        }
    }
}

fn update_status(
    app: &AppHandle,
    status: &Arc<RwLock<ConnectionStatus>>,
    talk_enabled: &Arc<AtomicBool>,
    next: ConnectionStatus,
) {
    apply_connection_status(app, status, talk_enabled, next);
}
