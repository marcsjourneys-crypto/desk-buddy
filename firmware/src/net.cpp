#include "net.h"

#include <Arduino.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#include "audio.h"
#include "backoff.h"
#include "eyes.h"
#include "link_msg.h"
#include "link_policy.h"
#include "looks.h"
#include "settings.h"

namespace {

// The library would reconnect by itself on every loop() while closed, and it hides the
// connection status we need. So its own reconnect interval is set to "never", and attempt()
// opens it for exactly one connect (WebSocketsClient::loop() only connects when
// millis() - _lastConnectionFail >= _reconnectInterval).
class LinkClient : public WebSocketsClient {
 public:
  LinkClient() { _reconnectInterval = UINT32_MAX; }

  // One connect: the TCP connect blocks for up to 5 s, then the handshake runs in loop().
  // Call with nothing open (shut() first).
  void attempt() {
    // A fresh heartbeat: no leftover pong timeouts, and the first ping goes out at once.
    _client.pongTimeoutCount = 0;
    _client.pongReceived = false;
    _client.lastPing = millis() - _client.pingInterval - 1;
    _reconnectInterval = 0;
    loop();
    _reconnectInterval = UINT32_MAX;
  }
  // True once the TCP connection is open (the handshake or the link is under way).
  bool tcpOpen() const { return _client.status != WSC_NOT_CONNECTED; }
  // Received data waiting: loop() handles one frame per call.
  bool rxPending() { return _client.tcp != nullptr && _client.tcp->available() > 0; }
  // Closes whatever is open, firing WStype_DISCONNECTED now (not on a later loop()).
  void shut() {
    if (isConnected()) {
      disconnect();  // sends a 1000 close frame first
    } else if (_client.tcp != nullptr) {
      clientDisconnect(&_client, nullptr);
    }
  }
};

enum class State { NoConfig, WifiConnecting, Connecting, WaitingWelcome, Linked, Backoff, Refused };

const char* stateName(State s) {
  switch (s) {
    case State::NoConfig: return "NoConfig";
    case State::WifiConnecting: return "WifiConnecting";
    case State::Connecting: return "Connecting";
    case State::WaitingWelcome: return "WaitingWelcome";
    case State::Linked: return "Linked";
    case State::Backoff: return "Backoff";
    case State::Refused: return "Refused";
  }
  return "?";
}

const char* wifiStatusName(wl_status_t s) {
  switch (s) {
    case WL_IDLE_STATUS: return "idle";
    case WL_NO_SSID_AVAIL: return "network not found";
    case WL_CONNECT_FAILED: return "connect failed (password?)";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_DISCONNECTED: return "disconnected";
    case WL_CONNECTED: return "connected";
    default: return "other";
  }
}

constexpr uint32_t kWelcomeTimeoutMs = 5000;
constexpr uint32_t kRefusedRetryMs = 5 * 60 * 1000;
constexpr uint32_t kWifiReportMs = 10000;
constexpr uint32_t kWifiRejoinMs = 30000;
constexpr uint32_t kWifiRestartGapMs = 200;  // between WiFi.disconnect() and begin()
constexpr int kMaxFramesPerPass = 4;
constexpr int kMaxWsLoopsPerPass = 16;  // drain queued frames: the eyes can take 40-60 ms

LinkClient ws;
State state = State::NoConfig;
const char* fw = "";
Backoff backoff;
LinkPolicy policy;
uint32_t since = 0;      // when the current state began
uint32_t backoffMs = 0;  // how long this Backoff lasts
uint32_t helloAt = 0;
uint32_t linkedAt = 0;
uint32_t wifiReportAt = 0;
uint32_t wifiJoinAt = 0;      // when the current join began
uint32_t wifiRejoinAt = 0;    // a rejoin is waiting for the disconnect to settle
bool wifiRejoinPending = false;
bool attempted = false;  // Connecting: the TCP connect has been tried
bool wifiStarted = false;
bool quiet = false;      // closing on purpose: ignore the DISCONNECTED event
uint32_t reconnects = 0;
uint32_t framesSent = 0;
bool badLogged = false;
char serverState[16] = "";
char emotion[16] = "neutral";

bool elapsed(uint32_t from, uint32_t ms) { return elapsedMs(millis(), from, ms); }

void enter(State s) {
  state = s;
  since = millis();
  attempted = false;
  if (s == State::WifiConnecting) wifiReportAt = since;
}

bool open() {
  return state == State::Connecting || state == State::WaitingWelcome ||
         state == State::Linked;
}

// The link is gone: stop the audio and put the eyes to sleep.
void linkDown() {
  audio::setMicStreaming(false);
  audio::streamStop();
  serverState[0] = '\0';
  strcpy(emotion, "neutral");
  eyes::show("sleep");
}

// Closes any connection without treating it as a failure.
void quietShut() {
  quiet = true;
  ws.shut();
  quiet = false;
}

void startBackoff(const char* why) {
  backoffMs = backoff.next();
  reconnects++;
  Serial.printf("%s, retry in %lus\n", why, static_cast<unsigned long>(backoffMs / 1000));
  enter(State::Backoff);
}

void refuse() {
  Serial.println("Server refused the robot (bad token or protocol) - check: token <value>");
  enter(State::Refused);
}

void showLook() { eyes::show(lookForState(serverState, emotion)); }

void onText(const char* text, size_t len) {
  const linkmsg::Parsed m = linkmsg::parseServerText(text, len);
  using linkmsg::Msg;
  if (m.type == Msg::Welcome) {
    if (state != State::WaitingWelcome) return;
    if (policy.welcomed()) backoff.reset();  // unless it keeps dropping straight away
    enter(State::Linked);
    linkedAt = since;
    audio::startStreaming();  // runs once; reports its own failures
    Serial.printf("Linked to %s:%u\n", settings::host(), static_cast<unsigned>(settings::port()));
    return;
  }
  if (state != State::Linked) return;
  switch (m.type) {
    case Msg::State:
      strcpy(serverState, m.name);  // both 16 bytes, NUL-terminated
      audio::setMicStreaming(!strcmp(serverState, "listening"));
      showLook();
      break;
    case Msg::Emotion:
      strcpy(emotion, m.name);
      if (!strcmp(serverState, "speaking")) showLook();
      break;
    case Msg::AudioStart:
      audio::streamBegin();
      break;
    case Msg::AudioEnd:
      audio::streamEnd();
      break;
    case Msg::AudioStop:
      audio::streamStop();
      break;
    case Msg::Bad:
      if (!badLogged) Serial.println("Link: ignoring a malformed message");
      badLogged = true;
      break;
    default:  // Unknown, and Welcome again: ignore
      break;
  }
}

void onEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      if (state != State::Connecting) return;
      char hello[640];  // a 256-char token, escaped, fits
      const size_t n = linkmsg::buildHello(hello, sizeof(hello), settings::token(), fw);
      if (n == 0 || !ws.sendTXT(hello, n)) {
        quietShut();
        startBackoff("Couldn't send hello");
        return;
      }
      badLogged = false;
      enter(State::WaitingWelcome);
      helloAt = since;
      break;
    }
    case WStype_TEXT:
      onText(reinterpret_cast<const char*>(payload), length);
      break;
    case WStype_BIN:
      if (state == State::Linked) audio::streamPush(payload, length);
      break;
    case WStype_DISCONNECTED: {
      if (quiet || !open()) return;
      const State was = state;
      if (was == State::Connecting) {  // the TCP connect worked, the handshake didn't
        char why[96];
        snprintf(why, sizeof(why), "Can't connect to %s:%u", settings::host(),
                 static_cast<unsigned>(settings::port()));
        startBackoff(why);
      } else if (was == State::WaitingWelcome) {
        if (policy.closedBeforeWelcome(millis() - helloAt)) {
          refuse();
        } else {
          startBackoff("Closed before welcome");
        }
      } else {
        policy.linkDropped(millis() - linkedAt);
        linkDown();
        startBackoff("Disconnected");
      }
      break;
    }
    default:
      break;
  }
}

void beginWifi() {
  WiFi.begin(settings::ssid(), settings::pass()[0] ? settings::pass() : nullptr);
  wifiStarted = true;
  wifiJoinAt = millis();
}

void startWifi() {
  WiFi.persistent(false);  // the credentials live in our own NVS namespace only
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // lower latency for streamed audio
  WiFi.setAutoReconnect(true);
  Serial.printf("Wi-Fi: joining %s\n", settings::ssid());
  beginWifi();
}

void configure() {
  if (!settings::complete()) {
    enter(State::NoConfig);
    return;
  }
  if (!wifiStarted) startWifi();
  ws.begin(settings::host(), settings::port(), "/robot");  // nothing is open here
  ws.enableHeartbeat(15000, 5000, 2);  // detects a dead server; it also pings every 20 s
  enter(State::WifiConnecting);
}

// Drops any link without counting it as a failure.
void quietClose() {
  const bool wasLinked = state == State::Linked;
  quietShut();
  if (wasLinked) linkDown();
  enter(State::NoConfig);
}

void stopWifi() {
  if (!wifiStarted) return;
  WiFi.disconnect();
  wifiStarted = false;
  wifiRejoinPending = false;
}

// Handles received frames; several per pass, so streamed audio keeps up with slow passes.
void pump() {
  for (int i = 0; i < kMaxWsLoopsPerPass; i++) {
    ws.loop();
    if (!open() || !ws.rxPending()) break;
  }
}

void loopWifiConnecting(bool wifiUp) {
  if (wifiUp) {
    Serial.printf("Wi-Fi connected %s\n", WiFi.localIP().toString().c_str());
    enter(State::Connecting);
    return;
  }
  if (wifiRejoinPending) {
    if (elapsed(wifiRejoinAt, kWifiRestartGapMs)) {
      wifiRejoinPending = false;
      beginWifi();
    }
    return;
  }
  if (elapsed(wifiReportAt, kWifiReportMs)) {
    wifiReportAt = millis();
    Serial.printf("Wi-Fi: still joining %s (%s)\n", settings::ssid(),
                  wifiStatusName(WiFi.status()));
  }
  if (elapsed(wifiJoinAt, kWifiRejoinMs)) {  // start the join afresh
    WiFi.disconnect();
    wifiRejoinPending = true;
    wifiRejoinAt = millis();
    wifiJoinAt = millis();
  }
}

}  // namespace

namespace net {

void begin(const char* fwVersion) {
  fw = fwVersion;
  ws.onEvent(onEvent);
  configure();
}

void restart(bool wifi) {
  quietClose();
  backoff.reset();
  policy.reset();
  if (wifi && wifiStarted) {
    stopWifi();
    delay(kWifiRestartGapMs);  // a serial command, not the steady loop: let it settle
  }
  configure();
}

void stop() {
  quietClose();
  stopWifi();
}

bool connected() { return state == State::WaitingWelcome || state == State::Linked; }

void loop() {
  const bool wifiUp = WiFi.status() == WL_CONNECTED;
  switch (state) {
    case State::NoConfig:
      return;
    case State::Refused:
      if (elapsed(since, kRefusedRetryMs)) {
        policy.retryAfterRefusal();  // one more early close refuses again
        enter(wifiUp ? State::Connecting : State::WifiConnecting);
      }
      return;
    case State::WifiConnecting:
      loopWifiConnecting(wifiUp);
      return;
    case State::Backoff:
      if (elapsed(since, backoffMs)) enter(wifiUp ? State::Connecting : State::WifiConnecting);
      return;
    default:
      break;
  }
  if (!wifiUp) {
    Serial.println("Wi-Fi lost");
    quietClose();
    enter(State::WifiConnecting);
    wifiJoinAt = millis();
    return;
  }
  if (state == State::Connecting && !attempted) {
    attempted = true;
    quietShut();  // nothing stale left for the library to clean up mid-attempt
    ws.attempt();
    if (!ws.tcpOpen()) {
      quietShut();
      char why[96];
      snprintf(why, sizeof(why), "Can't connect to %s:%u", settings::host(),
               static_cast<unsigned>(settings::port()));
      startBackoff(why);
    }
    return;
  }
  pump();
  if (state == State::WaitingWelcome && elapsed(helloAt, kWelcomeTimeoutMs)) {
    quietShut();
    policy.closedBeforeWelcome(kWelcomeTimeoutMs);  // slow, so not a refusal
    startBackoff("No welcome from the server");
    return;
  }
  if (state == State::Linked) {
    uint8_t frame[audio::kFrameBytes];
    for (int i = 0; i < kMaxFramesPerPass && audio::popMicFrame(frame); i++) {
      if (ws.sendBIN(frame, sizeof(frame))) framesSent++;
    }
  }
}

void printStatus() {
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("Wi-Fi:  connected, %s, RSSI %d dBm\n", WiFi.localIP().toString().c_str(),
                  static_cast<int>(WiFi.RSSI()));
  } else if (wifiStarted) {
    Serial.printf("Wi-Fi:  not connected (%s)\n", wifiStatusName(WiFi.status()));
  } else {
    Serial.println("Wi-Fi:  off");
  }
  Serial.printf("Link:   %s", stateName(state));
  if (state == State::Backoff || state == State::Refused) {
    const uint32_t wait = state == State::Backoff ? backoffMs : kRefusedRetryMs;
    const int32_t gone = static_cast<int32_t>(millis() - since);
    const int32_t left = gone >= static_cast<int32_t>(wait) ? 0 : static_cast<int32_t>(wait) - gone;
    Serial.printf(" (retry in %lds)", static_cast<long>((left + 999) / 1000));
  }
  Serial.println();
  Serial.printf("Reconnects: %lu\n", static_cast<unsigned long>(reconnects));
  Serial.printf("Mic frames: %lu sent, %lu dropped\n", static_cast<unsigned long>(framesSent),
                static_cast<unsigned long>(audio::micDropped()));
  Serial.printf("Heap free: %lu KB (min %lu KB)\n",
                static_cast<unsigned long>(ESP.getFreeHeap() / 1024),
                static_cast<unsigned long>(ESP.getMinFreeHeap() / 1024));
}

}  // namespace net
