#pragma once
// Reconnect delays: 1 s, doubling up to 30 s. Pure: no Arduino.
#include <stdint.h>

class Backoff {
 public:
  static constexpr uint32_t kFirstMs = 1000;
  static constexpr uint32_t kMaxMs = 30000;

  uint32_t next() {
    const uint32_t d = next_;
    next_ = next_ >= kMaxMs / 2 ? kMaxMs : next_ * 2;
    return d;
  }
  void reset() { next_ = kFirstMs; }

 private:
  uint32_t next_ = kFirstMs;
};
