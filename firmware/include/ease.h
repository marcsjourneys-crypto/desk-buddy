#pragma once
// Easing and interpolation. Pure maths: no Arduino, unit tested on the board.
#include <math.h>
#include <stdint.h>

namespace ease {

inline float clamp01(float t) { return t < 0 ? 0 : (t > 1 ? 1 : t); }

inline float lerp(float a, float b, float t) { return a + (b - a) * t; }

// Slow start, slow end. 0 -> 0, 0.5 -> 0.5, 1 -> 1.
inline float inOutCubic(float t) {
  t = clamp01(t);
  return t < 0.5f ? 4 * t * t * t : 1 - powf(-2 * t + 2, 3) / 2;
}

// 0..1 progress of an animation that started at `start` and lasts `duration` ms.
// Unsigned subtraction keeps it right when millis() wraps.
inline float progress(uint32_t now, uint32_t start, uint32_t duration) {
  if (duration == 0) return 1;
  return clamp01(static_cast<float>(static_cast<uint32_t>(now - start)) / duration);
}

}  // namespace ease
