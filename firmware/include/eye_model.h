#pragma once
// One eye as a few numbers, so looks can blend smoothly and M9's browser editor can store
// them. Pure logic: no drawing here (that's src/eyes.cpp).
#include <math.h>
#include <stdint.h>

#include "ease.h"

namespace eye {

// Each lid is a half-ellipse from the left corner to the right corner:
// -1 = bulges fully up, 0 = flat, +1 = bulges fully down.
// upper -1 / lower +1 is an open ring; equal values draw one curve (happy arc, sleepy
// curve, closed line).
struct Params {
  float upper = -1;
  float lower = 1;
  float radius = 70;     // px
  float thickness = 14;  // stroke width, px
  float gaze_x = 0;      // px offset from the centre
  float gaze_y = 0;
};

inline Params lerp(const Params& a, const Params& b, float t) {
  Params p;
  p.upper = ease::lerp(a.upper, b.upper, t);
  p.lower = ease::lerp(a.lower, b.lower, t);
  p.radius = ease::lerp(a.radius, b.radius, t);
  p.thickness = ease::lerp(a.thickness, b.thickness, t);
  p.gaze_x = ease::lerp(a.gaze_x, b.gaze_x, t);
  p.gaze_y = ease::lerp(a.gaze_y, b.gaze_y, t);
  return p;
}

// Close enough that redrawing would look the same.
inline bool same(const Params& a, const Params& b) {
  auto near = [](float x, float y) { return fabsf(x - y) < 0.01f; };
  return near(a.upper, b.upper) && near(a.lower, b.lower) && near(a.radius, b.radius) &&
         near(a.thickness, b.thickness) && near(a.gaze_x, b.gaze_x) &&
         near(a.gaze_y, b.gaze_y);
}

// The same eye with both lids shut into a gentle downward curve (the bottom of a blink).
inline Params closedLike(Params p) {
  p.upper = 0.3f;
  p.lower = 0.3f;
  return p;
}

namespace presets {
inline Params idle() { return Params{}; }
inline Params happy() {
  Params p;
  p.upper = -0.6f;
  p.lower = -0.6f;
  p.gaze_y = 20;  // the arc sits above the centre; nudge it down
  return p;
}
inline Params sleep() {
  Params p;
  p.upper = 0.35f;
  p.lower = 0.35f;
  return p;
}
}  // namespace presets

// Blends from whatever is showing to a target over `duration` ms, eased.
class Tween {
 public:
  void set(const Params& p) {
    from_ = to_ = p;
    duration_ = 0;
  }
  void to(const Params& target, uint32_t now, uint32_t duration) {
    from_ = at(now);
    to_ = target;
    start_ = now;
    duration_ = duration;
  }
  Params at(uint32_t now) const {
    return lerp(from_, to_, ease::inOutCubic(ease::progress(now, start_, duration_)));
  }
  bool moving(uint32_t now) const {
    return duration_ > 0 && static_cast<uint32_t>(now - start_) < duration_;
  }

 private:
  Params from_, to_;
  uint32_t start_ = 0;
  uint32_t duration_ = 0;
};

// One blink at a time: amount() is 0 when open and rises to 1 (closed) and back over kMs.
class Blinker {
 public:
  static constexpr uint32_t kMs = 180;

  void schedule(uint32_t now, uint32_t delay) { next_ = now + delay; }
  void blinkNow(uint32_t now) { next_ = now; }

  float amount(uint32_t now) const {
    const int32_t dt = static_cast<int32_t>(now - next_);
    if (dt < 0 || dt >= static_cast<int32_t>(kMs)) return 0;
    const float t = static_cast<float>(dt) / kMs;
    return t < 0.5f ? ease::inOutCubic(t * 2) : ease::inOutCubic((1 - t) * 2);
  }
  // True once the current blink is over (time to schedule the next one).
  bool finished(uint32_t now) const {
    return static_cast<int32_t>(now - next_) >= static_cast<int32_t>(kMs);
  }

 private:
  uint32_t next_ = 0;
};

}  // namespace eye
