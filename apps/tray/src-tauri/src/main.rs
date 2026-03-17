#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod audio;
mod screen;
mod ws;

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock};

use audio::{AudioEngine, AudioPlayback};
use tauri::image::Image;
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};
use screen::ScreenCaptureController;
use ws::{WsClientController, WsTransport};

fn backend_http_base() -> String {
    let ws_url = std::env::var("ATHENA_WS_URL")
        .unwrap_or_else(|_| "ws://localhost:8000/ws".to_string());
    ws_url
        .replace("wss://", "https://")
        .replace("ws://", "http://")
        .trim_end_matches("/ws")
        .to_string()
}

// ── Tauri commands ────────────────────────────────────────────────────────────

#[derive(serde::Deserialize, serde::Serialize)]
pub struct SessionSummary {
    filename: String,
    content: String,
}

#[derive(serde::Deserialize, serde::Serialize)]
pub struct CommitmentSnapshot {
    id: String,
    text: String,
    status: String,
    updated_at: Option<String>,
}

#[derive(serde::Deserialize, serde::Serialize)]
pub struct MemorySnapshot {
    profile_yaml: String,
    commitments: Vec<CommitmentSnapshot>,
    sessions: Vec<SessionSummary>,
    pending_candidates: usize,
}

fn curl_json(url: &str, method: &str) -> Result<serde_json::Value, String> {
    let output = std::process::Command::new("curl")
        .args(["-s", "-X", method, url])
        .output()
        .map_err(|e| format!("curl error: {e}"))?;
    if !output.status.success() {
        return Err(format!("backend request failed: {}", output.status));
    }
    serde_json::from_slice(&output.stdout).map_err(|e| format!("Invalid backend JSON: {e}"))
}

/// Read the backend memory snapshot so the tray and live app share one model.
#[tauri::command]
fn get_memory_snapshot() -> Result<MemorySnapshot, String> {
    let url = format!("{}/memory", backend_http_base());
    let payload = curl_json(&url, "GET")?;
    serde_json::from_value(payload).map_err(|e| format!("Memory snapshot decode failed: {e}"))
}

/// Clear memory through the backend so live-session state and UI stay aligned.
#[tauri::command]
fn clear_memory() -> Result<String, String> {
    let url = format!("{}/memory/clear", backend_http_base());
    let payload = curl_json(&url, "POST")?;
    let archived = payload
        .get("archived")
        .and_then(|value| value.as_str())
        .unwrap_or("memory archive");
    eprintln!("[athena] Memory cleared via backend");
    Ok(format!("Cleared memory ({archived})."))
}

/// Remove a single key from profile.yaml by calling the backend DELETE endpoint.
/// The backend handles YAML parsing; the tray just passes the key name.
#[tauri::command]
async fn forget_profile_key(key: String) -> Result<(), String> {
    let url = format!("{}/memory/profile/{key}", backend_http_base());
    tokio::task::spawn_blocking(move || {
        std::process::Command::new("curl")
            .args(["-s", "-X", "DELETE", &url])
            .output()
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| format!("curl error: {e}"))?;
    Ok(())
}

const TRAY_ID: &str = "athena-tray";
const ICON_SIZE: u32 = 32;

#[derive(Clone, Copy, Debug)]
pub enum ConnectionStatus {
    Disconnected,
    Connecting,
    Working,
    Live,
}

struct AppState {
    status: Arc<RwLock<ConnectionStatus>>,
    talk_enabled: Arc<AtomicBool>,
    muted: Arc<AtomicBool>,
    athena_playing: Arc<AtomicBool>,
    user_speaking: Arc<AtomicBool>,
    session_active: AtomicBool,
    session_controller: Mutex<Option<WsClientController>>,
    screen_controller: Mutex<Option<ScreenCaptureController>>,
    transport: WsTransport,
    playback: AudioPlayback,
    session_id: Arc<RwLock<Option<String>>>,
    /// Index into the Screen::all() list for screen sharing (default 0 = primary).
    selected_display: Arc<AtomicUsize>,
}

pub fn apply_connection_status(
    app: &AppHandle,
    status_state: &Arc<RwLock<ConnectionStatus>>,
    talk_enabled: &Arc<AtomicBool>,
    next: ConnectionStatus,
) {
    if let Ok(mut status) = status_state.write() {
        *status = next;
    }

    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_icon(Some(build_status_icon(next)));
        let talking = talk_enabled.load(Ordering::Relaxed);
        let _ = tray.set_tooltip(Some(build_tooltip(next, talking)));
    }
}

fn build_tooltip(status: ConnectionStatus, talking: bool) -> String {
    match status {
        ConnectionStatus::Disconnected => "Athena: idle (click to start)".to_string(),
        ConnectionStatus::Connecting => "Athena: getting ready...".to_string(),
        ConnectionStatus::Working => "Athena: fetching context...".to_string(),
        ConnectionStatus::Live => {
            if talking {
                "Athena: listening (click to mute)".to_string()
            } else {
                "Athena: ready (click to talk)".to_string()
            }
        }
    }
}

fn build_status_icon(status: ConnectionStatus) -> Image<'static> {
    let mut rgba = vec![0u8; (ICON_SIZE * ICON_SIZE * 4) as usize];

    // Base Athena glyph (ring).
    draw_disc(&mut rgba, ICON_SIZE, 14, 16, 8, [223, 231, 239, 255]);
    draw_disc(&mut rgba, ICON_SIZE, 14, 16, 4, [58, 72, 86, 255]);

    // Top-right badge border.
    draw_disc(&mut rgba, ICON_SIZE, 24, 8, 5, [242, 246, 250, 220]);

    let badge = match status {
        ConnectionStatus::Disconnected => [147, 159, 170, 255],
        ConnectionStatus::Connecting => [243, 192, 58, 255],
        ConnectionStatus::Working => [243, 192, 58, 255],
        ConnectionStatus::Live => [57, 198, 114, 255],
    };
    draw_disc(&mut rgba, ICON_SIZE, 24, 8, 4, badge);

    Image::new_owned(rgba, ICON_SIZE, ICON_SIZE)
}

fn draw_disc(buffer: &mut [u8], size: u32, cx: i32, cy: i32, radius: i32, color: [u8; 4]) {
    let radius2 = radius * radius;

    for y in (cy - radius)..=(cy + radius) {
        for x in (cx - radius)..=(cx + radius) {
            let dx = x - cx;
            let dy = y - cy;
            if dx * dx + dy * dy > radius2 {
                continue;
            }

            if x < 0 || y < 0 || x >= size as i32 || y >= size as i32 {
                continue;
            }

            let idx = ((y as u32 * size + x as u32) * 4) as usize;
            buffer[idx..idx + 4].copy_from_slice(&color);
        }
    }
}

fn build_tray_menu<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    muted: bool,
    screen_names: &[String],
) -> tauri::Result<tauri::menu::Menu<R>> {
    let mute_label = if muted { "Unmute mic" } else { "Mute mic" };
    let mute_item = MenuItemBuilder::with_id("mute_toggle", mute_label).build(app)?;

    let screen_items: Vec<_> = screen_names
        .iter()
        .enumerate()
        .map(|(i, name)| MenuItemBuilder::with_id(format!("screen_{i}"), name).build(app))
        .collect::<Result<Vec<_>, _>>()?;
    let mut screen_sub = SubmenuBuilder::new(app, "Share screen");
    for item in &screen_items {
        screen_sub = screen_sub.item(item);
    }
    let screen_submenu = screen_sub.build()?;

    let memory_view_item =
        MenuItemBuilder::with_id("memory_view", "What does Athena know?").build(app)?;
    let memory_clear_item =
        MenuItemBuilder::with_id("memory_clear", "Clear all memory").build(app)?;
    let quit_item = MenuItemBuilder::with_id("quit", "Quit").build(app)?;

    MenuBuilder::new(app)
        .item(&mute_item)
        .item(&screen_submenu)
        .item(&PredefinedMenuItem::separator(app)?)
        .item(&memory_view_item)
        .item(&memory_clear_item)
        .item(&PredefinedMenuItem::separator(app)?)
        .item(&quit_item)
        .build()
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            get_memory_snapshot,
            clear_memory,
            forget_profile_key,
        ])
        .setup(|app| {
            let status = Arc::new(RwLock::new(ConnectionStatus::Disconnected));
            let talk_enabled = Arc::new(AtomicBool::new(false));
            let muted = Arc::new(AtomicBool::new(false));
            let athena_playing = Arc::new(AtomicBool::new(false));
            let user_speaking = Arc::new(AtomicBool::new(false));
            let transport = WsTransport::default();
            let audio_engine = AudioEngine::start(
                talk_enabled.clone(),
                muted.clone(),
                transport.clone(),
                athena_playing.clone(),
                user_speaking.clone(),
            )
            .map_err(std::io::Error::other)?;
            let playback = audio_engine.playback_handle();

            // Keep cpal streams alive for the process lifetime.
            let _ = Box::leak(Box::new(audio_engine));

            let selected_display = Arc::new(AtomicUsize::new(0));

            app.manage(AppState {
                status,
                talk_enabled,
                muted,
                athena_playing,
                user_speaking,
                session_active: AtomicBool::new(false),
                session_controller: Mutex::new(None),
                screen_controller: Mutex::new(None),
                transport,
                playback,
                session_id: Arc::new(RwLock::new(None)),
                selected_display,
            });

            let screen_names = screen::list_screen_names();
            let tray_menu = build_tray_menu(app.handle(), false, &screen_names)?;

            let tray_builder = TrayIconBuilder::with_id(TRAY_ID)
                .menu(&tray_menu)
                .icon(build_status_icon(ConnectionStatus::Disconnected))
                .on_menu_event(|app, event| {
                    // "screen_N" items switch the active capture display.
                    let id = event.id().as_ref();
                    if let Some(rest) = id.strip_prefix("screen_") {
                        if let Ok(idx) = rest.parse::<usize>() {
                            let state = app.state::<AppState>();
                            state.selected_display.store(idx, Ordering::Relaxed);
                            eprintln!("[athena] screen share: display {idx}");
                        }
                        return;
                    }
                    match id {
                    "mute_toggle" => {
                        let state = app.state::<AppState>();
                        let was_muted = state.muted.fetch_xor(true, Ordering::Relaxed);
                        let now_muted = !was_muted;
                        eprintln!("[athena] mic {}", if now_muted { "muted" } else { "unmuted" });
                        let screen_names = screen::list_screen_names();
                        if let Ok(new_menu) = build_tray_menu(app, now_muted, &screen_names) {
                            if let Some(tray) = app.tray_by_id(TRAY_ID) {
                                let _ = tray.set_menu(Some(new_menu));
                            }
                        }
                    }
                    "quit" => app.exit(0),
                    "memory_view" => {
                        // Focus the existing window if already open, otherwise create it.
                        if let Some(win) = app.get_webview_window("memory") {
                            let _ = win.show();
                            let _ = win.set_focus();
                        } else {
                            let _ = WebviewWindowBuilder::new(
                                app,
                                "memory",
                                WebviewUrl::App("memory.html".into()),
                            )
                            .title("What Athena Knows")
                            .inner_size(680.0, 620.0)
                            .resizable(true)
                            .build();
                        }
                    }
                    "memory_clear" => match clear_memory() {
                        Ok(_) => eprintln!("[athena] Memory cleared from menu"),
                        Err(e) => eprintln!("[athena] Clear memory error: {e}"),
                    },
                    _ => {}
                    }
                })
                .show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        let state = app.state::<AppState>();
                        let shared = state.inner();
                        let active = shared.session_active.load(Ordering::Relaxed);

                        if !active {
                            shared.session_active.store(true, Ordering::Relaxed);
                            shared.talk_enabled.store(true, Ordering::Relaxed);
                            shared.muted.store(false, Ordering::Relaxed);
                            let screen_names = screen::list_screen_names();
                            if let Ok(menu) = build_tray_menu(app, false, &screen_names) {
                                if let Some(tray) = app.tray_by_id(TRAY_ID) {
                                    let _ = tray.set_menu(Some(menu));
                                }
                            }
                            apply_connection_status(
                                app,
                                &shared.status,
                                &shared.talk_enabled,
                                ConnectionStatus::Connecting,
                            );
                            let controller = ws::spawn_ws_client(
                                app.clone(),
                                shared.status.clone(),
                                shared.talk_enabled.clone(),
                                shared.transport.clone(),
                                shared.playback.clone(),
                                shared.athena_playing.clone(),
                                shared.session_id.clone(),
                            );
                            if let Ok(mut controller_slot) = shared.session_controller.lock() {
                                *controller_slot = Some(controller);
                            }
                            if let Ok(mut screen_slot) = shared.screen_controller.lock() {
                                let screen_controller = screen::spawn_screen_capture_loop(
                                    shared.transport.clone(),
                                    shared.user_speaking.clone(),
                                    shared.selected_display.clone(),
                                );
                                *screen_slot = Some(screen_controller);
                            }
                            return;
                        }

                        shared.session_active.store(false, Ordering::Relaxed);
                        shared.talk_enabled.store(false, Ordering::Relaxed);

                        if let Ok(mut controller_slot) = shared.session_controller.lock() {
                            if let Some(controller) = controller_slot.take() {
                                controller.stop();
                            }
                        }
                        if let Ok(mut screen_slot) = shared.screen_controller.lock() {
                            if let Some(controller) = screen_slot.take() {
                                controller.stop();
                            }
                        }

                        shared.transport.clear_sender();
                        shared.playback.clear();
                        shared.athena_playing.store(false, Ordering::Relaxed);
                        apply_connection_status(
                            app,
                            &shared.status,
                            &shared.talk_enabled,
                            ConnectionStatus::Disconnected,
                        );
                    }
                })
                .icon_as_template(false)
                .tooltip("Athena: idle (click to start)");

            tray_builder.build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri app");
}
