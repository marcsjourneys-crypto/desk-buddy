#pragma once
// The robot's side of the JSON control messages (docs/protocol.md): parsing what the
// server sends and building hello. Pure apart from ArduinoJson.
#include <ArduinoJson.h>
#include <stddef.h>
#include <string.h>

namespace linkmsg {

enum class Msg { Welcome, State, Emotion, AudioStart, AudioEnd, AudioStop, Unknown, Bad };

struct Parsed {
  Msg type = Msg::Bad;
  char name[16] = {0};  // state or emotion name, truncated to fit
};

inline Parsed parseServerText(const char* text, size_t len) {
  Parsed out;
  JsonDocument doc;
  if (deserializeJson(doc, text, len) != DeserializationError::Ok) return out;
  if (!doc.is<JsonObject>()) return out;
  const char* type = doc["type"].as<const char*>();
  if (type == nullptr) return out;

  if (!strcmp(type, "state") || !strcmp(type, "emotion")) {
    const char* name = doc["name"].as<const char*>();
    if (name == nullptr) return out;  // a bad field: Bad
    strncpy(out.name, name, sizeof(out.name) - 1);
    out.name[sizeof(out.name) - 1] = '\0';
    out.type = type[0] == 's' ? Msg::State : Msg::Emotion;
  } else if (!strcmp(type, "welcome")) {
    out.type = Msg::Welcome;
  } else if (!strcmp(type, "audio_start")) {
    out.type = Msg::AudioStart;
  } else if (!strcmp(type, "audio_end")) {
    out.type = Msg::AudioEnd;
  } else if (!strcmp(type, "audio_stop")) {
    out.type = Msg::AudioStop;
  } else {
    out.type = Msg::Unknown;
  }
  return out;
}

// Writes {"type":"hello","token":...,"fw_version":...} with proper escaping.
// Returns its length, or 0 if it doesn't fit in `cap` (NUL included).
inline size_t buildHello(char* out, size_t cap, const char* token, const char* fw) {
  JsonDocument doc;
  doc["type"] = "hello";
  doc["token"] = token;
  doc["fw_version"] = fw;
  if (cap == 0 || doc.overflowed() || measureJson(doc) >= cap) {
    if (cap) out[0] = '\0';
    return 0;
  }
  return serializeJson(doc, out, cap);
}

}  // namespace linkmsg
