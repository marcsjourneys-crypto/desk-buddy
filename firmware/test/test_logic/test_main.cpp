// Unit tests for the pure logic in include/. They run on the board: pio test
#include <Arduino.h>
#include <unity.h>

#include "ease.h"
#include "eye_model.h"

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
  TEST_ASSERT_EQUAL_FLOAT(1.0f, ease::inOutCubic(7));  // clamped
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
  RUN_TEST(test_blinker);
  UNITY_END();
}

void loop() {}
