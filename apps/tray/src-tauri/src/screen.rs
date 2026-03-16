use std::io::Cursor;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use image::{ColorType, ImageEncoder};
use screenshots::Screen;
use tokio::sync::watch;
use tokio::time::sleep;

use crate::ws::WsTransport;

const CAPTURE_INTERVAL: Duration = Duration::from_secs(1);
const JPEG_QUALITY: u8 = 70;

#[derive(Clone)]
pub struct ScreenCaptureController {
    stop_tx: watch::Sender<bool>,
}

impl ScreenCaptureController {
    pub fn stop(&self) {
        let _ = self.stop_tx.send(true);
    }
}

/// Returns a human-readable label for each connected display, in the same
/// order that `Screen::all()` returns them.  Used to populate the tray menu.
pub fn list_screen_names() -> Vec<String> {
    Screen::all()
        .unwrap_or_default()
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let info = &s.display_info;
            if info.is_primary {
                format!("Display {} – {}×{} (Primary)", i + 1, info.width, info.height)
            } else {
                format!("Display {} – {}×{}", i + 1, info.width, info.height)
            }
        })
        .collect()
}

pub fn spawn_screen_capture_loop(
    transport: WsTransport,
    user_speaking: Arc<AtomicBool>,
    selected_display: Arc<AtomicUsize>,
) -> ScreenCaptureController {
    let (stop_tx, mut stop_rx) = watch::channel(false);

    tauri::async_runtime::spawn(async move {
        loop {
            if stop_requested(&stop_rx) {
                break;
            }

            // Only send screen frames while the user is actively speaking.
            // Sending images at all times causes the model to proactively
            // respond to screen changes even without a user turn, creating
            // phantom interruption loops.
            if user_speaking.load(Ordering::Relaxed) {
                let index = selected_display.load(Ordering::Relaxed);
                let encoded = tokio::task::spawn_blocking(move || capture_screen_as_jpeg_base64(index))
                    .await
                    .ok()
                    .flatten();

                if let Some(frame_base64) = encoded {
                    transport.send_image(frame_base64, "image/jpeg".to_string());
                }
            }

            tokio::select! {
                _ = wait_for_stop(&mut stop_rx) => break,
                _ = sleep(CAPTURE_INTERVAL) => {}
            }
        }
    });

    ScreenCaptureController { stop_tx }
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

fn capture_screen_as_jpeg_base64(index: usize) -> Option<String> {
    let screens = Screen::all().ok()?;
    // Fall back to the primary (first) screen if the index is out of range.
    let screen = if index < screens.len() { &screens[index] } else { screens.first()? };
    let frame = screen.capture().ok()?;
    let (width, height) = (frame.width(), frame.height());
    let rgba = frame.into_raw();

    // JPEG does not support alpha — strip the A channel before encoding.
    let rgb: Vec<u8> = rgba.chunks_exact(4).flat_map(|p| [p[0], p[1], p[2]]).collect();

    let mut jpeg_bytes = Vec::with_capacity((width * height) as usize);
    {
        let mut cursor = Cursor::new(&mut jpeg_bytes);
        let encoder = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut cursor, JPEG_QUALITY);
        encoder
            .write_image(&rgb, width, height, ColorType::Rgb8.into())
            .ok()?;
    }

    Some(STANDARD.encode(jpeg_bytes))
}
