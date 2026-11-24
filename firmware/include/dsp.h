#pragma once
// Audio maths for 16 kHz 16-bit mono (the protocol's format). Pure: no Arduino.
#include <math.h>
#include <stddef.h>
#include <stdint.h>

namespace dsp {

constexpr int kRate = 16000;
constexpr float kToneAmp = 16000;  // half scale; volume scales it further

// INMP441 sends a 24-bit sample left-justified in a 32-bit slot. Shifting by 16 would be
// unity; 14 adds 12 dB because the INMP441 is quiet. Saturates instead of wrapping.
constexpr int kMicShift = 14;
inline int16_t micTo16(int32_t s) {
  const int32_t v = s >> kMicShift;
  return v > 32767 ? 32767 : (v < -32768 ? -32768 : static_cast<int16_t>(v));
}

// Amplitude (0..32768) to dBFS; silence floors at -96.
inline float toDb(float amp) { return amp < 0.5f ? -96.0f : 20 * log10f(amp / 32768.0f); }

struct Level {
  float rms_db;
  float peak_db;
};

inline Level level(const int16_t* x, size_t n) {
  if (n == 0) return {-96.0f, -96.0f};
  double sum = 0;
  int peak = 0;
  for (size_t i = 0; i < n; i++) {
    const int v = x[i];
    sum += static_cast<double>(v) * v;
    const int a = v < 0 ? -v : v;
    if (a > peak) peak = a;
  }
  return {toDb(sqrtf(static_cast<float>(sum / n))), toDb(static_cast<float>(peak))};
}

// True when every sample is (nearly) the same: no mic on the line.
inline bool isFlat(const int16_t* x, size_t n) {
  int lo = 32767, hi = -32768;
  for (size_t i = 0; i < n; i++) {
    if (x[i] < lo) lo = x[i];
    if (x[i] > hi) hi = x[i];
  }
  return n == 0 || hi - lo < 2;
}

// Linear fade in/out over `fade` samples at each end of a `total`-sample sound.
inline float fadeGain(size_t i, size_t total, size_t fade) {
  if (fade == 0) return 1;
  if (i < fade) return static_cast<float>(i) / fade;
  const size_t left = total - 1 - i;
  if (left < fade) return static_cast<float>(left) / fade;
  return 1;
}

inline int16_t scale(int16_t s, int volume) {
  return static_cast<int16_t>(static_cast<int32_t>(s) * volume / 100);
}

// n samples of a sine; `phase` carries on across blocks.
inline void tone(int16_t* out, size_t n, float hz, float amp, float& phase) {
  const float step = 2 * static_cast<float>(M_PI) * hz / kRate;
  for (size_t i = 0; i < n; i++) {
    out[i] = static_cast<int16_t>(amp * sinf(phase));
    phase += step;
    if (phase > 2 * static_cast<float>(M_PI)) phase -= 2 * static_cast<float>(M_PI);
  }
}

}  // namespace dsp
