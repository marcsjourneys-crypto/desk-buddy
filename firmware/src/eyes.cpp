#include "eyes.h"

#include <Arduino.h>
#define LGFX_USE_V1
#include <LovyanGFX.hpp>

#include "eye_model.h"
#include "pins.h"

namespace {

constexpr uint16_t kCyan = 0x2F3C;  // RGB565 of #2EE6E6, the expression guide's cyan
constexpr uint16_t kBlack = 0x0000;
constexpr int kSize = 240;  // panel
constexpr int kCenter = kSize / 2;
// One sprite in internal RAM (80 KB) draws both eyes in turn: drawing in PSRAM was the
// bottleneck. It covers the middle of each panel; the border is cleared once.
constexpr int kSprite = 200;
constexpr int kSpriteCenter = kSprite / 2;
constexpr int kSpriteOffset = (kSize - kSprite) / 2;
constexpr int kSegments = 16;  // straight pieces per lid curve (24 looks smoother: 22 fps)
constexpr int kRotation = 0;   // 0 = arc at the pin edge; change once the head is built

// One GC9A01 on the shared SPI2 bus. Only the left panel drives the shared reset line:
// if both did, starting the right eye would reset the already-started left eye.
// Each panel has its own Bus_SPI, and both use SPI2, which is also Arduino's default SPI
// host. So never release() one panel (it would free the bus under the other), and anything
// else that uses the global `SPI` object would end up on the eye pins.
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
Panel* panels[2] = {&leftPanel, &rightPanel};
LGFX_Sprite sprite(&leftPanel);  // shared; always pushed to an explicit panel

eye::Tween tweens[2];
eye::Params drawn[2];
eye::Blinker blinker;
bool ready = false;
bool dirty = true;  // force the next update() to redraw
bool autoBlink = true;
bool testMode = false;
bool drifting = false;  // the thinking look's slow left-right gaze
uint32_t driftStart = 0;

// The thinking drift's offset: starts at 0, so the look eases in without a jump.
float driftAt(uint32_t now) {
  if (!drifting) return 0;
  const float phase = static_cast<float>((now - driftStart) % eye::kThinkingPeriodMs) /
                      eye::kThinkingPeriodMs;
  return eye::kThinkingDrift * sinf(2 * PI * phase);
}

// The look as it should appear now, before any blink.
eye::Params current(int side, uint32_t now) {
  eye::Params p = tweens[side].at(now);
  p.gaze_x += driftAt(now);
  return p;
}

// One lid: a half-ellipse from the left corner to the right, as thick anti-aliased segments,
// turned by p.tilt about the eye's centre (anticlockwise on screen for a positive tilt, so
// the left corner goes down).
void drawLid(LGFX_Sprite& s, const eye::Params& p, float bulge) {
  const float cx = kSpriteCenter + p.gaze_x;
  const float cy = kSpriteCenter + p.gaze_y;
  const float w = p.thickness / 2;
  const float c = cosf(p.tilt);
  const float sn = sinf(p.tilt);
  float px = 0, py = 0;
  for (int i = 0; i <= kSegments; i++) {
    const float th = PI * (1 - static_cast<float>(i) / kSegments);  // left corner -> right
    const float dx = p.radius * cosf(th);
    const float dy = bulge * p.radius * sinf(th);  // screen y grows downwards
    const float x = cx + dx * c + dy * sn;
    const float y = cy - dx * sn + dy * c;
    if (i > 0) s.drawWedgeLine(px, py, x, y, w, w, kCyan);
    px = x;
    py = y;
  }
}

// Moves the gaze back so the whole shape (stroke included) stays inside the sprite.
// A tilt adds up to radius * |sin(tilt)| above and below; it can't widen a lid past its
// radius, so the x limit needs nothing extra.
eye::Params fitInSprite(eye::Params p) {
  const float w = p.thickness / 2 + 1;
  const float t = p.radius * fabsf(sinf(p.tilt));
  const float maxX = fmaxf(0, kSpriteCenter - p.radius - w);
  p.gaze_x = fminf(fmaxf(p.gaze_x, -maxX), maxX);
  const float up = -fminf(0, fminf(p.upper, p.lower)) * p.radius + w + t;    // above cy
  const float down = fmaxf(0, fmaxf(p.upper, p.lower)) * p.radius + w + t;  // below cy
  p.gaze_y = fminf(fmaxf(p.gaze_y, up - kSpriteCenter), kSpriteCenter - down);
  return p;
}

// Drawing the lids is the slow part (~23 ms; a push is ~8 ms), so when both eyes look the
// same, the second one reuses what is already in the sprite.
eye::Params inSprite;
bool spriteValid = false;

void draw(int side, const eye::Params& params) {
  eye::Params mirrored = params;
  // Panel 0 is the eye on your left as you face the robot, so a positive tilt droops its
  // left (outer) corner; the other eye turns the other way to droop its right corner.
  // If the head is ever built with the panels swapped, flip this.
  if (side == 1) mirrored.tilt = -mirrored.tilt;
  const eye::Params p = fitInSprite(mirrored);
  if (!spriteValid || !eye::same(p, inSprite)) {
    sprite.fillScreen(kBlack);
    drawLid(sprite, p, p.upper);
    if (fabsf(p.lower - p.upper) > 0.02f) drawLid(sprite, p, p.lower);
    inSprite = p;
    spriteValid = true;
  }
  sprite.pushSprite(panels[side], kSpriteOffset, kSpriteOffset);
}

void clearPanels() {
  for (auto* panel : panels) panel->fillScreen(kBlack);
}

// "L" and "R" inside a ring, so you can tell the eyes apart and see the panel edges.
// A one-off, so it draws straight on the panels (the ring is outside the sprite).
void drawTest() {
  const char* labels[2] = {"L", "R"};
  for (int i = 0; i < 2; i++) {
    Panel& s = *panels[i];
    s.fillScreen(kBlack);
    s.fillSmoothCircle(kCenter, kCenter, 118, kCyan);
    s.fillSmoothCircle(kCenter, kCenter, 112, kBlack);
    s.setTextColor(kCyan);
    s.setTextSize(8);
    s.drawString(labels[i], kCenter - s.textWidth(labels[i]) / 2, kCenter - s.fontHeight() / 2);
  }
}

}  // namespace

namespace eyes {

bool begin() {
  // Both chip selects high first, so the right panel ignores the left panel's init traffic.
  for (int cs : {pins::kEyeCsLeft, pins::kEyeCsRight}) {
    pinMode(cs, OUTPUT);
    digitalWrite(cs, HIGH);
  }
  leftPanel.init();  // left first: it owns the shared reset line
  rightPanel.init();
  for (int i = 0; i < 2; i++) {
    panels[i]->setRotation(kRotation);
    tweens[i].set(eye::presets::idle());
  }
  clearPanels();
  sprite.setPsram(false);
  sprite.setColorDepth(16);
  if (sprite.createSprite(kSprite, kSprite) == nullptr) {
    Serial.println("Eyes: no memory for the frame buffer");
    sprite.deleteSprite();
    return false;
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
  namespace ps = eye::presets;
  constexpr float kLook = 28;  // px; fitInSprite() keeps it inside the sprite
  eye::Params left, right;
  uint32_t ms = 250;
  bool blinks = true;
  if (!strcmp(name, "idle")) {
    left = right = ps::idle();
  } else if (!strcmp(name, "happy")) {
    left = right = ps::happy();
  } else if (!strcmp(name, "sleep")) {
    left = right = ps::sleep();
    ms = 600;
    blinks = false;
  } else if (!strcmp(name, "surprised")) {
    left = right = ps::surprised();
    ms = 150;
  } else if (!strcmp(name, "left")) {  // as you look at the robot
    left = right = ps::look(-kLook, 0);
  } else if (!strcmp(name, "right")) {
    left = right = ps::look(kLook, 0);
  } else if (!strcmp(name, "up")) {
    left = right = ps::look(0, -kLook);
  } else if (!strcmp(name, "down")) {
    left = right = ps::look(0, kLook);
  } else if (!strcmp(name, "wink")) {
    left = ps::happy();
    right = ps::closedLine();
    ms = 180;
    blinks = false;  // a blink would reopen the winking eye
  } else if (!strcmp(name, "skeptical")) {
    left = right = ps::skeptical();
    ms = 400;
  } else if (!strcmp(name, "sad")) {
    left = right = ps::sad();
    ms = 500;
  } else if (!strcmp(name, "listening")) {
    left = right = ps::listening();
    ms = 150;
  } else if (!strcmp(name, "thinking")) {
    left = right = ps::thinking();
    ms = 400;
  } else {
    return false;
  }
  if (testMode) clearPanels();  // the test ring is outside the sprite
  testMode = false;
  if (drifting) {  // carry on from where the drift is, so leaving it doesn't jump
    for (int i = 0; i < 2; i++) tweens[i].set(current(i, now));
  }
  drifting = !strcmp(name, "thinking");
  driftStart = now;
  autoBlink = blinks;
  // A fresh schedule: after a long sleep, the old one could be stale enough to wrap.
  if (blinks) blinker.schedule(now, 3000 + esp_random() % 3000);
  dirty = true;
  tweens[0].to(left, now, ms);
  tweens[1].to(right, now, ms);
  return true;
}

void update(uint32_t now) {
  if (!ready || testMode) return;
  if (autoBlink && blinker.finished(now)) blinker.schedule(now, 3000 + esp_random() % 3000);
  const float b = blinker.amount(now);
  for (int i = 0; i < 2; i++) {
    tweens[i].settle(now);
    eye::Params p = current(i, now);
    if (b > 0) p = eye::lerp(p, eye::closedLike(p), b);
    if (!dirty && eye::same(p, drawn[i])) continue;
    draw(i, p);
    drawn[i] = p;
  }
  dirty = false;
}

float measureFps(uint32_t ms) {
  if (!ready || ms == 0) return 0;
  if (testMode) {  // leave test mode, or the screen would stay frozen on the last frame
    testMode = false;
    clearPanels();
  }
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
