#pragma once
// The robot's settings (Wi-Fi, server address, token), typed once over serial and kept in
// NVS, so they survive reflashing and nothing secret goes into the build.
#include <stdint.h>

namespace settings {

void load();

const char* ssid();   // "" when not set
const char* pass();
const char* host();
const char* token();
uint16_t port();      // 0 when not set

bool complete();  // Wi-Fi, server and token are all set

// Each setter saves and returns nullptr, or returns why the value was refused.
const char* setWifi(const char* ssid, const char* pass);
const char* setServer(const char* hostPort);  // "<host>:<port>"
const char* setToken(const char* token);
void forget();

}  // namespace settings
