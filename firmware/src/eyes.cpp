#include "eyes.h"

#include <Arduino.h>
#define LGFX_USE_V1
#include <LovyanGFX.hpp>

#include "eye_model.h"
#include "pins.h"

namespace {

constexpr uint16_t kCyan = 0x2F3C;  // RGB565 of #2EE6E6, the expression guide's cyan
constexpr uint16_t kBlack = 0x0000;
constexpr int kSize = 240;
constexpr int kCenter = kSize / 2;
constexpr int kSegments = 24;  // straight pieces per lid curve
constexpr int kRotation = 0;   // 0 = arc at the pin edge; change once the head is built

// One GC9A01 on the shared SPI2 bus. Only the left panel drives the shared reset line:
// if both did, starting the right eye would reset the already-started left eye.
class Panel : public lgfx::LGFX_Device {
 public:
  Panel(int cs, int rst) {
    auto b = bus_.config();
    b.spi_host = SPI2_HOST;
    b.spi_mode = 0;
    b.freq_write = 80000000;
    b.freq_read = 16000000;
    b.spi_3wire = true;
    b.use_lock = true;
    b.dma_channel = SPI_DMA_CH_AUTO;
    b.pin_sclk = pins::kEyeSck;
    b.pin_mosi = pins::kEyeMosi;
    b.pin_miso = -1;
    b.pin_dc = pins::kEyeDc;
    bus_.config(b);
    panel_.setBus(&bus_);
    auto p = panel_.config();
    p.pin_cs = cs;
    p.pin_rst = rst;
    p.pin_busy = -1;
    p.panel_width = kSize;
    p.panel_height = kSize;
    p.invert = true;
    p.readable = false;
    p.bus_shared = true;
    panel_.config(p);
    setPanel(&panel_);
  }

 private:
  lgfx::Panel_GC9A01 panel_;
  lgfx::Bus_SPI bus_;
};

Panel leftPanel(pins::kEyeCsLeft, pins::kEyeRst);
Panel rightPanel(pins::kEyeCsRight, -1);
LGFX_Sprite leftSprite(&leftPanel);
LGFX_Sprite rightSprite(&rightPanel);
Panel* panels[2] = {&leftPanel, &rightPanel};
LGFX_Sprite* sprites[2] = {&leftSprite, &rightSprite};

eye::Tween tweens[2];
eye::Params drawn[2];
eye::Blinker blinker;
bool ready = false;
bool dirty = true;  // force the next update() to redraw
bool autoBlink = true;
bool testMode = false;

// One lid: a half-ellipse from the left corner to the right, as thick anti-aliased segments.
void drawLid(LGFX_Sprite& s, const eye::Params& p, float bulge) {
  const float cx = kCenter + p.gaze_x;
  const float cy = kCenter + p.gaze_y;
  const float w = p.thickness / 2;
  float px = 0, py = 0;
  for (int i = 0; i <= kSegments; i++) {
    const float th = PI * (1 - static_cast<float>(i) / kSegments);  // left corner -> right
    const float x = cx + p.radius * cosf(th);
    const float y = cy + bulge * p.radius * sinf(th);
    if (i > 0) s.drawWedgeLine(px, py, x, y, w, w, kCyan);
    px = x;
    py = y;
  }
}

void draw(int side, const eye::Params& p) {
  LGFX_Sprite& s = *sprites[side];
  s.fillScreen(kBlack);
  drawLid(s, p, p.upper);
  if (fabsf(p.lower - p.upper) > 0.02f) drawLid(s, p, p.lower);
  s.pushSprite(0, 0);
}

// "L" and "R" inside a ring, so you can tell the eyes apart and see the panel edges.
void drawTest() {
  const char* labels[2] = {"L", "R"};
  for (int i = 0; i < 2; i++) {
    LGFX_Sprite& s = *sprites[i];
    s.fillScreen(kBlack);
    s.fillSmoothCircle(kCenter, kCenter, 118, kCyan);
    s.fillSmoothCircle(kCenter, kCenter, 112, kBlack);
    s.setTextColor(kCyan);
    s.setTextSize(8);
    s.drawString(labels[i], kCenter - s.textWidth(labels[i]) / 2, kCenter - s.fontHeight() / 2);
    s.pushSprite(0, 0);
  }
}

}  // namespace

namespace eyes {

bool begin() {
  leftPanel.init();  // left first: it owns the shared reset line
  rightPanel.init();
  for (int i = 0; i < 2; i++) {
    panels[i]->setRotation(kRotation);
    sprites[i]->setPsram(true);
    sprites[i]->setColorDepth(16);
    if (sprites[i]->createSprite(kSize, kSize) == nullptr) {
      Serial.println("Eyes: no memory for the frame buffers (is PSRAM on?)");
      return false;
    }
    tweens[i].set(eye::presets::idle());
  }
  blinker.schedule(millis(), 2000);
  ready = true;
  dirty = true;
  return true;
}

bool show(const char* name) {
  if (!ready) return false;
  const uint32_t now = millis();
  if (!strcmp(name, "blink")) {
    blinker.blinkNow(now);
    return true;
  }
  if (!strcmp(name, "test")) {
    drawTest();
    testMode = true;
    return true;
  }
  eye::Params target;
  uint32_t ms = 250;
  bool blinks = true;
  if (!strcmp(name, "idle")) {
    target = eye::presets::idle();
  } else if (!strcmp(name, "happy")) {
    target = eye::presets::happy();
  } else if (!strcmp(name, "sleep")) {
    target = eye::presets::sleep();
    ms = 600;
    blinks = false;
  } else {
    return false;
  }
  testMode = false;
  autoBlink = blinks;
  dirty = true;
  for (auto& t : tweens) t.to(target, now, ms);
  return true;
}

void update(uint32_t now) {
  if (!ready || testMode) return;
  if (autoBlink && blinker.finished(now)) blinker.schedule(now, 3000 + esp_random() % 3000);
  const float b = blinker.amount(now);
  for (int i = 0; i < 2; i++) {
    eye::Params p = tweens[i].at(now);
    if (b > 0) p = eye::lerp(p, eye::closedLike(p), b);
    if (!dirty && eye::same(p, drawn[i])) continue;
    draw(i, p);
    drawn[i] = p;
  }
  dirty = false;
}

float measureFps(uint32_t ms) {
  if (!ready) return 0;
  const uint32_t start = millis();
  uint32_t frames = 0;
  while (millis() - start < ms) {
    for (int i = 0; i < 2; i++) {
      eye::Params p = tweens[i].at(millis());
      p.gaze_x = 15 * sinf(frames * 0.2f);  // keep it moving so every frame is real work
      draw(i, p);
    }
    frames++;
  }
  dirty = true;
  return frames * 1000.0f / (millis() - start);
}

}  // namespace eyes
