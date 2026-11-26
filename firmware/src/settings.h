#pragma once
// The robot's settings (Wi-Fi, server address, token, and the wake word), typed once over
// serial and kept in NVS, so they survive reflashing and nothing secret goes into the build.
// The wake word's settings live in their own NVS namespace, so `forget` leaves them alone.
//
// Not thread-safe: the getters return static buffers that the setters overwrite, so read
// them and call the setters only from loop() (the serial commands and net:: run there).
#include <stdint.h>

namespace settings {

constexpr char kWifiUsage[] = "Usage: wifi <name> <password>  (use - for an open network)";

void load();

const char* ssid();   // "" when not set
const char* pass();   // "" when not set or an open network
const char* host();
const char* token();
uint16_t port();      // 0 when not set

bool complete();  // Wi-Fi, server and token are all set

// Each setter saves and returns nullptr, or returns why the value was refused or not saved.
// RAM copies change only once everything is saved.
const char* setWifi(const char* ssid, const char* pass);  // pass "" = open network
const char* setServer(const char* hostPort);  // "<host>:<port>", host [A-Za-z0-9.-]
const char* setToken(const char* token);
const char* forget();

// The wake word: on by default; the threshold in hundredths (50-99), 0 = the model's cutoff.
bool wakeOn();
uint8_t wakeThreshold();
const char* setWakeOn(bool on);
const char* setWakeThreshold(uint8_t hundredths);

}  // namespace settings
