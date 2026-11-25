#include "net.h"

#include <Arduino.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#include "audio.h"
#include "backoff.h"
#include "eyes.h"
#include "link_msg.h"
#include "looks.h"
#include "settings.h"

namespace {

// The library hides the connection status we need, and its own reconnect logic would retry
// at once forever, so we drive every attempt ourselves (ws.loop() is only called while an
// attempt or a link is under way).
class LinkClient : public WebSocketsClient {
 public:
  // True once the TCP connection is open (the handshake or the link is under way).
  bool tcpOpen() const { return _client.status != WSC_NOT_CONNECTED; }
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

constexpr uint32_t kWelcomeTimeoutMs = 5000;
// The library doesn't pass the server's close code (4001/4002/4003) to the app, so closes
// before `welcome` stand in for it: this many in a row and we stop retrying.
constexpr int kMaxEarlyCloses = 3;
constexpr int kMaxFramesPerPass = 4;

LinkClient ws;
State state = State::NoConfig;
const char* fw = "";
Backoff backoff;
uint32_t since = 0;         // when the current state began
uint32_t backoffMs = 0;     // how long this Backoff lasts
bool attempted = false;     // Connecting: the TCP connect has been tried
bool wifiStarted = false;
int earlyCloses = 0;
uint32_t reconnects = 0;
uint32_t framesSent = 0;
bool badLogged = false;
char serverState[16] = "";
char emotion[16] = "neutral";

void enter(State s) {
  state = s;
  since = millis();
  attempted = false;
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

void startBackoff(const char* why) {
  backoffMs = backoff.next();
  reconnects++;
  Serial.printf("%s, retry in %lus\n", why, static_cast<unsigned long>(backoffMs / 1000));
  enter(State::Backoff);
}

// A close before `welcome`: the server refused the hello (bad token, blocked, too many
// half-open connections) or something in between failed.
void earlyClose(const char* why) {
  if (++earlyCloses >= kMaxEarlyCloses) {
    Serial.println("Server refused the robot (bad token or protocol) - check: token <value>");
    enter(State::Refused);
    return;
  }
  startBackoff(why);
}

void showLook() { eyes::show(lookForState(serverState, emotion)); }

void onText(const char* text, size_t len) {
  const linkmsg::Parsed m = linkmsg::parseServerText(text, len);
  using linkmsg::Msg;
  if (m.type == Msg::Welcome) {
    if (state != State::WaitingWelcome) return;
    earlyCloses = 0;
    backoff.reset();
    enter(State::Linked);
    if (!audio::streaming() && !audio::startStreaming()) {
      Serial.println("Audio: streaming didn't start (no memory?)");
    }
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
      char hello[384];
      const size_t n = linkmsg::buildHello(hello, sizeof(hello), settings::token(), fw);
      if (n == 0 || !ws.sendTXT(hello, n)) {
        Serial.println("Link: couldn't send hello");
        return;  // the welcome timeout cleans up
      }
      badLogged = false;
      enter(State::WaitingWelcome);
      break;
    }
    case WStype_TEXT:
      onText(reinterpret_cast<const char*>(payload), length);
      break;
    case WStype_BIN:
      if (state == State::Linked) audio::streamPush(payload, length);
      break;
    case WStype_DISCONNECTED: {
      if (!open()) return;  // we closed it on purpose; already handled
      const State was = state;
      if (was == State::Linked) linkDown();
      char why[96];
      if (was == State::Connecting) {
        snprintf(why, sizeof(why), "Can't connect to %s:%u", settings::host(),
                 static_cast<unsigned>(settings::port()));
        startBackoff(why);
      } else if (was == State::WaitingWelcome) {
        earlyClose("Closed before welcome");
      } else {
        startBackoff("Disconnected");
      }
      break;
    }
    default:
      break;
  }
}

void startWifi() {
  WiFi.persistent(false);  // the credentials live in our own NVS namespace only
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // lower latency for streamed audio
  WiFi.setAutoReconnect(true);
  WiFi.begin(settings::ssid(), settings::pass()[0] ? settings::pass() : nullptr);
  wifiStarted = true;
  Serial.printf("Wi-Fi: joining %s\n", settings::ssid());
}

void configure() {
  if (!settings::complete()) {
    enter(State::NoConfig);
    return;
  }
  if (!wifiStarted) startWifi();
  ws.begin(settings::host(), settings::port(), "/robot");
  ws.setReconnectInterval(0);  // unused: attempts only happen when we call ws.loop()
  ws.enableHeartbeat(15000, 5000, 2);  // detects a dead server; it also pings every 20 s
  enter(State::WifiConnecting);
}

// Drops any link without counting it as a failure.
void quietClose() {
  const bool wasLinked = state == State::Linked;
  enter(State::NoConfig);  // the DISCONNECTED event sees a closed state and does nothing
  ws.shut();
  if (wasLinked) linkDown();
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
  earlyCloses = 0;
  if (wifi && wifiStarted) {
    WiFi.disconnect();
    wifiStarted = false;
  }
  configure();
}

void stop() {
  quietClose();
  if (wifiStarted) {
    WiFi.disconnect();
    wifiStarted = false;
  }
}

bool connected() { return state == State::WaitingWelcome || state == State::Linked; }

void loop() {
  const uint32_t now = millis();
  const bool wifiUp = WiFi.status() == WL_CONNECTED;
  switch (state) {
    case State::NoConfig:
    case State::Refused:
      return;
    case State::WifiConnecting:
      if (wifiUp) {
        Serial.printf("Wi-Fi connected %s\n", WiFi.localIP().toString().c_str());
        enter(State::Connecting);
      }
      return;
    case State::Backoff:
      if (now - since >= backoffMs) enter(wifiUp ? State::Connecting : State::WifiConnecting);
      return;
    default:
      break;
  }
  if (!wifiUp) {
    Serial.println("Wi-Fi lost");
    quietClose();
    enter(State::WifiConnecting);
    return;
  }
  ws.loop();  // in Connecting, the first call makes the (blocking, up to 5 s) TCP connect
  if (state == State::Connecting && !attempted) {
    attempted = true;
    if (!ws.tcpOpen()) ws.shut();  // the connect failed: DISCONNECTED -> back off
    return;
  }
  if (state == State::WaitingWelcome && now - since >= kWelcomeTimeoutMs) {
    enter(State::NoConfig);  // so our own close isn't handled twice
    ws.shut();
    earlyClose("No welcome from the server");
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
  } else {
    Serial.printf("Wi-Fi:  %s\n", wifiStarted ? "not connected" : "off");
  }
  Serial.printf("Link:   %s", stateName(state));
  if (state == State::Backoff) {
    const uint32_t left = backoffMs - (millis() - since);
    Serial.printf(" (retry in %lus)", static_cast<unsigned long>(left / 1000 + 1));
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
