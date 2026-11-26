#pragma once
// The robot's side of the JSON control messages (docs/protocol.md): parsing what the
// server sends and building hello and wake. Pure apart from ArduinoJson.
#include <ArduinoJson.h>
#include <math.h>
#include <stddef.h>
#include <string.h>

namespace linkmsg {

enum class Msg {
  Welcome, State, Emotion, AudioStart, AudioEnd, AudioStop, WakeConfig, Unknown, Bad
};

// wake_config's threshold range (the same as wake_mode.h's; kept here so this stays standalone).
constexpr float kWakeThresholdMin = 0.5f;
constexpr float kWakeThresholdMax = 0.99f;

struct Parsed {
  Msg type = Msg::Bad;
  char name[16] = {0};  // state or emotion name, truncated to fit
  // wake_config: each field is optional; a present one must be valid or the message is Bad.
  bool hasEnabled = false;
  bool enabled = false;
  bool hasThreshold = false;
  float threshold = 0;
};

// {"type": "wake_config", "enabled": true, "threshold": 0.97}: enabled a bool, threshold a
// number from 0.5 to 0.99.
inline void parseWakeConfig(JsonDocument& doc, Parsed& out) {
  JsonVariant enabled = doc["enabled"];
  JsonVariant threshold = doc["threshold"];
  if (!enabled.isNull()) {
    if (!enabled.is<bool>()) return;
    out.hasEnabled = true;
    out.enabled = enabled.as<bool>();
  }
  if (!threshold.isNull()) {
    if (!threshold.is<float>()) return;
    const float t = threshold.as<float>();
    if (!(t >= kWakeThresholdMin - 1e-4f && t <= kWakeThresholdMax + 1e-4f)) return;
    out.hasThreshold = true;
    out.threshold = t;
  }
  out.type = Msg::WakeConfig;
}

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
  } else if (!strcmp(type, "wake_config")) {
    parseWakeConfig(doc, out);  // Bad unless every field present is valid
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

// Writes {"type":"wake","score":0.99}: the score clamped to 0..1 and rounded to 2 decimals.
// Returns its length, or 0 if it doesn't fit in `cap` (NUL included).
inline size_t buildWake(char* out, size_t cap, float score) {
  if (!(score >= 0)) score = 0;  // NaN too
  if (score > 1) score = 1;
  JsonDocument doc;
  doc["type"] = "wake";
  doc["score"] = round(static_cast<double>(score) * 100) / 100;
  if (cap == 0 || doc.overflowed() || measureJson(doc) >= cap) {
    if (cap) out[0] = '\0';
    return 0;
  }
  return serializeJson(doc, out, cap);
}

}  // namespace linkmsg
