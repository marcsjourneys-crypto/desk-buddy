#pragma once
// Wake mode's decisions (Phase 1): when the robot listens for "Hey CARL", who gets the mic,
// when a detection counts, and when a wake the server didn't answer is given up. Plus the
// threshold's parsing and range. Pure: no Arduino. Times are millis() and survive the wrap.
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

namespace wake {

constexpr uint32_t kStateHoldOffMs = 2000;   // detections ignored after a state change
constexpr uint32_t kAnswerTimeoutMs = 2000;  // wake sent, no `state listening`: give up
constexpr float kMinThreshold = 0.5f;
constexpr float kMaxThreshold = 0.99f;

// A threshold the detector accepts (NaN fails). The small slack lets 0.99f and 0.5f through.
inline bool validThreshold(float t) {
  return t >= kMinThreshold - 1e-4f && t <= kMaxThreshold + 1e-4f;
}

// "0.97" (or ".97") -> 97 hundredths. The whole string must be the number, 0.50-0.99;
// more decimals are rounded to hundredths.
inline bool parseThreshold(const char* s, uint8_t* hundredths) {
  if (s == nullptr || *s == '\0') return false;
  for (const char* c = s; *c; c++) {  // digits and one point only: no signs, exponents, "nan"
    if (!((*c >= '0' && *c <= '9') || *c == '.')) return false;
  }
  if (strchr(s, '.') != strrchr(s, '.')) return false;
  char* end = nullptr;
  const float v = strtof(s, &end);
  if (end == s || *end != '\0' || !validThreshold(v)) return false;
  long h = lrintf(v * 100);
  if (h < 50) h = 50;
  if (h > 99) h = 99;
  *hundredths = static_cast<uint8_t>(h);
  return true;
}

// The server's state as far as wake mode cares. None: linked, no state yet.
enum class Server : uint8_t { None, Idle, Listening, Other };

inline Server serverFromName(const char* name) {
  if (name == nullptr) return Server::None;
  if (!strcmp(name, "idle")) return Server::Idle;
  if (!strcmp(name, "listening")) return Server::Listening;
  return Server::Other;
}

// Who the mic's frames go to (audio::MicMode mirrors this):
// Off: nobody reads the mic. Wake: the wake engine, and the pre-roll as history.
// Hold: a wake was sent; the pre-roll keeps everything from the detection on.
// Stream: the link (the pre-roll first, if a wake started it).
enum class Mic : uint8_t { Off, Wake, Hold, Stream };

// What a `state` message meant for a wake waiting for its answer.
enum class Answer : uint8_t { None, Listening, Other };

class Gate {
 public:
  // The wake word is on and the engine is ready.
  void setEnabled(bool on) { enabled_ = on; }

  // The link came up or went down: no state yet, nothing pending; a fresh hold-off.
  void onLink(bool up, uint32_t now) {
    linked_ = up;
    server_ = Server::None;
    pending_ = false;
    changed(now);
  }

  // Any `state` message (repeats count too): a hold-off, and the end of a pending wake.
  Answer onState(Server s, uint32_t now) {
    Answer a = Answer::None;
    if (pending_) a = s == Server::Listening ? Answer::Listening : Answer::Other;
    pending_ = false;
    server_ = s;
    changed(now);
    return a;
  }

  Mic mic() const {
    if (!linked_) return Mic::Off;
    if (server_ == Server::Listening) return Mic::Stream;  // the server's state rules
    if (pending_) return Mic::Hold;
    if (server_ == Server::Idle && enabled_) return Mic::Wake;
    return Mic::Off;
  }

  bool holdingOff(uint32_t now) const {
    return static_cast<int32_t>(now - changedAt_) < static_cast<int32_t>(kStateHoldOffMs);
  }

  // A detection now would be sent.
  bool accepts(uint32_t now) const { return mic() == Mic::Wake && !holdingOff(now); }

  // The engine fired. True: send `wake` (the mic goes to Hold until the server answers).
  bool onDetection(uint32_t now) {
    if (!accepts(now)) return false;
    pending_ = true;
    pendingAt_ = now;
    return true;
  }

  // True once when a pending wake got no answer in time; wake listening resumes after a
  // fresh hold-off.
  bool expire(uint32_t now) {
    if (!pending_ ||
        static_cast<int32_t>(now - pendingAt_) < static_cast<int32_t>(kAnswerTimeoutMs)) {
      return false;
    }
    pending_ = false;
    changed(now);
    return true;
  }

  bool pending() const { return pending_; }
  bool enabled() const { return enabled_; }
  Server server() const { return server_; }

 private:
  void changed(uint32_t now) { changedAt_ = now; }

  bool enabled_ = false;
  bool linked_ = false;
  bool pending_ = false;
  Server server_ = Server::None;
  uint32_t changedAt_ = 0;
  uint32_t pendingAt_ = 0;
};

}  // namespace wake
