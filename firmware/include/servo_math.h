#pragma once
// Hobby-servo maths (MG90S): angle -> pulse -> LEDC duty, and eased moves. Pure logic.
#include <math.h>
#include <stdint.h>

#include "ease.h"

namespace servo {

constexpr uint32_t kFreqHz = 50;
constexpr uint8_t kBits = 14;
constexpr uint32_t kPeriodUs = 1000000 / kFreqHz;  // 20 ms

inline float clampAngle(float deg, float limit) {
  return deg > limit ? limit : (deg < -limit ? -limit : deg);
}

// -90..+90 degrees -> 500..2500 us (centre 1500).
inline uint32_t angleToPulseUs(float deg) {
  deg = clampAngle(deg, 90);
  return static_cast<uint32_t>(1500 + deg * 1000.0f / 90.0f + 0.5f);
}

inline uint32_t pulseToDuty(uint32_t us) { return us * ((1u << kBits) - 1) / kPeriodUs; }

// An eased move at a capped speed.
struct Motion {
  float from = 0;
  float to = 0;
  uint32_t start = 0;
  uint32_t duration = 0;

  void moveTo(float current, float target, uint32_t now, float degPerSec) {
    from = current;
    to = target;
    start = now;
    // A zero or negative speed means "go now" rather than dividing by it.
    duration = degPerSec > 0
                   ? static_cast<uint32_t>(fabsf(target - current) / degPerSec * 1000.0f + 0.5f)
                   : 0;
  }
  float at(uint32_t now) const {
    return ease::lerp(from, to, ease::inOutCubic(ease::progress(now, start, duration)));
  }
  bool done(uint32_t now) const { return static_cast<uint32_t>(now - start) >= duration; }
};

}  // namespace servo
