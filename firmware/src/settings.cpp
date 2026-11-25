#include "settings.h"

#include <Preferences.h>
#include <stdlib.h>
#include <string.h>

namespace {

constexpr char kNamespace[] = "buddy";
constexpr size_t kMinToken = 16;
constexpr size_t kMaxToken = 256;

char ssidBuf[33];   // 802.11 allows 32 bytes
char passBuf[64];   // WPA2: 8-63 characters (or empty for an open network)
char hostBuf[64];
char tokenBuf[kMaxToken + 1];
uint16_t portVal = 0;

// Copies only if it fits; the setters check lengths first.
void copy(char* dst, size_t cap, const char* src) {
  strncpy(dst, src, cap - 1);
  dst[cap - 1] = '\0';
}

void readString(Preferences& p, const char* key, char* dst, size_t cap) {
  dst[0] = '\0';
  if (p.isKey(key)) p.getString(key, dst, cap);
}

}  // namespace

namespace settings {

void load() {
  Preferences p;
  // Read-write, so the first boot creates the namespace instead of logging an NVS error.
  if (!p.begin(kNamespace, false)) return;
  readString(p, "ssid", ssidBuf, sizeof(ssidBuf));
  readString(p, "pass", passBuf, sizeof(passBuf));
  readString(p, "host", hostBuf, sizeof(hostBuf));
  readString(p, "token", tokenBuf, sizeof(tokenBuf));
  portVal = p.getUShort("port", 0);
  p.end();
}

const char* ssid() { return ssidBuf; }
const char* pass() { return passBuf; }
const char* host() { return hostBuf; }
const char* token() { return tokenBuf; }
uint16_t port() { return portVal; }

bool complete() { return ssidBuf[0] && hostBuf[0] && portVal && tokenBuf[0]; }

const char* setWifi(const char* ssid, const char* pass) {
  if (ssid == nullptr || !ssid[0]) return "Usage: wifi <name> <password>";
  if (strlen(ssid) >= sizeof(ssidBuf)) return "Wi-Fi name too long (max 32 chars)";
  if (pass == nullptr) pass = "";
  const size_t n = strlen(pass);
  if (n != 0 && (n < 8 || n >= sizeof(passBuf))) return "Wi-Fi password must be 8-63 chars";
  Preferences p;
  if (!p.begin(kNamespace, false)) return "Couldn't open the settings store";
  p.putString("ssid", ssid);
  p.putString("pass", pass);
  p.end();
  copy(ssidBuf, sizeof(ssidBuf), ssid);
  copy(passBuf, sizeof(passBuf), pass);
  return nullptr;
}

const char* setServer(const char* hostPort) {
  static const char kUsage[] = "Usage: server <host>:<port>";
  if (hostPort == nullptr || strchr(hostPort, ' ') != nullptr) return kUsage;
  const char* colon = strrchr(hostPort, ':');
  if (colon == nullptr || colon == hostPort) return kUsage;
  const size_t hostLen = static_cast<size_t>(colon - hostPort);
  if (hostLen >= sizeof(hostBuf)) return "Server host too long (max 63 chars)";
  char* end = nullptr;
  const long port = strtol(colon + 1, &end, 10);
  if (end == colon + 1 || *end != '\0' || port < 1 || port > 65535) {
    return "Port must be 1-65535";
  }
  char host[sizeof(hostBuf)];
  memcpy(host, hostPort, hostLen);
  host[hostLen] = '\0';
  Preferences p;
  if (!p.begin(kNamespace, false)) return "Couldn't open the settings store";
  p.putString("host", host);
  p.putUShort("port", static_cast<uint16_t>(port));
  p.end();
  copy(hostBuf, sizeof(hostBuf), host);
  portVal = static_cast<uint16_t>(port);
  return nullptr;
}

const char* setToken(const char* token) {
  if (token == nullptr || !token[0]) return "Usage: token <value>";
  const size_t n = strlen(token);
  if (n < kMinToken || n > kMaxToken) return "Token must be 16-256 chars";
  if (strchr(token, ' ') != nullptr) return "Token can't contain spaces";
  Preferences p;
  if (!p.begin(kNamespace, false)) return "Couldn't open the settings store";
  p.putString("token", token);
  p.end();
  copy(tokenBuf, sizeof(tokenBuf), token);
  return nullptr;
}

void forget() {
  Preferences p;
  if (p.begin(kNamespace, false)) {
    p.clear();
    p.end();
  }
  ssidBuf[0] = passBuf[0] = hostBuf[0] = tokenBuf[0] = '\0';
  portVal = 0;
}

}  // namespace settings
