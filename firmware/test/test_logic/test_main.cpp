// Unit tests for the pure logic in include/. They run on the board: pio test
#include <Arduino.h>
#include <unity.h>

#include "dsp.h"
#include "ease.h"
#include "eye_model.h"
#include "servo_math.h"

void setUp() {}
void tearDown() {}

void test_lerp_and_clamp() {
  TEST_ASSERT_EQUAL_FLOAT(5.0f, ease::lerp(0, 10, 0.5f));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, ease::clamp01(-2));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::clamp01(3));
}

void test_in_out_cubic() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, ease::inOutCubic(0));
  TEST_ASSERT_EQUAL_FLOAT(0.0625f, ease::inOutCubic(0.25f));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, ease::inOutCubic(0.5f));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::inOutCubic(1));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::inOutCubic(7));   // clamped
  TEST_ASSERT_EQUAL_FLOAT(0.0f, ease::inOutCubic(-1));  // clamped
}

void test_progress_survives_millis_wrap() {
  TEST_ASSERT_EQUAL_FLOAT(0.5f, ease::progress(0x00000010u, 0xFFFFFF00u, 544));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::progress(100, 0, 0));  // zero duration = done
}

void test_params_lerp() {
  const eye::Params p = eye::lerp(eye::presets::idle(), eye::presets::happy(), 0.5f);
  TEST_ASSERT_EQUAL_FLOAT(-0.8f, p.upper);
  TEST_ASSERT_EQUAL_FLOAT(0.2f, p.lower);
}

void test_closed_like_keeps_size_and_gaze() {
  eye::Params p = eye::presets::idle();
  p.gaze_x = 7;
  const eye::Params c = eye::closedLike(p);
  TEST_ASSERT_EQUAL_FLOAT(c.upper, c.lower);
  TEST_ASSERT_EQUAL_FLOAT(7.0f, c.gaze_x);
  TEST_ASSERT_EQUAL_FLOAT(p.radius, c.radius);
}

void test_same() {
  eye::Params a = eye::presets::idle();
  eye::Params b = a;
  TEST_ASSERT_TRUE(eye::same(a, b));
  b.upper += 0.1f;
  TEST_ASSERT_FALSE(eye::same(a, b));
}

void test_tween() {
  eye::Tween t;
  t.set(eye::presets::idle());
  t.to(eye::presets::happy(), 1000, 200);
  TEST_ASSERT_EQUAL_FLOAT(-1.0f, t.at(1000).upper);
  TEST_ASSERT_EQUAL_FLOAT(-0.8f, t.at(1100).upper);  // eased midpoint is exactly halfway
  TEST_ASSERT_EQUAL_FLOAT(-0.6f, t.at(1200).upper);
  TEST_ASSERT_TRUE(t.moving(1100));
  TEST_ASSERT_FALSE(t.moving(1200));
}

void test_tween_retarget_starts_from_current() {
  eye::Tween t;
  t.set(eye::presets::idle());
  t.to(eye::presets::happy(), 0, 200);
  t.to(eye::presets::idle(), 100, 200);  // mid-blend: starts from where it is now
  TEST_ASSERT_EQUAL_FLOAT(-0.8f, t.at(100).upper);
  TEST_ASSERT_EQUAL_FLOAT(-1.0f, t.at(300).upper);  // and ends at the new target
}

void test_tween_set_and_settle() {
  eye::Tween t;
  t.set(eye::presets::idle());
  TEST_ASSERT_FALSE(t.moving(0));
  t.to(eye::presets::happy(), 0, 200);
  t.settle(500);
  // Half a millis() wrap later the old blend must not replay.
  TEST_ASSERT_EQUAL_FLOAT(-0.6f, t.at(500 + 0x80000000u).upper);
}

void test_blinker() {
  eye::Blinker b;
  b.blinkNow(1000);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(999));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(1000));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, b.amount(1090));  // fully closed halfway through
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(1180));
  TEST_ASSERT_FALSE(b.finished(1100));
  TEST_ASSERT_TRUE(b.finished(1180));
  b.schedule(2000, 4000);
  TEST_ASSERT_FALSE(b.finished(5999));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(5999));
}

void test_blinker_default_is_not_blinking() {
  eye::Blinker b;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, b.amount(50));
  TEST_ASSERT_TRUE(b.finished(50));
}

void test_blinker_symmetric() {
  eye::Blinker b;
  b.blinkNow(1000);
  TEST_ASSERT_EQUAL_FLOAT(b.amount(1045), b.amount(1135));
}

void test_blinker_survives_millis_wrap() {
  eye::Blinker b;
  b.blinkNow(0xFFFFFFF0u);
  TEST_ASSERT_EQUAL_FLOAT(1.0f, b.amount(0xFFFFFFF0u + 90));
}

void test_mic_to_16() {
  TEST_ASSERT_EQUAL_INT16(4, dsp::micTo16(0x00010000));
  TEST_ASSERT_EQUAL_INT16(-4, dsp::micTo16(-0x00010000));
  TEST_ASSERT_EQUAL_INT16(32767, dsp::micTo16(0x7FFFFF00));   // saturates, no wrap
  TEST_ASSERT_EQUAL_INT16(-32768, dsp::micTo16(INT32_MIN));
}

void test_to_db() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::toDb(32768));
  TEST_ASSERT_FLOAT_WITHIN(0.05f, -20.0f, dsp::toDb(3277));
  TEST_ASSERT_EQUAL_FLOAT(-96.0f, dsp::toDb(0));
}

void test_level() {
  int16_t x[100];
  for (auto& s : x) s = 16384;
  dsp::Level lv = dsp::level(x, 100);
  TEST_ASSERT_FLOAT_WITHIN(0.05f, -6.02f, lv.rms_db);
  TEST_ASSERT_FLOAT_WITHIN(0.05f, -6.02f, lv.peak_db);
  x[3] = -32768;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::level(x, 100).peak_db);
  TEST_ASSERT_EQUAL_FLOAT(-96.0f, dsp::level(x, 0).rms_db);
}

void test_is_flat() {
  int16_t zeros[50] = {0};
  TEST_ASSERT_TRUE(dsp::isFlat(zeros, 50));
  zeros[10] = 300;
  TEST_ASSERT_FALSE(dsp::isFlat(zeros, 50));
}

void test_fade_gain() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(0, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, dsp::fadeGain(5, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, dsp::fadeGain(50, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, dsp::fadeGain(94, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(99, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, dsp::fadeGain(3, 100, 0));
}

void test_scale_volume() {
  TEST_ASSERT_EQUAL_INT16(5000, dsp::scale(10000, 50));
  TEST_ASSERT_EQUAL_INT16(0, dsp::scale(10000, 0));
  TEST_ASSERT_EQUAL_INT16(-32768, dsp::scale(-32768, 100));
}

void test_tone_continues_phase() {
  int16_t a[8], b[8], all[16];
  float p1 = 0, p2 = 0;
  dsp::tone(a, 8, 1000, 10000, p1);
  dsp::tone(b, 8, 1000, 10000, p1);
  dsp::tone(all, 16, 1000, 10000, p2);
  TEST_ASSERT_EQUAL_INT16(0, a[0]);
  TEST_ASSERT_EQUAL_INT16(all[8], b[0]);   // second block carries on seamlessly
  TEST_ASSERT_EQUAL_INT16(all[15], b[7]);
}

void test_servo_pulse() {
  TEST_ASSERT_EQUAL_UINT32(1500, servo::angleToPulseUs(0));
  TEST_ASSERT_EQUAL_UINT32(2500, servo::angleToPulseUs(90));
  TEST_ASSERT_EQUAL_UINT32(500, servo::angleToPulseUs(-90));
  TEST_ASSERT_EQUAL_UINT32(2500, servo::angleToPulseUs(120));  // clamped
  TEST_ASSERT_EQUAL_UINT32(1228, servo::pulseToDuty(1500));     // 14-bit duty at 50 Hz
  TEST_ASSERT_EQUAL_UINT32(409, servo::pulseToDuty(500));
}

void test_servo_clamp() {
  TEST_ASSERT_EQUAL_FLOAT(60.0f, servo::clampAngle(75, 60));
  TEST_ASSERT_EQUAL_FLOAT(-60.0f, servo::clampAngle(-75, 60));
  TEST_ASSERT_EQUAL_FLOAT(10.0f, servo::clampAngle(10, 60));
}

void test_servo_motion() {
  servo::Motion m;
  m.moveTo(0, 90, 1000, 90);  // 90 degrees at 90 deg/s = 1 s
  TEST_ASSERT_EQUAL_FLOAT(0.0f, m.at(1000));
  TEST_ASSERT_EQUAL_FLOAT(45.0f, m.at(1500));
  TEST_ASSERT_EQUAL_FLOAT(90.0f, m.at(2000));
  TEST_ASSERT_FALSE(m.done(1999));
  TEST_ASSERT_TRUE(m.done(2000));
  m.moveTo(90, 90, 3000, 90);  // no distance: done at once
  TEST_ASSERT_TRUE(m.done(3000));
}

void setup() {
  delay(2000);  // let the serial port settle
  UNITY_BEGIN();
  RUN_TEST(test_lerp_and_clamp);
  RUN_TEST(test_in_out_cubic);
  RUN_TEST(test_progress_survives_millis_wrap);
  RUN_TEST(test_params_lerp);
  RUN_TEST(test_closed_like_keeps_size_and_gaze);
  RUN_TEST(test_same);
  RUN_TEST(test_tween);
  RUN_TEST(test_tween_retarget_starts_from_current);
  RUN_TEST(test_tween_set_and_settle);
  RUN_TEST(test_blinker);
  RUN_TEST(test_blinker_default_is_not_blinking);
  RUN_TEST(test_blinker_symmetric);
  RUN_TEST(test_blinker_survives_millis_wrap);
  RUN_TEST(test_mic_to_16);
  RUN_TEST(test_to_db);
  RUN_TEST(test_level);
  RUN_TEST(test_is_flat);
  RUN_TEST(test_fade_gain);
  RUN_TEST(test_scale_volume);
  RUN_TEST(test_tone_continues_phase);
  RUN_TEST(test_servo_pulse);
  RUN_TEST(test_servo_clamp);
  RUN_TEST(test_servo_motion);
  UNITY_END();
}

void loop() {}
