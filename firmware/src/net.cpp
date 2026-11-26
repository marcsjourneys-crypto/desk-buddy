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
#include "wake.h"
#include "wake_mode.h"

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
// While the wake word's pre-roll is waiting (it can be ~1.5 s), more per pass, so it catches
// up a few times faster than real time. 8 frames (5 KB) stay within lwIP's TCP send buffer,
// so a pass doesn't wait on the network; the server takes frames as fast as they come.
constexpr int kMaxBacklogFramesPerPass = 8;
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

// Wake mode (main loop only). The server's wake_config overrides the saved settings, field by
// field, until restart.
wake::Gate gate;
struct ServerWake {
  bool hasEnabled = false;
  bool enabled = false;
  bool hasThreshold = false;
  float threshold = 0;
} serverWake;
bool wakeBegun = false;  // wake::begin() has been tried from here
uint32_t wakesSent = 0;
uint32_t wakesIgnored = 0;  // detections in a hold-off, or while not listening
uint32_t wakesUnanswered = 0;

bool wakeOn() { return serverWake.hasEnabled ? serverWake.enabled : settings::wakeOn(); }

float wakeThreshold() {
  if (serverWake.hasThreshold) return serverWake.threshold;
  const uint8_t t = settings::wakeThreshold();
  return t ? t / 100.0f : wake::modelCutoff();
}

audio::MicMode micModeFor(wake::Mic m) {
  switch (m) {
    case wake::Mic::Wake: return audio::MicMode::Wake;
    case wake::Mic::Hold: return audio::MicMode::Hold;
    case wake::Mic::Stream: return audio::MicMode::Stream;
    default: return audio::MicMode::Off;
  }
}

// The mic goes where the gate says, and the wake engine runs only in Wake mode.
void applyMic() {
  const wake::Mic m = gate.mic();
  audio::setMicMode(micModeFor(m));
  wake::run(m == wake::Mic::Wake);
}

bool elapsed(uint32_t from, uint32_t ms) { return elapsedMs(millis(), from, ms); }

// Forward: printStatus() is in namespace net.
void printWake();

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
  gate.onLink(false, millis());
  applyMic();  // Off: the mic reader lets go, the wake engine pauses
  audio::dropPreRoll();
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
    gate.onLink(true, millis());
    Serial.printf("Linked to %s:%u\n", settings::host(), static_cast<unsigned>(settings::port()));
    return;
  }
  if (state != State::Linked) return;
  switch (m.type) {
    case Msg::State: {
      strcpy(serverState, m.name);  // both 16 bytes, NUL-terminated
      // A pending wake ends here: `listening` streams the kept pre-roll first (Hold ->
      // Stream), anything else means the server ignored it.
      if (gate.onState(wake::serverFromName(serverState), millis()) == wake::Answer::Other) {
        audio::dropPreRoll();
      }
      applyMic();
      showLook();
      break;
    }
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
    case Msg::WakeConfig:
      if (m.hasEnabled) {
        serverWake.hasEnabled = true;
        serverWake.enabled = m.enabled;
      }
      if (m.hasThreshold) {
        serverWake.hasThreshold = true;
        serverWake.threshold = m.threshold;
      }
      Serial.printf("Wake word: the server set it %s, threshold %.2f (until restart)\n",
                    wakeOn() ? "on" : "off", wakeThreshold());
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

// Wake mode, every pass while linked: starts the engine when it's wanted, keeps the gate's
// inputs current, gives up a wake the server didn't answer, and sends a detection.
void serviceWake() {
  const uint32_t now = millis();
  if (wakeOn() && wake::haveModel() && !wakeBegun) {
    wakeBegun = true;  // begin() runs once anyway; report a failure once
    if (const char* e = wake::begin()) Serial.printf("Wake word off: %s\n", e);
  }
  wake::setThreshold(wakeThreshold());
  gate.setEnabled(wakeOn() && wake::ready());
  if (gate.expire(now)) {
    wakesUnanswered++;
    audio::dropPreRoll();
    Serial.println("Wake: no answer from the server; listening again in 2 s");
    showLook();
  }
  applyMic();
  float score = 0;
  uint32_t seq = 0;
  if (!wake::takeDetection(&score, &seq)) return;
  if (!gate.onDetection(now)) {
    wakesIgnored++;
    return;
  }
  // Keep the audio from just after the detection; the mic goes to Hold (the engine pauses)
  // until the server's `state listening` turns it into the stream.
  audio::keepPreRollFrom(seq + 1);
  applyMic();
  eyes::show("listening");
  char msg[48];
  const size_t n = linkmsg::buildWake(msg, sizeof(msg), score);
  if (n > 0 && ws.sendTXT(msg, n)) wakesSent++;
  Serial.printf("Wake: heard (score %.2f)\n", score);
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
    serviceWake();
    uint8_t frame[audio::kFrameBytes];
    const int most = audio::micBacklog() ? kMaxBacklogFramesPerPass : kMaxFramesPerPass;
    for (int i = 0; i < most && audio::popMicFrame(frame); i++) {
      if (ws.sendBIN(frame, sizeof(frame))) framesSent++;
    }
  }
}

bool wakeFromServer() { return serverWake.hasEnabled || serverWake.hasThreshold; }

float wakeThresholdInUse() { return wakeThreshold(); }

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
  printWake();
  Serial.printf("Heap free: %lu KB (min %lu KB)\n",
                static_cast<unsigned long>(ESP.getFreeHeap() / 1024),
                static_cast<unsigned long>(ESP.getMinFreeHeap() / 1024));
}

}  // namespace net

namespace {

// The `net` command's wake word lines.
void printWake() {
  const char* thresholdFrom = serverWake.hasThreshold ? "server"
                              : settings::wakeThreshold() ? "saved"
                                                          : "model";
  Serial.printf("Wake:   %s (%s), threshold %.2f (%s), ", wakeOn() ? "on" : "off",
                serverWake.hasEnabled ? "server" : "saved", wakeThreshold(), thresholdFrom);
  const uint32_t now = millis();
  if (!wake::haveModel()) {
    Serial.println("no model built in");
  } else if (wakeOn() && wakeBegun && !wake::ready()) {
    Serial.println("engine failed (see wake info)");
  } else if (state != State::Linked) {
    Serial.println("paused (no link)");
  } else if (gate.pending()) {
    Serial.println("heard, waiting for the server");
  } else if (gate.mic() == wake::Mic::Wake) {
    Serial.println(gate.holdingOff(now) ? "listening (holding off)" : "listening");
  } else if (!wakeOn()) {
    Serial.println("off");
  } else {
    Serial.printf("paused (state %s)\n", serverState[0] ? serverState : "none yet");
  }
  Serial.printf("        wakes sent %lu, unanswered %lu, ignored %lu\n",
                static_cast<unsigned long>(wakesSent), static_cast<unsigned long>(wakesUnanswered),
                static_cast<unsigned long>(wakesIgnored));
}

}  // namespace
