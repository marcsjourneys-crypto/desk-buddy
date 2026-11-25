// Unit tests for the pure logic in include/. They run on the board: pio test
#include <Arduino.h>
#include <unity.h>

#include <string.h>

#include "backoff.h"
#include "dc_block.h"
#include "dsp.h"
#include "ease.h"
#include "eye_model.h"
#include "link_msg.h"
#include "looks.h"
#include "ring_buffer.h"
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

void test_params_lerp_tilt() {
  const eye::Params p = eye::lerp(eye::presets::idle(), eye::presets::sad(), 0.5f);
  TEST_ASSERT_EQUAL_FLOAT(0.175f, p.tilt);
  eye::Params a = eye::presets::idle();
  eye::Params b = a;
  b.tilt = 0.1f;
  TEST_ASSERT_FALSE(eye::same(a, b));
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
  TEST_ASSERT_TRUE(dsp::isFlat(zeros, 0));
  const int16_t jitter[4] = {5, 6, 5, 6};  // hi - lo == 1 still counts as flat
  TEST_ASSERT_TRUE(dsp::isFlat(jitter, 4));
}

void test_fade_gain() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(0, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, dsp::fadeGain(5, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, dsp::fadeGain(50, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, dsp::fadeGain(94, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(99, 100, 10));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, dsp::fadeGain(3, 100, 0));
}

void test_fade_gain_edges() {
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(0, 10, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(9, 10, 10));
  // Fades longer than half the sound meet in the middle, symmetrically.
  TEST_ASSERT_EQUAL_FLOAT(dsp::fadeGain(4, 10, 10), dsp::fadeGain(5, 10, 10));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(100, 100, 10));  // past the end
  TEST_ASSERT_EQUAL_FLOAT(0.0f, dsp::fadeGain(0, 0, 10));      // empty sound
}

void test_scale_volume() {
  TEST_ASSERT_EQUAL_INT16(5000, dsp::scale(10000, 50));
  TEST_ASSERT_EQUAL_INT16(0, dsp::scale(10000, 0));
  TEST_ASSERT_EQUAL_INT16(-32768, dsp::scale(-32768, 100));
  TEST_ASSERT_EQUAL_INT16(-1, dsp::scale(-3, 50));  // truncates toward zero
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

void test_tone_values() {
  int16_t a[16];
  float phase = 0;
  dsp::tone(a, 16, 1000, 10000, phase);  // 16 samples per cycle at 16 kHz
  TEST_ASSERT_INT16_WITHIN(2, 10000, a[4]);  // quarter cycle: the peak
  TEST_ASSERT_INT16_WITHIN(2, 0, a[8]);      // half cycle: back through zero
}

void test_servo_pulse() {
  TEST_ASSERT_EQUAL_UINT32(1500, servo::angleToPulseUs(0));
  TEST_ASSERT_EQUAL_UINT32(2500, servo::angleToPulseUs(90));
  TEST_ASSERT_EQUAL_UINT32(500, servo::angleToPulseUs(-90));
  TEST_ASSERT_EQUAL_UINT32(2500, servo::angleToPulseUs(120));  // clamped
  TEST_ASSERT_EQUAL_UINT32(1000, servo::angleToPulseUs(-45));
  TEST_ASSERT_EQUAL_UINT32(500, servo::angleToPulseUs(-120));  // clamped
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

void test_servo_motion_bad_speed() {
  servo::Motion m;
  m.moveTo(0, 90, 1000, 0);
  TEST_ASSERT_TRUE(m.done(1000));
  TEST_ASSERT_EQUAL_FLOAT(90.0f, m.at(1000));
  m.moveTo(0, 90, 2000, -10);
  TEST_ASSERT_TRUE(m.done(2000));
}

void test_servo_motion_reverse() {
  servo::Motion m;
  m.moveTo(90, 0, 1000, 90);
  TEST_ASSERT_EQUAL_FLOAT(45.0f, m.at(1500));
}

void test_servo_motion_across_millis_wrap() {
  servo::Motion m;
  const uint32_t start = 0xFFFFFE0Cu;  // 500 ms before millis() wraps
  m.moveTo(0, 90, start, 90);
  TEST_ASSERT_EQUAL_FLOAT(45.0f, m.at(start + 500));
  TEST_ASSERT_TRUE(m.done(start + 1000));
}

// Every preset (with the largest look offset) stays inside the 200 px sprite, stroke
// included, so fitInSprite() never has to move it. Tilt adds radius * |sin(tilt)| above
// and below (rotating can't widen a lid past its radius, so x needs nothing extra).
void test_presets_fit_sprite() {
  namespace ps = eye::presets;
  eye::Params thinkingL = ps::thinking();
  eye::Params thinkingR = ps::thinking();
  thinkingL.gaze_x = -eye::kThinkingDrift;  // the drift's extremes
  thinkingR.gaze_x = eye::kThinkingDrift;
  const eye::Params looks[] = {ps::idle(),        ps::happy(),       ps::sleep(),
                               ps::surprised(),   ps::look(-28, 0),  ps::look(28, 0),
                               ps::look(0, -28),  ps::look(0, 28),   ps::closedLine(),
                               ps::skeptical(),   ps::sad(),         ps::listening(),
                               thinkingL,         thinkingR,         ps::lookLeft(),
                               ps::lookRight(),   ps::lookUp(),      ps::lookDown(),
                               ps::small()};
  for (const auto& p : looks) {
    const float w = p.thickness / 2 + 1;
    const float t = p.radius * fabsf(sinf(p.tilt));
    TEST_ASSERT_LESS_OR_EQUAL_FLOAT(100.0f, fabsf(p.gaze_x) + p.radius + w);
    const float up = -fminf(0, fminf(p.upper, p.lower)) * p.radius + w + t - p.gaze_y;
    const float down = fmaxf(0, fmaxf(p.upper, p.lower)) * p.radius + w + t + p.gaze_y;
    TEST_ASSERT_LESS_OR_EQUAL_FLOAT(100.0f, up);
    TEST_ASSERT_LESS_OR_EQUAL_FLOAT(100.0f, down);
  }
}

// A ring blends smoothly into a crescent, and a blink shuts a crescent like a ring.
void test_crescent_blend_and_blink() {
  const eye::Params mid = eye::lerp(eye::presets::idle(), eye::presets::lookLeft(), 0.5f);
  TEST_ASSERT_EQUAL_FLOAT(0.5f, mid.fill);
  TEST_ASSERT_EQUAL_FLOAT(0.425f, mid.cut);
  TEST_ASSERT_EQUAL_FLOAT(0.225f, mid.cut_x);
  const eye::Params shut = eye::closedLike(eye::presets::lookLeft());
  TEST_ASSERT_EQUAL_FLOAT(0.0f, shut.fill);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, shut.cut);
  TEST_ASSERT_FALSE(eye::same(eye::presets::lookLeft(), eye::presets::lookRight()));
}

void test_dc_block_removes_offset() {
  DcBlocker dc;
  int16_t y = 0;
  for (int i = 0; i < 2000; i++) y = dc.process(1000);
  TEST_ASSERT_LESS_THAN_INT(50, abs(y));
}

void test_dc_block_keeps_speech() {
  DcBlocker dc;
  int16_t buf[1600];  // 100 ms of a 1 kHz sine at 16 kHz
  for (size_t i = 0; i < 1600; i++) {
    buf[i] = static_cast<int16_t>(10000 * sinf(2 * PI * 1000 * i / 16000.0f));
  }
  dc.processBlock(buf, 1600);
  int peak = 0;
  for (size_t i = 800; i < 1600; i++) peak = max(peak, abs(static_cast<int>(buf[i])));
  TEST_ASSERT_GREATER_THAN_INT(9500, peak);
}

void test_dc_block_saturates() {
  DcBlocker dc;
  TEST_ASSERT_EQUAL_INT16(32767, dc.process(32767));
  // -32768 - 32767 + 0.995 * 32767 = about -32932 unclamped
  TEST_ASSERT_EQUAL_INT16(-32768, dc.process(-32768));
  // The state holds the clamped value: 0 + 32768 + 0.995 * -32768 = about 164
  // (an unclamped y1 of -32932 would give about 1).
  TEST_ASSERT_INT16_WITHIN(1, 164, dc.process(0));
  DcBlocker up;
  up.process(-32768);
  TEST_ASSERT_EQUAL_INT16(32767, up.process(32767));
}

void test_dc_block_reset() {
  DcBlocker dc;
  for (int i = 0; i < 10; i++) dc.process(5000);
  dc.reset();
  TEST_ASSERT_EQUAL_INT16(0, dc.process(0));
  dc.reset(1000);  // seeded with the stream's first sample: no step from its DC offset
  TEST_ASSERT_EQUAL_INT16(0, dc.process(1000));
  DcBlocker fresh;
  TEST_ASSERT_EQUAL_INT16(1000, fresh.process(1000));  // unseeded, the offset steps through
}

void test_ring_wraps() {
  int16_t store[5];
  Int16Ring r(store, 5);
  const int16_t a[] = {1, 2, 3, 4};
  int16_t out[5] = {0};
  TEST_ASSERT_EQUAL_UINT32(4, r.write(a, 4));
  TEST_ASSERT_EQUAL_UINT32(3, r.read(out, 3));
  TEST_ASSERT_EQUAL_INT16(3, out[2]);
  TEST_ASSERT_EQUAL_UINT32(4, r.write(a, 4));  // wraps round the end
  TEST_ASSERT_EQUAL_UINT32(5, r.available());
  TEST_ASSERT_EQUAL_UINT32(5, r.read(out, 5));
  const int16_t want[] = {4, 1, 2, 3, 4};
  TEST_ASSERT_EQUAL_INT16_ARRAY(want, out, 5);
  TEST_ASSERT_EQUAL_UINT32(0, r.read(out, 5));
}

void test_ring_partial_write_when_full() {
  int16_t store[4];
  Int16Ring r(store, 4);
  const int16_t a[] = {1, 2, 3, 4, 5, 6};
  TEST_ASSERT_EQUAL_UINT32(4, r.write(a, 6));
  TEST_ASSERT_EQUAL_UINT32(0, r.space());
  TEST_ASSERT_EQUAL_UINT32(0, r.write(a, 1));
}

void test_ring_clear_and_counts() {
  int16_t store[8];
  Int16Ring r(store, 8);
  const int16_t a[] = {1, 2, 3};
  r.write(a, 3);
  TEST_ASSERT_EQUAL_UINT32(8, r.available() + r.space());
  r.clear();
  TEST_ASSERT_EQUAL_UINT32(0, r.available());
  TEST_ASSERT_EQUAL_UINT32(8, r.space());
}

void test_backoff() {
  Backoff b;
  const uint32_t want[] = {1000, 2000, 4000, 8000, 16000, 30000, 30000};
  for (uint32_t w : want) TEST_ASSERT_EQUAL_UINT32(w, b.next());
  b.reset();
  TEST_ASSERT_EQUAL_UINT32(1000, b.next());
}

linkmsg::Parsed parse(const char* s) { return linkmsg::parseServerText(s, strlen(s)); }

void test_parse_messages() {
  using linkmsg::Msg;
  TEST_ASSERT_TRUE(parse("{\"type\":\"welcome\"}").type == Msg::Welcome);
  linkmsg::Parsed p = parse("{\"type\":\"state\",\"name\":\"listening\"}");
  TEST_ASSERT_TRUE(p.type == Msg::State);
  TEST_ASSERT_EQUAL_STRING("listening", p.name);
  p = parse("{\"type\":\"emotion\",\"name\":\"happy\"}");
  TEST_ASSERT_TRUE(p.type == Msg::Emotion);
  TEST_ASSERT_EQUAL_STRING("happy", p.name);
  TEST_ASSERT_TRUE(parse("{\"type\":\"audio_start\",\"rate\":16000}").type == Msg::AudioStart);
  TEST_ASSERT_TRUE(parse("{\"type\":\"audio_end\"}").type == Msg::AudioEnd);
  TEST_ASSERT_TRUE(parse("{\"type\":\"audio_stop\"}").type == Msg::AudioStop);
}

void test_parse_truncates_long_name() {
  const linkmsg::Parsed p = parse("{\"type\":\"state\",\"name\":\"abcdefghijklmnopqrstuvwxyz\"}");
  TEST_ASSERT_TRUE(p.type == linkmsg::Msg::State);
  TEST_ASSERT_EQUAL_STRING("abcdefghijklmno", p.name);  // 15 chars + NUL
}

void test_parse_bad_and_unknown() {
  using linkmsg::Msg;
  TEST_ASSERT_TRUE(parse("not json{").type == Msg::Bad);
  TEST_ASSERT_TRUE(parse("[1,2]").type == Msg::Bad);
  TEST_ASSERT_TRUE(parse("{\"name\":\"idle\"}").type == Msg::Bad);
  TEST_ASSERT_TRUE(parse("{\"type\":\"state\"}").type == Msg::Bad);  // name missing
  TEST_ASSERT_TRUE(parse("{\"type\":\"look\"}").type == Msg::Unknown);
  // A length that cuts the JSON short makes it bad.
  TEST_ASSERT_TRUE(linkmsg::parseServerText("{\"type\":\"welcome\"}", 10).type == Msg::Bad);
  // Only the given length counts, even when the buffer carries on.
  TEST_ASSERT_TRUE(linkmsg::parseServerText("{\"type\":\"welcome\"}xx", 18).type == Msg::Welcome);
}

void test_build_hello_round_trips() {
  char out[128];
  const size_t n = linkmsg::buildHello(out, sizeof(out), "tok\"en\\x", "0.2.0");
  TEST_ASSERT_GREATER_THAN_UINT32(0, n);
  TEST_ASSERT_EQUAL_UINT32(strlen(out), n);
  TEST_ASSERT_NOT_NULL(strstr(out, "tok\\\"en\\\\x"));  // escaped
  JsonDocument doc;
  TEST_ASSERT_TRUE(deserializeJson(doc, out) == DeserializationError::Ok);
  TEST_ASSERT_EQUAL_STRING("hello", doc["type"].as<const char*>());
  TEST_ASSERT_EQUAL_STRING("tok\"en\\x", doc["token"].as<const char*>());
  TEST_ASSERT_EQUAL_STRING("0.2.0", doc["fw_version"].as<const char*>());
}

void test_build_hello_too_small() {
  char out[10];
  TEST_ASSERT_EQUAL_UINT32(0, linkmsg::buildHello(out, sizeof(out), "abcdefghijklmnop", "1"));
}

void test_look_for_emotion() {
  TEST_ASSERT_EQUAL_STRING("idle", lookForEmotion("neutral"));
  TEST_ASSERT_EQUAL_STRING("happy", lookForEmotion("happy"));
  TEST_ASSERT_EQUAL_STRING("sad", lookForEmotion("sad"));
  TEST_ASSERT_EQUAL_STRING("surprised", lookForEmotion("surprised"));
  TEST_ASSERT_EQUAL_STRING("sleep", lookForEmotion("sleepy"));
  TEST_ASSERT_EQUAL_STRING("skeptical", lookForEmotion("thinking"));
  TEST_ASSERT_EQUAL_STRING("up", lookForEmotion("curious"));
  TEST_ASSERT_EQUAL_STRING("idle", lookForEmotion("grumpy"));
  TEST_ASSERT_EQUAL_STRING("idle", lookForEmotion(nullptr));
}

void test_look_for_state() {
  TEST_ASSERT_EQUAL_STRING("idle", lookForState("idle", "happy"));
  TEST_ASSERT_EQUAL_STRING("listening", lookForState("listening", "happy"));
  TEST_ASSERT_EQUAL_STRING("thinking", lookForState("thinking", "happy"));
  TEST_ASSERT_EQUAL_STRING("happy", lookForState("speaking", "happy"));
  TEST_ASSERT_EQUAL_STRING("sad", lookForState("speaking", "sad"));
  TEST_ASSERT_EQUAL_STRING("sleep", lookForState("sleeping", "happy"));
  TEST_ASSERT_EQUAL_STRING("sleep", lookForState("", "happy"));
  TEST_ASSERT_EQUAL_STRING("sleep", lookForState(nullptr, nullptr));
}

void setup() {
  delay(2000);  // let the serial port settle
  UNITY_BEGIN();
  RUN_TEST(test_lerp_and_clamp);
  RUN_TEST(test_in_out_cubic);
  RUN_TEST(test_progress_survives_millis_wrap);
  RUN_TEST(test_params_lerp);
  RUN_TEST(test_params_lerp_tilt);
  RUN_TEST(test_closed_like_keeps_size_and_gaze);
  RUN_TEST(test_same);
  RUN_TEST(test_tween);
  RUN_TEST(test_tween_retarget_starts_from_current);
  RUN_TEST(test_tween_set_and_settle);
  RUN_TEST(test_blinker);
  RUN_TEST(test_presets_fit_sprite);
  RUN_TEST(test_crescent_blend_and_blink);
  RUN_TEST(test_blinker_default_is_not_blinking);
  RUN_TEST(test_blinker_symmetric);
  RUN_TEST(test_blinker_survives_millis_wrap);
  RUN_TEST(test_mic_to_16);
  RUN_TEST(test_to_db);
  RUN_TEST(test_level);
  RUN_TEST(test_is_flat);
  RUN_TEST(test_fade_gain);
  RUN_TEST(test_fade_gain_edges);
  RUN_TEST(test_scale_volume);
  RUN_TEST(test_tone_continues_phase);
  RUN_TEST(test_tone_values);
  RUN_TEST(test_servo_pulse);
  RUN_TEST(test_servo_clamp);
  RUN_TEST(test_servo_motion);
  RUN_TEST(test_servo_motion_bad_speed);
  RUN_TEST(test_servo_motion_reverse);
  RUN_TEST(test_servo_motion_across_millis_wrap);
  RUN_TEST(test_dc_block_removes_offset);
  RUN_TEST(test_dc_block_keeps_speech);
  RUN_TEST(test_dc_block_saturates);
  RUN_TEST(test_dc_block_reset);
  RUN_TEST(test_ring_wraps);
  RUN_TEST(test_ring_partial_write_when_full);
  RUN_TEST(test_ring_clear_and_counts);
  RUN_TEST(test_backoff);
  RUN_TEST(test_parse_messages);
  RUN_TEST(test_parse_truncates_long_name);
  RUN_TEST(test_parse_bad_and_unknown);
  RUN_TEST(test_build_hello_round_trips);
  RUN_TEST(test_build_hello_too_small);
  RUN_TEST(test_look_for_emotion);
  RUN_TEST(test_look_for_state);
  UNITY_END();
}

void loop() {}
