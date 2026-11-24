#pragma once
// Which eye look (an eyes::show() name) goes with the server's state and Buddy's emotion.
// The tables are in docs/plans/2026-11-24-m6-robot-joins-design.md. Pure: no Arduino.
#include <string.h>

inline const char* lookForEmotion(const char* emotion) {
  static const char* const kTable[][2] = {
      {"neutral", "idle"},
      {"happy", "happy"},
      {"sad", "sad"},
      {"surprised", "surprised"},
      {"sleepy", "sleep"},
      {"thinking", "skeptical"},
      {"curious", "up"},
  };
  if (emotion == nullptr) return "idle";
  for (const auto& row : kTable) {
    if (!strcmp(emotion, row[0])) return row[1];
  }
  return "idle";
}

// Anything that isn't a live state (not connected, sleeping, unknown) sleeps.
inline const char* lookForState(const char* state, const char* emotion) {
  if (state == nullptr) return "sleep";
  if (!strcmp(state, "idle")) return "idle";
  if (!strcmp(state, "listening")) return "listening";
  if (!strcmp(state, "thinking")) return "thinking";
  if (!strcmp(state, "speaking")) return lookForEmotion(emotion);
  return "sleep";
}
