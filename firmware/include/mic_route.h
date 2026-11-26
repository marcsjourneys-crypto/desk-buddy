#pragma once
// Where the mic reader's frames go (audio.cpp), as pure rules so they can be tested:
// per frame, by mode and backlog (route); per mode change, what to reset (onModeChange);
// and when the link may take frames (mayPop). See audio.h for the modes. Pure: no Arduino.
#include <stddef.h>
#include <stdint.h>

namespace microute {

enum class Mode : uint8_t { Off, Wake, Hold, Stream };

// The pre-roll keeps this much from before the detecting step (300 ms), so a question said
// in one breath ("Hey CARL, what's the time?") keeps its first word. The server may get the
// end of the phrase in it.
constexpr uint32_t kWakeLeadFrames = 15;

// The first pre-roll frame to keep after a detection while frame `detectedSeq` was being
// processed. Unsigned, so wrap-safe; FrameRing::keepFrom ignores a frame older than its oldest.
inline uint32_t keepFromSeq(uint32_t detectedSeq) { return detectedSeq + 1 - kWakeLeadFrames; }

enum class Dest : uint8_t {
  Drop,     // nowhere (Off, or a mode that needs the pre-roll when there is none)
  History,  // the pre-roll; overwriting its oldest frame is normal (Wake)
  Keep,     // the pre-roll; overwriting its oldest frame loses audio to be sent (Hold, backlog)
  Queue,    // the link's queue
};

// One frame. `backlog` (in/out): Stream after Hold, the kept pre-roll goes out first; it
// ends for good once the ring has drained.
inline Dest route(Mode m, bool haveRing, size_t ringCount, bool& backlog) {
  switch (m) {
    case Mode::Stream:
      if (backlog && haveRing && ringCount > 0) return Dest::Keep;  // behind the kept audio
      backlog = false;
      return Dest::Queue;
    case Mode::Wake:
      return haveRing ? Dest::History : Dest::Drop;
    case Mode::Hold:
      return haveRing ? Dest::Keep : Dest::Drop;
    default:
      return Dest::Drop;
  }
}

// The wake engine gets frames only in Wake mode.
inline bool feedsEngine(Mode m) { return m == Mode::Wake; }

// The link takes frames (pre-roll backlog first, then the queue) only while streaming, so
// nothing goes out once `listening` has ended.
inline bool mayPop(Mode m) { return m == Mode::Stream; }

struct Change {
  bool backlog;     // the new backlog flag
  bool clearRing;   // forget the pre-roll
  bool resetQueue;  // empty the link's queue
};

// A mode change `was` -> `now` (only called when they differ). A stream that follows a held
// wake sends the kept pre-roll first; any other stream starts now. Entering or leaving
// Stream empties the queue, so no stale or late frame goes out.
inline Change onModeChange(Mode was, Mode now) {
  Change c;
  c.backlog = now == Mode::Stream && was == Mode::Hold;
  c.clearRing = now == Mode::Stream && !c.backlog;
  c.resetQueue = now == Mode::Stream || was == Mode::Stream;
  return c;
}

}  // namespace microute
