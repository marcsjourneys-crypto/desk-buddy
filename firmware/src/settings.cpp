#include "settings.h"

#include <Preferences.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

namespace {

constexpr char kNamespace[] = "buddy";
constexpr char kSaveFailed[] = "Couldn't save the setting (NVS error)";
constexpr size_t kMinToken = 16;
constexpr size_t kMaxToken = 256;

char ssidBuf[33];  // 802.11 allows 32 bytes
char passBuf[65];  // WPA2: an 8-63 char passphrase or a 64-hex-digit key; empty = open
char hostBuf[64];
char tokenBuf[kMaxToken + 1];
uint16_t portVal = 0;

// Copies, truncating to fit; the setters have already checked the length.
void copy(char* dst, size_t cap, const char* src) {
  strncpy(dst, src, cap - 1);
  dst[cap - 1] = '\0';
}

void readString(Preferences& p, const char* key, char* dst, size_t cap) {
  dst[0] = '\0';
  if (p.isKey(key)) p.getString(key, dst, cap);
}

// putString() returns the length written, so an empty value can't show success: store
// empty as "no key" instead (load() reads a missing key as "").
bool putStr(Preferences& p, const char* key, const char* value) {
  if (!value[0]) return !p.isKey(key) || p.remove(key);
  return p.putString(key, value) == strlen(value);
}

bool isHex(const char* s) {
  for (; *s; s++) {
    if (!isxdigit(static_cast<unsigned char>(*s))) return false;
  }
  return true;
}

bool validHost(const char* s) {
  if (!*s) return false;
  for (; *s; s++) {
    const char c = *s;
    if (!isalnum(static_cast<unsigned char>(c)) && c != '.' && c != '-') return false;
  }
  return true;
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
  if (ssid == nullptr || !ssid[0] || pass == nullptr) return kWifiUsage;
  if (strlen(ssid) >= sizeof(ssidBuf)) return "Wi-Fi name too long (max 32 chars)";
  const size_t n = strlen(pass);
  const bool ok = n == 0 || (n >= 8 && n <= 63) || (n == 64 && isHex(pass));
  if (!ok) return "Wi-Fi password must be 8-63 chars (or a 64-digit hex key)";
  Preferences p;
  if (!p.begin(kNamespace, false)) return kSaveFailed;
  const bool saved = putStr(p, "ssid", ssid) && putStr(p, "pass", pass);
  p.end();
  if (!saved) return kSaveFailed;
  copy(ssidBuf, sizeof(ssidBuf), ssid);
  copy(passBuf, sizeof(passBuf), pass);
  return nullptr;
}

const char* setServer(const char* hostPort) {
  static const char kUsage[] = "Usage: server <host>:<port>";
  if (hostPort == nullptr) return kUsage;
  const char* colon = strrchr(hostPort, ':');
  if (colon == nullptr || colon == hostPort) return kUsage;
  const size_t hostLen = static_cast<size_t>(colon - hostPort);
  if (hostLen >= sizeof(hostBuf)) return "Server host too long (max 63 chars)";
  char host[sizeof(hostBuf)];
  memcpy(host, hostPort, hostLen);
  host[hostLen] = '\0';
  if (!validHost(host)) return "Server host may only use letters, digits, '.' and '-'";
  char* end = nullptr;
  const long port = strtol(colon + 1, &end, 10);
  if (end == colon + 1 || *end != '\0' || port < 1 || port > 65535) {
    return "Port must be 1-65535";
  }
  Preferences p;
  if (!p.begin(kNamespace, false)) return kSaveFailed;
  const bool saved =
      putStr(p, "host", host) && p.putUShort("port", static_cast<uint16_t>(port)) == 2;
  p.end();
  if (!saved) return kSaveFailed;
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
  if (!p.begin(kNamespace, false)) return kSaveFailed;
  const bool saved = putStr(p, "token", token);
  p.end();
  if (!saved) return kSaveFailed;
  copy(tokenBuf, sizeof(tokenBuf), token);
  return nullptr;
}

const char* forget() {
  Preferences p;
  if (!p.begin(kNamespace, false)) return kSaveFailed;
  const bool cleared = p.clear();
  p.end();
  if (!cleared) return kSaveFailed;
  ssidBuf[0] = passBuf[0] = hostBuf[0] = tokenBuf[0] = '\0';
  portVal = 0;
  return nullptr;
}

}  // namespace settings
