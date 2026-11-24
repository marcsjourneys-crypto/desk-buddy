#include "servos.h"

#include <Arduino.h>

#include "pins.h"
#include "servo_math.h"

namespace {

constexpr float kSpeed = 90;       // deg/s
constexpr uint32_t kSettleMs = 200;  // keep driving briefly after arriving, then stop pulses
constexpr float kPanLimit = 60;
constexpr float kTiltLimit = 20;  // provisional until the head is built

// One servo. Its PWM channel is attached once in begin() and never detached; at rest the
// duty is 0, so no pulses are sent (no jitter, no buzz). LEDC applies a duty change at the
// period boundary, so stopping never cuts a pulse short.
// The real position is unknown at power-on, so the first move starts from "centre" and may
// jump.
struct Axis {
  Axis(int pin, float limit) : pin(pin), limit(limit) {}

  int pin;
  float limit;
  float angle = 0;
  servo::Motion motion;
  bool driving = false;
  bool arrived = false;
  uint32_t arrivedAt = 0;

  bool begin() {
    if (!ledcAttach(pin, servo::kFreqHz, servo::kBits)) return false;
    ledcWrite(pin, 0);
    return true;
  }

  void moveTo(float target, uint32_t now) {
    motion.moveTo(angle, servo::clampAngle(target, limit), now, kSpeed);
    arrived = false;
    driving = true;
  }

  void rest() {
    ledcWrite(pin, 0);
    driving = false;
  }

  void update(uint32_t now) {
    if (!driving) return;
    angle = motion.at(now);
    ledcWrite(pin, servo::pulseToDuty(servo::angleToPulseUs(angle)));
    if (!motion.done(now)) return;
    if (!arrived) {
      arrived = true;
      arrivedAt = now;
    } else if (now - arrivedAt >= kSettleMs) {
      rest();
    }
  }
};

Axis pan(pins::kServoPan, kPanLimit);
Axis tilt(pins::kServoTilt, kTiltLimit);
bool started = false;

bool run(Axis& axis, float target, bool (*stop)()) {
  if (!started) return false;
  axis.moveTo(target, millis());
  while (axis.driving) {
    axis.update(millis());
    if (stop()) {
      axis.rest();
      return false;
    }
    delay(5);
  }
  return true;
}

}  // namespace

namespace servos {

bool begin() {
  started = pan.begin() && tilt.begin();
  return started;
}

bool ready() { return started; }

bool sweep(bool (*stop)()) {
  const float panSteps[] = {0, -45, 45, 0};
  const float tiltSteps[] = {0, -15, 15, 0};
  for (float a : panSteps) {
    if (!run(pan, a, stop)) return false;
  }
  for (float a : tiltSteps) {
    if (!run(tilt, a, stop)) return false;
  }
  return true;
}

}  // namespace servos
