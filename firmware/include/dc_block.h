#pragma once
// Removes the mic's DC offset with a one-pole high-pass filter: y = x - x1 + r*y1.
// With r = 0.995 at 16 kHz the corner is about 13 Hz, so speech passes untouched.
// Pure: no Arduino.
#include <math.h>
#include <stddef.h>
#include <stdint.h>

struct DcBlocker {
  float r = 0.995f;
  float x1 = 0, y1 = 0;

  int16_t process(int16_t x) {
    float y = x - x1 + r * y1;
    if (y > 32767.0f) y = 32767.0f;  // saturate, and keep the state saturated too
    if (y < -32768.0f) y = -32768.0f;
    x1 = x;
    y1 = y;
    return static_cast<int16_t>(lrintf(y));
  }

  void processBlock(int16_t* buf, size_t n) {
    for (size_t i = 0; i < n; i++) buf[i] = process(buf[i]);
  }
};
