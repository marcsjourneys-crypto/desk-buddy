#pragma once
// The robot link's retry rules, kept pure so they can be tested. Pure: no Arduino.
#include <stdint.h>

// True once `ms` have passed since `since`. Wrap-safe, and false if `since` is a moment
// later than `now` (e.g. a timer set by a callback after `now` was read).
inline bool elapsedMs(uint32_t now, uint32_t since, uint32_t ms) {
  return static_cast<int32_t>(now - since) >= static_cast<int32_t>(ms);
}

// The library doesn't report the server's close code, so the timing of closes stands in
// for it:
// - a close within kEarlyCloseMs of sending hello means the server refused it (bad token,
//   blocked IP, too many half-open connections); kMaxEarlyCloses in a row -> Refused;
// - a link that drops within kQuickDropMs of welcome kMaxQuickDrops times in a row (e.g.
//   two robots replacing each other) stops resetting the back-off, so retries slow down.
struct LinkPolicy {
  static constexpr uint32_t kEarlyCloseMs = 1000;
  static constexpr int kMaxEarlyCloses = 3;
  static constexpr uint32_t kQuickDropMs = 2000;
  static constexpr int kMaxQuickDrops = 3;

  int earlyCloses = 0;
  int quickDrops = 0;

  // A close before welcome, `msSinceHello` after hello went out. True: stop retrying.
  bool closedBeforeWelcome(uint32_t msSinceHello) {
    if (msSinceHello >= kEarlyCloseMs) {
      earlyCloses = 0;  // not a refusal: an ordinary failure
      return false;
    }
    return ++earlyCloses >= kMaxEarlyCloses;
  }
  // Welcome arrived. True: reset the back-off.
  bool welcomed() {
    earlyCloses = 0;
    return quickDrops < kMaxQuickDrops;
  }
  // A link that had been welcomed dropped, `msLinked` after welcome.
  void linkDropped(uint32_t msLinked) { quickDrops = msLinked < kQuickDropMs ? quickDrops + 1 : 0; }
  // The periodic retry after Refused: one more early close refuses again.
  void retryAfterRefusal() { earlyCloses = kMaxEarlyCloses - 1; }
  void reset() { earlyCloses = quickDrops = 0; }
};
