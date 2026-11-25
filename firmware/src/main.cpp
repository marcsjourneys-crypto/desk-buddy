// Desk Buddy firmware: the robot link (M6) plus the serial test menu from the M5 bring-up.
#include <Arduino.h>

#include <algorithm>
#include <iterator>

#include "audio.h"
#include "dsp.h"
#include "eyes.h"
#include "net.h"
#include "pins.h"
#include "servos.h"
#include "settings.h"

namespace {

constexpr char kFwVersion[] = "0.2.0-m6";

char line[301];  // room for "token " plus a 256-char token
size_t lineLen = 0;
bool overflow = false;  // the current line didn't fit; reject it at the line end

void blinkLed() {
  for (int i = 0; i < 3; i++) {
    rgbLedWrite(pins::kOnboardLed, 0, 0, 40);
    delay(150);
    rgbLedWrite(pins::kOnboardLed, 0, 0, 0);
    delay(150);
  }
}

void printInfo() {
  Serial.printf("Desk Buddy firmware %s\n", kFwVersion);
  Serial.printf("Chip: %s rev %d, %d cores @ %lu MHz\n", ESP.getChipModel(),
                ESP.getChipRevision(), ESP.getChipCores(),
                static_cast<unsigned long>(ESP.getCpuFreqMHz()));
  Serial.printf("Flash: %lu MB\n", static_cast<unsigned long>(ESP.getFlashChipSize() >> 20));
  if (psramFound()) {
    Serial.printf("PSRAM: %.1f MB (free %.1f MB)\n", ESP.getPsramSize() / 1048576.0,
                  ESP.getFreePsram() / 1048576.0);
  } else {
    Serial.println("PSRAM: NOT FOUND - check the board setting in platformio.ini");
  }
  Serial.printf("Heap free: %lu KB\n", static_cast<unsigned long>(ESP.getFreeHeap() / 1024));
  Serial.printf("Uptime: %lu s\n", static_cast<unsigned long>(millis() / 1000));
  Serial.printf("Speaker write errors: %lu\n", static_cast<unsigned long>(audio::writeErrors()));
}

void printHelp() {
  Serial.println("Commands:");
  Serial.println("  help                      this list");
  Serial.println("  info                      chip, flash, PSRAM, memory, uptime");
  Serial.println("  eyes <look>               idle happy sad sleep blink surprised wink skeptical");
  Serial.println("                            listening ponder small test");
  Serial.println("                            thinking excited (animated)");
  Serial.println("                            left right up down lookaround (crescents)");
  Serial.println("  fps                       eye frame rate over 3 s");
  Serial.println("  tone [50-7900]            1 s test tone in Hz (440 default)");
  Serial.println("  vol <0-100>               speaker volume (starts at 20)");
  Serial.println("  mic                       mic levels for 10 s");
  Serial.println("  rec / play                record 3 s, play it back");
  Serial.println("  servo                     eased pan/tilt sweep");
  Serial.println("Robot link setup (saved on the robot):");
  Serial.println("  wifi <name> <password>    Wi-Fi network; the name can't contain spaces, the");
  Serial.println("                            password is 8-63 chars (no tabs) or - for open");
  Serial.println("  server <host>:<port>      the PC running Buddy, e.g. 192.168.1.20:8765");
  Serial.println("  token <value>             ROBOT_TOKEN from server/.env (16-256 chars)");
  Serial.println("  config                    show the settings (secrets masked)");
  Serial.println("  forget                    wipe the Wi-Fi, server and token");
  Serial.println("  net                       Wi-Fi, link state, reconnects, mic frames, heap");
}

// The rest of the line after the command name, trimmed; nullptr if there's nothing.
char* trimmed(char* s) {
  if (s == nullptr) return nullptr;
  while (*s == ' ') s++;
  size_t n = strlen(s);
  while (n > 0 && s[n - 1] == ' ') s[--n] = '\0';
  return n ? s : nullptr;
}

// After a settings command: report, and reconnect with the new values if it was saved.
void printSaved(const char* error, bool wifi) {
  if (error != nullptr) {
    Serial.println(error);
    return;
  }
  net::restart(wifi);
  if (settings::complete()) {
    Serial.println("Saved. (reconnecting)");
  } else {
    Serial.println("Saved. Set wifi/server/token, then the robot connects.");
  }
}

// Never prints the password or the whole token.
void printConfig() {
  const char* notSet = "(not set)";
  Serial.printf("wifi:   %s\n", settings::ssid()[0] ? settings::ssid() : notSet);
  Serial.printf("pass:   %s\n", settings::pass()[0] ? "****" : notSet);
  if (settings::host()[0] && settings::port()) {
    Serial.printf("server: %s:%u\n", settings::host(), static_cast<unsigned>(settings::port()));
  } else {
    Serial.printf("server: %s\n", notSet);
  }
  const char* t = settings::token();
  const size_t n = strlen(t);
  if (n >= 2) {
    Serial.printf("token:  ****%s\n", t + n - 2);
  } else {
    Serial.printf("token:  %s\n", notSet);
  }
}

// `wifi <name> <password>`: the password is everything after the single space that follows
// the name, untrimmed, so it may contain spaces. "-" means an open network.
const char* wifiCommand(char* raw) {
  char* ssid = raw;
  if (ssid == nullptr) return settings::kWifiUsage;
  while (*ssid == ' ') ssid++;
  char* space = strchr(ssid, ' ');
  if (!*ssid || space == nullptr || space[1] == '\0') return settings::kWifiUsage;
  *space = '\0';
  const char* pass = space + 1;
  if (!strcmp(pass, "-")) pass = "";
  return settings::setWifi(ssid, pass);
}

// Settings commands get the raw rest of the line. Returns false if `name` isn't one.
bool handleSettings(const char* name, char* raw) {
  if (!strcmp(name, "wifi")) {
    printSaved(wifiCommand(raw), true);
  } else if (!strcmp(name, "server")) {
    printSaved(settings::setServer(trimmed(raw)), false);
  } else if (!strcmp(name, "token")) {
    printSaved(settings::setToken(trimmed(raw)), false);
  } else if (!strcmp(name, "config")) {
    printConfig();
  } else if (!strcmp(name, "forget")) {
    const char* error = settings::forget();
    if (error == nullptr) net::stop();
    Serial.println(error ? error : "Forgot the Wi-Fi, server and token.");
  } else if (!strcmp(name, "net")) {
    net::printStatus();
  } else {
    return false;
  }
  return true;
}

// Any typed character stops a running test. CR/LF don't count, by design: they are left
// over from the command line, so Enter alone never stops anything.
bool keyPressed() {
  bool any = false;
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c != '\r' && c != '\n') any = true;
  }
  return any;
}

// For long blocking tests: keeps the eyes animating, then checks for a key.
bool serviceAndCheckKey() {
  eyes::update(millis());
  return keyPressed();
}

constexpr size_t kRecSamples = dsp::kRate * 3;  // 3 s = 96 KB, in PSRAM
int16_t* recording = nullptr;
size_t recorded = 0;

void runMicLevels() {
  static int16_t buf[dsp::kRate / 10];  // 100 ms
  Serial.println("Mic levels for 10 s (press any letter to stop)");
  audio::flushMic();
  const uint32_t start = millis();
  while (millis() - start < 10000 && !keyPressed()) {
    const size_t n = audio::readMic(buf, std::size(buf));
    if (n == 0) {
      Serial.println("Mic: no data (not started)");
      return;
    }
    if (dsp::isFlat(buf, n)) {
      Serial.println("Mic: no signal (all samples equal) - is it wired and soldered?");
      return;
    }
    const dsp::Level lv = dsp::level(buf, n);
    const int bars = constrain(static_cast<int>((lv.rms_db + 60) / 2), 0, 30);  // -60..0 dBFS
    Serial.printf("%6.1f dBFS  peak %6.1f  |%-30.*s|\n", lv.rms_db, lv.peak_db, bars,
                  "##############################");
    eyes::update(millis());
  }
}

void runRecord() {
  if (recording == nullptr) {
    recording = static_cast<int16_t*>(ps_malloc(kRecSamples * sizeof(int16_t)));
  }
  if (recording == nullptr) {
    Serial.println("rec: no memory");
    return;
  }
  audio::flushMic();
  Serial.println("Recording 3 s... speak now");
  recorded = 0;
  while (recorded < kRecSamples) {
    const size_t n = audio::readMic(recording + recorded,
                                    std::min<size_t>(dsp::kRate / 50, kRecSamples - recorded));
    if (n == 0) break;
    recorded += n;
  }
  if (recorded == 0) {
    Serial.println("Mic: no data (not started)");
    return;
  }
  const dsp::Level lv = dsp::level(recording, recorded);
  Serial.printf("Recorded %.1f s, level %.1f dBFS, peak %.1f dBFS. Type 'play'.\n",
                recorded / static_cast<float>(dsp::kRate), lv.rms_db, lv.peak_db);
}

void runPlay() {
  if (recorded == 0) {
    Serial.println("Nothing recorded yet: type 'rec' first");
    return;
  }
  if (!audio::speakerReady()) {
    Serial.println("Speaker: not started");
    return;
  }
  Serial.printf("Playing %.1f s at volume %d\n", recorded / static_cast<float>(dsp::kRate),
                audio::volume());
  audio::play(recording, recorded);
}

void handle(char* cmd) {
  char* name = strtok(cmd, " ");
  if (name == nullptr) return;
  char* raw = strtok(nullptr, "");  // everything after the name, untouched
  if (handleSettings(name, raw)) return;
  char* rest = trimmed(raw);
  char* arg = rest ? strtok(rest, " ") : nullptr;
  const bool usesAudio = !strcmp(name, "tone") || !strcmp(name, "mic") ||
                         !strcmp(name, "rec") || !strcmp(name, "play");
  if (usesAudio && net::connected()) {  // the streaming tasks own the speaker and mic now
    Serial.println("Not while the robot link is connected");
    return;
  }
  if (!strcmp(name, "help")) {
    printHelp();
  } else if (!strcmp(name, "info")) {
    printInfo();
  } else if (!strcmp(name, "eyes")) {
    if (arg == nullptr || !eyes::show(arg)) Serial.println("Usage: eyes <look> (see help)");
  } else if (!strcmp(name, "fps")) {
    Serial.printf("Eyes: %.1f fps (both eyes)\n", eyes::measureFps(3000));
  } else if (!strcmp(name, "tone")) {
    float hz = 440;
    bool ok = true;
    if (arg) {
      char* end = nullptr;
      hz = strtof(arg, &end);
      ok = end != arg && *end == '\0';
    }
    // Written so NaN fails too. 7900 stays clear of the 8 kHz Nyquist limit.
    if (!ok || !(hz >= 50 && hz <= 7900)) {
      Serial.println("Usage: tone [50-7900]");
    } else {
      Serial.printf("Tone %.0f Hz at volume %d\n", hz, audio::volume());
      audio::playTone(hz, 1000);
    }
  } else if (!strcmp(name, "vol")) {
    if (arg) {
      char* end = nullptr;
      const long v = strtol(arg, &end, 10);
      if (end == arg || *end != '\0') {
        Serial.println("Usage: vol <0-100>");
        return;
      }
      audio::setVolume(static_cast<int>(v < 0 ? 0 : (v > 100 ? 100 : v)));
    }
    Serial.printf("Volume %d\n", audio::volume());
  } else if (!strcmp(name, "mic")) {
    runMicLevels();
  } else if (!strcmp(name, "rec")) {
    runRecord();
  } else if (!strcmp(name, "play")) {
    runPlay();
  } else if (!strcmp(name, "servo")) {
    Serial.println("Servo sweep (press any letter to stop)");
    if (servos::sweep(serviceAndCheckKey)) {
      Serial.println("Servo sweep done");
    } else if (!servos::ready()) {
      Serial.println("Servo: not started");
    } else {
      Serial.println("Servo sweep stopped");
    }
  } else {
    // A long "name" may be a pasted secret (a token without "token "): don't echo it.
    if (strlen(name) > 12) {
      Serial.println("Unknown command");
    } else {
      Serial.printf("Unknown command '%s'\n", name);
    }
    printHelp();
  }
}

// The monitor echoes what you type (monitor_echo), so the firmware doesn't.
void pollSerial() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r' || c == '\n') {
      line[lineLen] = '\0';
      if (overflow) {
        Serial.printf("Line too long (max %u chars)\n", static_cast<unsigned>(sizeof(line) - 1));
      } else if (lineLen > 0) {
        handle(line);
      }
      lineLen = 0;
      overflow = false;
    } else if (c == 0x08 || c == 0x7F) {  // backspace, DEL
      if (lineLen) lineLen--;
    } else if (static_cast<unsigned char>(c) < 0x20) {
      // other control characters: ignore
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = c;
    } else {
      overflow = true;
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(200);
  blinkLed();
  printInfo();
  settings::load();
  if (!eyes::begin()) Serial.println("Eyes: not started");
  eyes::show("sleep");  // until the link is up
  if (!audio::beginSpeaker()) Serial.println("Speaker: not started");
  if (!audio::beginMic()) Serial.println("Mic: not started");
  if (!servos::begin()) Serial.println("Servos: not started");
  net::begin(kFwVersion);
  if (!settings::complete()) Serial.println("Robot link: not set up (see 'help')");
  Serial.println("Type 'help' for commands.");
}

void loop() {
  net::loop();
  eyes::update(millis());
  pollSerial();
  delay(1);
}
