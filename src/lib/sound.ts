// Short, synthesized UI sound cues for dictation state changes — no asset
// files, just plain oscillator tones via the Web Audio API. Mirrors the same
// three moments the Rust side already fires distinct haptic feedback for
// (see fn_key.rs): recording starts, recording stops/transcription begins,
// and a double-tap engages hands-free lock. Haptics only reach machines with
// a Force Touch trackpad; sound reaches everyone, which is exactly why both
// exist rather than just one.

let audioContext: AudioContext | null = null;

function getAudioContext(): AudioContext {
  if (!audioContext) {
    audioContext = new AudioContext();
  }
  return audioContext;
}

/** A single short tone with a quick attack and exponential decay — a "blip", not a sustained note. */
function playTone(frequency: number, durationMs: number, delayMs = 0, volume = 0.12) {
  const ctx = getAudioContext();
  const startTime = ctx.currentTime + delayMs / 1000;
  const stopTime = startTime + durationMs / 1000;

  const oscillator = ctx.createOscillator();
  const gain = ctx.createGain();
  oscillator.type = "sine";
  oscillator.frequency.setValueAtTime(frequency, startTime);

  // Quick linear attack avoids an audible click; exponential decay reads as
  // a natural, soft fade rather than an abrupt cutoff.
  gain.gain.setValueAtTime(0, startTime);
  gain.gain.linearRampToValueAtTime(volume, startTime + 0.008);
  gain.gain.exponentialRampToValueAtTime(0.0001, stopTime);

  oscillator.connect(gain);
  gain.connect(ctx.destination);
  oscillator.start(startTime);
  oscillator.stop(stopTime + 0.02);
}

export function playRecordingStartSound() {
  playTone(660, 90);
}

export function playRecordingStopSound() {
  playTone(420, 110);
}

export function playLockEngagedSound() {
  // Two quick ascending notes — distinct from the single-blip start/stop
  // sounds, since lock-engaged is the one transition with no other physical
  // confirmation (no key held down anymore).
  playTone(660, 70);
  playTone(880, 90, 90);
}
