use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, Stream, StreamConfig};

use crate::ws::WsTransport;

const TARGET_INPUT_RATE_HZ: u32 = 16_000;
const DEFAULT_SERVER_OUTPUT_RATE_HZ: u32 = 24_000;
const MAX_PLAYBACK_SECONDS: usize = 10;

/// RMS threshold for speech detection when Athena is silent.
/// Sits above typical fan/HVAC noise (~0.005–0.010) but below quiet speech.
const SPEECH_THRESHOLD: f32 = 0.012;
/// RMS threshold for barge-in while Athena is actively playing back.
/// Must exceed MacBook speaker echo (~0.015–0.035) but stay reachable with
/// normal-volume speech (~0.035–0.100).
const BARGE_IN_THRESHOLD: f32 = 0.035;
/// Stricter threshold applied during the post-playback cooldown window.
/// Guards against residual speaker reverb at the turn boundary.
const POST_PLAYBACK_THRESHOLD: f32 = 0.020;
/// How many callbacks of silence before ending a speech turn (~1000 ms at 10 ms/callback).
/// Longer hangover reduces false activity_end events during natural mid-sentence pauses —
/// previously 600 ms was too short and caused Athena to respond before the user finished.
const VAD_HANGOVER_CALLBACKS: u32 = 100;
/// Consecutive above-threshold callbacks required to confirm speech start
/// (~20 ms).  Filters brief noise spikes without delaying real speech.
const SPEECH_ONSET_CALLBACKS: u32 = 2;
/// Sustained barge-in required while Athena is playing (~50 ms).
/// Long enough to reject echo spikes; short enough not to feel sluggish.
const BARGE_IN_ONSET_CALLBACKS: u32 = 5;
/// Stricter onset required during the post-playback cooldown window (~50 ms).
const POST_PLAYBACK_ONSET_CALLBACKS: u32 = 5;
/// Callbacks to apply strict thresholds after playback ends (~500 ms).
/// Prevents mic from capturing speaker reverb at the turn boundary.
const PLAYBACK_COOLDOWN_CALLBACKS: u32 = 50;

#[derive(Clone)]
pub struct AudioPlayback {
    inner: Arc<Mutex<PlaybackState>>,
}

impl AudioPlayback {
    pub fn push_pcm_bytes(&self, bytes: &[u8]) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.push_pcm_bytes(bytes);
        }
    }

    pub fn clear(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.clear();
        }
    }

    /// Called when the server signals turn_complete.  Defers setting
    /// athena_playing=false until the audio queue actually drains so the VAD
    /// cooldown starts only after the last sample reaches the speakers.
    pub fn signal_turn_complete(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.signal_turn_complete();
        }
    }
}

pub struct AudioEngine {
    playback: AudioPlayback,
    _input_stream: Stream,
    _output_stream: Stream,
}

impl AudioEngine {
    pub fn start(
        talk_enabled: Arc<AtomicBool>,
        transport: WsTransport,
        athena_playing: Arc<AtomicBool>,
        user_speaking: Arc<AtomicBool>,
    ) -> Result<Self, String> {
        let host = cpal::default_host();
        let input_device = host
            .default_input_device()
            .ok_or_else(|| "No input device found".to_string())?;
        let output_device = host
            .default_output_device()
            .ok_or_else(|| "No output device found".to_string())?;

        let input_supported = input_device
            .default_input_config()
            .map_err(|err| format!("default input config error: {err}"))?;
        let output_supported = output_device
            .default_output_config()
            .map_err(|err| format!("default output config error: {err}"))?;

        let input_config: StreamConfig = input_supported.clone().into();
        let output_config: StreamConfig = output_supported.clone().into();

        let server_output_rate_hz = std::env::var("ATHENA_OUTPUT_PCM_RATE")
            .ok()
            .and_then(|value| value.parse::<u32>().ok())
            .unwrap_or(DEFAULT_SERVER_OUTPUT_RATE_HZ);

        let playback = AudioPlayback {
            inner: Arc::new(Mutex::new(PlaybackState::new(
                server_output_rate_hz,
                output_config.sample_rate.0,
                athena_playing.clone(),
            ))),
        };

        let output_stream = match output_supported.sample_format() {
            SampleFormat::F32 => build_output_stream_f32(&output_device, &output_config, playback.clone()),
            SampleFormat::I16 => build_output_stream_i16(&output_device, &output_config, playback.clone()),
            SampleFormat::U16 => build_output_stream_u16(&output_device, &output_config, playback.clone()),
            other => return Err(format!("Unsupported output sample format: {other:?}")),
        }
        .map_err(|err| format!("build output stream error: {err}"))?;

        let input_stream = match input_supported.sample_format() {
            SampleFormat::F32 => build_input_stream_f32(
                &input_device,
                &input_config,
                talk_enabled,
                transport,
                athena_playing,
                user_speaking.clone(),
            ),
            SampleFormat::I16 => build_input_stream_i16(
                &input_device,
                &input_config,
                talk_enabled,
                transport,
                athena_playing,
                user_speaking.clone(),
            ),
            SampleFormat::U16 => build_input_stream_u16(
                &input_device,
                &input_config,
                talk_enabled,
                transport,
                athena_playing,
                user_speaking.clone(),
            ),
            other => return Err(format!("Unsupported input sample format: {other:?}")),
        }
        .map_err(|err| format!("build input stream error: {err}"))?;

        output_stream
            .play()
            .map_err(|err| format!("output stream play error: {err}"))?;
        input_stream
            .play()
            .map_err(|err| format!("input stream play error: {err}"))?;

        println!(
            "audio ready: input_rate={}Hz output_rate={}Hz server_output_rate={}Hz",
            input_config.sample_rate.0, output_config.sample_rate.0, server_output_rate_hz
        );

        Ok(Self {
            playback,
            _input_stream: input_stream,
            _output_stream: output_stream,
        })
    }

    pub fn playback_handle(&self) -> AudioPlayback {
        self.playback.clone()
    }

}

#[derive(Debug)]
struct PlaybackState {
    source_rate_hz: f64,
    output_rate_hz: f64,
    source_pos: f64,
    queue: VecDeque<f32>,
    max_samples: usize,
    /// Set to true when the server signals turn_complete.  The output callback
    /// clears this and sets athena_playing=false the moment the queue drains.
    /// This ensures the VAD's post-playback cooldown starts only after the
    /// last PCM sample has actually been sent to the speakers — not when the
    /// server-side turn_complete event arrives, which can precede the last
    /// buffered chunk by several hundred milliseconds.
    turn_complete_pending: bool,
    athena_playing: Arc<AtomicBool>,
}

impl PlaybackState {
    fn new(source_rate_hz: u32, output_rate_hz: u32, athena_playing: Arc<AtomicBool>) -> Self {
        Self {
            source_rate_hz: source_rate_hz as f64,
            output_rate_hz: output_rate_hz as f64,
            source_pos: 0.0,
            queue: VecDeque::new(),
            max_samples: source_rate_hz as usize * MAX_PLAYBACK_SECONDS,
            turn_complete_pending: false,
            athena_playing,
        }
    }

    fn push_pcm_bytes(&mut self, bytes: &[u8]) {
        for chunk in bytes.chunks_exact(2) {
            let sample = i16::from_le_bytes([chunk[0], chunk[1]]);
            self.queue.push_back(i16_to_f32(sample));
        }

        if self.queue.len() > self.max_samples {
            let overflow = self.queue.len() - self.max_samples;
            for _ in 0..overflow {
                let _ = self.queue.pop_front();
            }
            self.source_pos = self.source_pos.min(1.0);
        }
    }

    fn signal_turn_complete(&mut self) {
        if self.queue.is_empty() {
            // Queue already drained — flip immediately.
            self.athena_playing.store(false, Ordering::Relaxed);
        } else {
            // Defer until the output callback drains the queue.
            self.turn_complete_pending = true;
        }
    }

    fn next_sample(&mut self) -> f32 {
        if self.queue.len() < 2 {
            if !self.queue.is_empty() {
                self.queue.clear();
                self.source_pos = 0.0;
            }
            // Queue just drained — if turn_complete was pending, signal the VAD.
            if self.turn_complete_pending {
                self.turn_complete_pending = false;
                self.athena_playing.store(false, Ordering::Relaxed);
            }
            return 0.0;
        }

        let index = self.source_pos.floor() as usize;
        if index + 1 >= self.queue.len() {
            return 0.0;
        }

        let first = *self.queue.get(index).unwrap_or(&0.0);
        let second = *self.queue.get(index + 1).unwrap_or(&first);
        let alpha = (self.source_pos - index as f64) as f32;
        let out = first + (second - first) * alpha;

        let step = self.source_rate_hz / self.output_rate_hz;
        self.source_pos += step;

        let consumed = self.source_pos.floor() as usize;
        if consumed > 0 {
            let drop_count = consumed.min(self.queue.len());
            for _ in 0..drop_count {
                let _ = self.queue.pop_front();
            }
            self.source_pos -= drop_count as f64;
        }

        out
    }

    fn clear(&mut self) {
        self.queue.clear();
        self.source_pos = 0.0;
        self.turn_complete_pending = false;
    }
}

struct InputResampler {
    step: f64,
    next_output_t: f64,
    current_index: f64,
    previous: f32,
    initialized: bool,
}

impl InputResampler {
    fn new(input_rate_hz: u32) -> Self {
        Self {
            step: input_rate_hz as f64 / TARGET_INPUT_RATE_HZ as f64,
            next_output_t: 0.0,
            current_index: 0.0,
            previous: 0.0,
            initialized: false,
        }
    }

    fn push_sample(&mut self, sample: f32, out: &mut Vec<i16>) {
        if !self.initialized {
            self.previous = sample;
            self.initialized = true;
            return;
        }

        self.current_index += 1.0;
        let segment_start = self.current_index - 1.0;

        while self.next_output_t <= self.current_index {
            let alpha = (self.next_output_t - segment_start).clamp(0.0, 1.0) as f32;
            let interpolated = self.previous + (sample - self.previous) * alpha;
            out.push(f32_to_i16(interpolated));
            self.next_output_t += self.step;
        }

        self.previous = sample;
    }
}

fn build_input_stream_f32(
    device: &cpal::Device,
    config: &StreamConfig,
    talk_enabled: Arc<AtomicBool>,
    transport: WsTransport,
    athena_playing: Arc<AtomicBool>,
    user_speaking: Arc<AtomicBool>,
) -> Result<Stream, cpal::BuildStreamError> {
    build_input_stream(device, config, talk_enabled, transport, athena_playing, user_speaking, |sample| sample)
}

fn build_input_stream_i16(
    device: &cpal::Device,
    config: &StreamConfig,
    talk_enabled: Arc<AtomicBool>,
    transport: WsTransport,
    athena_playing: Arc<AtomicBool>,
    user_speaking: Arc<AtomicBool>,
) -> Result<Stream, cpal::BuildStreamError> {
    build_input_stream(device, config, talk_enabled, transport, athena_playing, user_speaking, i16_to_f32)
}

fn build_input_stream_u16(
    device: &cpal::Device,
    config: &StreamConfig,
    talk_enabled: Arc<AtomicBool>,
    transport: WsTransport,
    athena_playing: Arc<AtomicBool>,
    user_speaking: Arc<AtomicBool>,
) -> Result<Stream, cpal::BuildStreamError> {
    build_input_stream(device, config, talk_enabled, transport, athena_playing, user_speaking, u16_to_f32)
}

fn build_input_stream<S, F>(
    device: &cpal::Device,
    config: &StreamConfig,
    talk_enabled: Arc<AtomicBool>,
    transport: WsTransport,
    athena_playing: Arc<AtomicBool>,
    user_speaking: Arc<AtomicBool>,
    convert: F,
) -> Result<Stream, cpal::BuildStreamError>
where
    S: Copy + cpal::SizedSample + Send + 'static,
    F: Fn(S) -> f32 + Send + 'static,
{
    let channels = config.channels as usize;
    let input_rate_hz = config.sample_rate.0;
    let mut resampler = InputResampler::new(input_rate_hz);
    let mut out_i16 = Vec::<i16>::with_capacity(4096);
    let mut out_bytes = Vec::<u8>::with_capacity(8192);
    // Client-side VAD state
    let mut in_speech = false;
    let mut hangover_remaining: u32 = 0;
    let mut barge_in_onset: u32 = 0;
    let mut playback_cooldown: u32 = 0;
    let mut prev_athena_playing = false;
    // True when the current speech turn was initiated as a barge-in (while Athena
    // was playing).  Lets us gate audio correctly: we suppress audio during
    // Athena's playback to prevent echo from reaching the server, but allow it
    // through for confirmed barge-ins where the user actually wants to interrupt.
    let mut is_barge_in = false;

    device.build_input_stream(
        config,
        move |data: &[S], _| {
            if !talk_enabled.load(Ordering::Relaxed) {
                return;
            }

            out_i16.clear();
            let mut energy_sum = 0.0f64;
            let mut sample_count = 0u32;

            for frame in data.chunks(channels) {
                let mut mono = 0.0f32;
                for sample in frame {
                    mono += convert(*sample);
                }
                mono /= channels as f32;
                energy_sum += (mono as f64) * (mono as f64);
                sample_count += 1;
                resampler.push_sample(mono, &mut out_i16);
            }

            if sample_count == 0 || out_i16.is_empty() {
                return;
            }

            let rms = (energy_sum / sample_count as f64).sqrt() as f32;
            let athena_is_playing = athena_playing.load(Ordering::Relaxed);

            // When playback ends, arm the cooldown and reset onset counter.
            if prev_athena_playing && !athena_is_playing {
                playback_cooldown = PLAYBACK_COOLDOWN_CALLBACKS;
                barge_in_onset = 0;
            }
            prev_athena_playing = athena_is_playing;

            if playback_cooldown > 0 && !athena_is_playing {
                playback_cooldown -= 1;
            }

            // Threshold and onset scale with context:
            //   - During active playback:       BARGE_IN (above echo, requires intention)
            //   - During post-playback cooldown: POST_PLAYBACK (moderate, guards reverb)
            //   - Otherwise:                     SPEECH (normal sensitivity)
            let (threshold, required_onset) = if athena_is_playing {
                (BARGE_IN_THRESHOLD, BARGE_IN_ONSET_CALLBACKS)
            } else if playback_cooldown > 0 {
                (POST_PLAYBACK_THRESHOLD, POST_PLAYBACK_ONSET_CALLBACKS)
            } else {
                (SPEECH_THRESHOLD, SPEECH_ONSET_CALLBACKS)
            };

            if rms >= threshold {
                if !in_speech {
                    barge_in_onset += 1;
                    if barge_in_onset >= required_onset {
                        in_speech = true;
                        is_barge_in = athena_is_playing;
                        barge_in_onset = 0;
                        user_speaking.store(true, Ordering::Relaxed);
                        transport.send_activity_start();
                    }
                }
                hangover_remaining = VAD_HANGOVER_CALLBACKS;
            } else {
                barge_in_onset = 0;
                if in_speech {
                    if hangover_remaining > 0 {
                        hangover_remaining -= 1;
                    } else {
                        in_speech = false;
                        is_barge_in = false;
                        user_speaking.store(false, Ordering::Relaxed);
                        transport.send_activity_end();
                    }
                }
            }

            out_bytes.clear();
            out_bytes.reserve(out_i16.len() * 2);
            for sample in &out_i16 {
                out_bytes.extend_from_slice(&sample.to_le_bytes());
            }

            // Always stream audio when Athena is not playing so the server-side
            // VAD (re-enabled) hears both speech AND silence for accurate turn
            // detection.  During Athena playback, only forward confirmed barge-ins
            // to prevent mic echo from triggering false VAD detections.
            if !athena_is_playing || is_barge_in {
                transport.send_audio(out_bytes.clone());
            }
        },
        move |err| {
            eprintln!("input stream error: {err}");
        },
        None,
    )
}

fn build_output_stream_f32(
    device: &cpal::Device,
    config: &StreamConfig,
    playback: AudioPlayback,
) -> Result<Stream, cpal::BuildStreamError> {
    build_output_stream(device, config, playback, |sample| sample)
}

fn build_output_stream_i16(
    device: &cpal::Device,
    config: &StreamConfig,
    playback: AudioPlayback,
) -> Result<Stream, cpal::BuildStreamError> {
    build_output_stream(device, config, playback, f32_to_i16)
}

fn build_output_stream_u16(
    device: &cpal::Device,
    config: &StreamConfig,
    playback: AudioPlayback,
) -> Result<Stream, cpal::BuildStreamError> {
    build_output_stream(device, config, playback, f32_to_u16)
}

fn build_output_stream<S, F>(
    device: &cpal::Device,
    config: &StreamConfig,
    playback: AudioPlayback,
    convert: F,
) -> Result<Stream, cpal::BuildStreamError>
where
    S: Copy + cpal::SizedSample + Send + 'static,
    F: Fn(f32) -> S + Send + 'static,
{
    let channels = config.channels as usize;

    device.build_output_stream(
        config,
        move |data: &mut [S], _| {
            let mut maybe_inner = playback.inner.lock();
            if let Ok(inner) = maybe_inner.as_mut() {
                for frame in data.chunks_mut(channels) {
                    let value = inner.next_sample();
                    let output = convert(value);
                    for sample in frame {
                        *sample = output;
                    }
                }
            } else {
                for sample in data.iter_mut() {
                    *sample = convert(0.0);
                }
            }
        },
        move |err| {
            eprintln!("output stream error: {err}");
        },
        None,
    )
}

fn i16_to_f32(sample: i16) -> f32 {
    sample as f32 / i16::MAX as f32
}

fn u16_to_f32(sample: u16) -> f32 {
    (sample as f32 / u16::MAX as f32) * 2.0 - 1.0
}

fn f32_to_i16(sample: f32) -> i16 {
    let clamped = sample.clamp(-1.0, 1.0);
    (clamped * i16::MAX as f32) as i16
}

fn f32_to_u16(sample: f32) -> u16 {
    let clamped = sample.clamp(-1.0, 1.0);
    ((clamped + 1.0) * 0.5 * u16::MAX as f32) as u16
}
