#pragma once
// Wake word decision logic: the model's per-step probabilities are averaged over a short
// sliding window, compared with a cutoff, and a cooldown stops one "Hey CARL" (or a state
// change) from firing twice. Also the int8 quantisation of the microfrontend's features.
// Pure: no Arduino, no TFLM.
#include <math.h>
#include <stddef.h>
#include <stdint.h>

namespace wake {

constexpr size_t kMaxWindow = 32;      // longest sliding window we accept
constexpr uint32_t kCooldownMs = 2000;  // after a detection or a state change

// microWakeWord trains on the microfrontend's uint16 output (out_scale 1) multiplied by
// 0.0390625 (= 1/25.6), so that is the real value the model's input quantisation applies to.
constexpr float kFeatureToReal = 0.0390625f;

// One uint16 feature to the model's int8 input: real = feature / 25.6, then
// q = round(real / scale) + zero_point, saturated. Scale and zero point come from the
// model's input tensor; a scale <= 0 (a broken model) gives zero_point.
inline int8_t saturateInt8(int32_t q) {
  return static_cast<int8_t>(q < -128 ? -128 : (q > 127 ? 127 : q));
}

inline int8_t quantizeFeature(uint16_t feature, float scale, int32_t zeroPoint) {
  if (!(scale > 0)) return saturateInt8(zeroPoint);
  return saturateInt8(static_cast<int32_t>(lrintf(feature * kFeatureToReal / scale)) + zeroPoint);
}

// A quantised output value to a probability, clamped to 0..1.
inline float dequantize(int32_t q, float scale, int32_t zeroPoint) {
  const float p = (q - zeroPoint) * scale;
  return p < 0 ? 0 : (p > 1 ? 1 : p);
}

// The mean of the last `size` probabilities. Until `size` have arrived, the missing ones
// count as 0, so a fresh window can't trigger on its first loud step.
class SlidingWindow {
 public:
  explicit SlidingWindow(size_t size = 5) { resize(size); }

  void resize(size_t size) {
    size_ = size < 1 ? 1 : (size > kMaxWindow ? kMaxWindow : size);
    reset();
  }

  void reset() {
    for (size_t i = 0; i < kMaxWindow; i++) values_[i] = 0;
    next_ = 0;
  }

  // Adds one probability (clamped to 0..1) and returns the new mean.
  float push(float p) {
    values_[next_] = p < 0 ? 0 : (p > 1 ? 1 : p);
    next_ = (next_ + 1) % size_;
    return mean();
  }

  // Summed afresh each time (at most 32 adds), so float error never builds up.
  float mean() const {
    float sum = 0;
    for (size_t i = 0; i < size_; i++) sum += values_[i];
    return sum / size_;
  }

  size_t size() const { return size_; }

 private:
  float values_[kMaxWindow];
  size_t size_ = 1;
  size_t next_ = 0;
};

// Window + cutoff + cooldown. Fires when the window's mean reaches the cutoff, outside a
// cooldown. A detection clears the window and starts a cooldown; so does holdOff(), which
// the caller uses on a state change. Times are millis() and survive the wrap.
class Detector {
 public:
  Detector(float cutoff, size_t window, uint32_t cooldownMs = kCooldownMs)
      : window_(window), cutoff_(cutoff), cooldownMs_(cooldownMs) {}

  // One model step. Returns true on a detection. The window keeps filling during a cooldown
  // (so the live score stays honest), it just can't fire.
  bool step(float p, uint32_t now) {
    score_ = window_.push(p);
    if (cooling(now) || score_ < cutoff_) return false;
    detectedScore_ = score_;
    holdOff(now);
    return true;
  }

  // Ignore triggers for the cooldown from `now`, and start the window afresh.
  void holdOff(uint32_t now) {
    window_.reset();
    coolUntil_ = now + cooldownMs_;
    coolingSet_ = true;
  }

  bool cooling(uint32_t now) const {
    return coolingSet_ && static_cast<int32_t>(now - coolUntil_) < 0;
  }

  void setCutoff(float cutoff) { cutoff_ = cutoff; }
  float cutoff() const { return cutoff_; }
  float score() const { return score_; }                  // the window's mean, last step
  float detectedScore() const { return detectedScore_; }  // the mean that last fired
  size_t windowSize() const { return window_.size(); }

 private:
  SlidingWindow window_;
  float cutoff_;
  uint32_t cooldownMs_;
  uint32_t coolUntil_ = 0;
  bool coolingSet_ = false;
  float score_ = 0;
  float detectedScore_ = 0;
};

}  // namespace wake
