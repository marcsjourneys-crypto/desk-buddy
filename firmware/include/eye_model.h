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
  // Rotates both lids about the eye's centre, in radians; positive droops the outer
  // corner. The drawing mirrors it for the second eye.
  float tilt = 0;
  // Crescent looks (docs/looking.png). fill: 0 = outline ring, 1 = solid disc. cut: radius
  // of a black disc cut out of the eye, as a fraction of radius (0 = none); cut_x/cut_y:
  // where that disc sits, as fractions of radius from the eye's centre. Not mirrored.
  float fill = 0;
  float cut = 0;
  float cut_x = 0;
  float cut_y = 0;
};

inline Params lerp(const Params& a, const Params& b, float t) {
  Params p;
  p.upper = ease::lerp(a.upper, b.upper, t);
  p.lower = ease::lerp(a.lower, b.lower, t);
  p.radius = ease::lerp(a.radius, b.radius, t);
  p.thickness = ease::lerp(a.thickness, b.thickness, t);
  p.gaze_x = ease::lerp(a.gaze_x, b.gaze_x, t);
  p.gaze_y = ease::lerp(a.gaze_y, b.gaze_y, t);
  p.tilt = ease::lerp(a.tilt, b.tilt, t);
  p.fill = ease::lerp(a.fill, b.fill, t);
  p.cut = ease::lerp(a.cut, b.cut, t);
  p.cut_x = ease::lerp(a.cut_x, b.cut_x, t);
  p.cut_y = ease::lerp(a.cut_y, b.cut_y, t);
  return p;
}

// Close enough that redrawing would look the same.
inline bool same(const Params& a, const Params& b) {
  auto near = [](float x, float y) { return fabsf(x - y) < 0.01f; };
  return near(a.upper, b.upper) && near(a.lower, b.lower) && near(a.radius, b.radius) &&
         near(a.thickness, b.thickness) && near(a.gaze_x, b.gaze_x) &&
         near(a.gaze_y, b.gaze_y) && near(a.tilt, b.tilt) && near(a.fill, b.fill) &&
         near(a.cut, b.cut) && near(a.cut_x, b.cut_x) && near(a.cut_y, b.cut_y);
}

// The thinking look drifts its gaze left and right by this much (px), once per period.
constexpr float kThinkingDrift = 14;
constexpr uint32_t kThinkingPeriodMs = 3000;

// The same eye with both lids shut into a gentle downward curve (the bottom of a blink).
inline Params closedLike(Params p) {
  p.upper = 0.3f;
  p.lower = 0.3f;
  p.fill = 0;  // a crescent blinks shut like a ring
  p.cut = 0;
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
inline Params surprised() {
  Params p;
  p.radius = 84;
  p.thickness = 12;
  return p;
}
// Slightly smaller eyes so the gaze has room to move inside the 200 px sprite.
inline Params look(float x, float y) {
  Params p;
  p.radius = 62;
  p.gaze_x = x;
  p.gaze_y = y;
  return p;
}
// A filled crescent looking toward (x, y) px (docs/looking.png): a solid disc with a black
// disc cut out at (cut_x, cut_y) radii from its centre.
inline Params crescent(float x, float y, float cut_x, float cut_y) {
  Params p = look(x, y);
  p.fill = 1;
  p.cut = 0.85f;
  p.cut_x = cut_x;
  p.cut_y = cut_y;
  return p;
}
// Crescents lean toward the gaze with the hollow facing the middle (left/right), or sit
// high/low with the hollow facing up (up/down), as in docs/looking.png.
inline Params lookLeft() { return crescent(-28, 0, 0.45f, -0.1f); }
inline Params lookRight() { return crescent(28, 0, -0.45f, -0.1f); }
inline Params lookUp() { return crescent(0, -28, 0, -0.45f); }
inline Params lookDown() { return crescent(0, 28, 0, -0.45f); }
// Small rings: the start of "look around" (docs/look around.png).
inline Params small() {
  Params p;
  p.radius = 38;
  p.thickness = 12;
  return p;
}
inline Params closedLine() {
  Params p;
  p.upper = 0.08f;
  p.lower = 0.08f;
  return p;
}
inline Params skeptical() {  // flattened top lid
  Params p;
  p.upper = -0.15f;
  p.lower = 0.85f;
  p.gaze_y = -10;
  return p;
}
inline Params sad() {  // a narrowed eye with drooping outer corners
  Params p;
  p.upper = -0.55f;
  p.lower = 0.55f;
  p.tilt = 0.35f;
  p.gaze_y = 12;
  return p;
}
inline Params listening() { return surprised(); }  // wide and attentive
// Looking up; src/eyes.cpp adds the slow left-right drift (kThinkingDrift).
inline Params thinking() { return look(0, -24); }
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
  // Once the blend is over, hold the target for good. Without this, the last transition
  // would replay when millis() comes round again (~49.7 days later).
  void settle(uint32_t now) {
    if (duration_ > 0 && static_cast<uint32_t>(now - start_) >= duration_) {
      from_ = to_;
      duration_ = 0;
    }
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

  void schedule(uint32_t now, uint32_t delay) {
    next_ = now + delay;
    armed_ = true;
  }
  void blinkNow(uint32_t now) {
    next_ = now;
    armed_ = true;
  }

  float amount(uint32_t now) const {
    if (!armed_) return 0;
    const int32_t dt = static_cast<int32_t>(now - next_);
    if (dt < 0 || dt >= static_cast<int32_t>(kMs)) return 0;
    const float t = static_cast<float>(dt) / kMs;
    return t < 0.5f ? ease::inOutCubic(t * 2) : ease::inOutCubic((1 - t) * 2);
  }
  // True once the current blink is over (time to schedule the next one).
  bool finished(uint32_t now) const {
    if (!armed_) return true;
    return static_cast<int32_t>(now - next_) >= static_cast<int32_t>(kMs);
  }

 private:
  uint32_t next_ = 0;
  bool armed_ = false;  // no blink until schedule() or blinkNow()
};

}  // namespace eye
