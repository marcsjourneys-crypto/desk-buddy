// Desk Buddy firmware, M5 bring-up: a serial test menu for every part of the robot.
#include <Arduino.h>

#include "audio.h"
#include "eyes.h"
#include "pins.h"

namespace {

constexpr char kFwVersion[] = "0.1.0-m5";

char line[64];
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
  Serial.printf("Desk Buddy firmware %s (M5 bring-up)\n", kFwVersion);
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
}

void printHelp() {
  Serial.println("Commands:");
  Serial.println("  help                      this list");
  Serial.println("  info                      chip, flash, PSRAM, memory, uptime");
  Serial.println("  eyes idle|happy|sleep|blink|test");
  Serial.println("  fps                       eye frame rate over 3 s");
  Serial.println("  tone [hz]                 1 s test tone (440 Hz default)");
  Serial.println("  vol <0-100>               speaker volume (starts at 20)");
}

void handle(char* cmd) {
  char* name = strtok(cmd, " ");
  char* arg = strtok(nullptr, " ");
  if (name == nullptr) return;
  if (!strcmp(name, "help")) {
    printHelp();
  } else if (!strcmp(name, "info")) {
    printInfo();
  } else if (!strcmp(name, "eyes")) {
    if (arg == nullptr || !eyes::show(arg)) Serial.println("Usage: eyes idle|happy|sleep|blink|test");
  } else if (!strcmp(name, "fps")) {
    Serial.printf("Eyes: %.1f fps (both eyes)\n", eyes::measureFps(3000));
  } else if (!strcmp(name, "tone")) {
    const float hz = arg ? atof(arg) : 440;
    if (hz < 50 || hz > 8000) {
      Serial.println("Usage: tone [50-8000]");
    } else {
      Serial.printf("Tone %.0f Hz at volume %d\n", hz, audio::volume());
      audio::playTone(hz, 1000);
    }
  } else if (!strcmp(name, "vol")) {
    if (arg) audio::setVolume(atoi(arg));
    Serial.printf("Volume %d\n", audio::volume());
  } else {
    Serial.printf("Unknown command '%s'\n", name);
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
  if (!eyes::begin()) Serial.println("Eyes: not started");
  if (!audio::beginSpeaker()) Serial.println("Speaker: not started");
  Serial.println("Type 'help' for commands.");
}

void loop() {
  eyes::update(millis());
  pollSerial();
  delay(1);
}
