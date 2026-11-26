#include "audio.h"

#include <Arduino.h>
#include <ESP_I2S.h>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <string.h>

#include <algorithm>
#include <atomic>

#include "dc_block.h"
#include "dsp.h"
#include "pins.h"
#include "pre_roll.h"
#include "ring_buffer.h"

namespace {

constexpr size_t kBlock = dsp::kRate / 50;  // 20 ms
// Explicit ports, so the speaker and mic never compete for one.
I2SClass speaker(I2S_NUM_0);
bool speakerOk = false;
I2SClass mic(I2S_NUM_1);
bool micOk = false;
constexpr size_t kMicDmaFrames = 6 * 240;  // ESP_I2S: 6 DMA buffers of 240 frames (90 ms)
int32_t micRaw[kBlock];
std::atomic<int> vol{20};  // read by the playback task
uint32_t errors = 0;

void write(const int16_t* block, size_t n) {
  const size_t bytes = n * sizeof(int16_t);
  if (speaker.write(reinterpret_cast<const uint8_t*>(block), bytes) != bytes) errors++;
}

// ESP_I2S turns on auto_clear, so the DMA sends zeros once it runs out of data. Writing
// silence ourselves is for timing: a blocking write returns as soon as the data is queued,
// so trailing silence makes playTone() return only once the sound has actually played.
void writeSilence(uint32_t ms) {
  static const int16_t zeros[kBlock] = {0};
  for (uint32_t done = 0; done < ms; done += 20) write(zeros, kBlock);
}

}  // namespace

namespace audio {

bool beginSpeaker() {
  speaker.setPins(pins::kAmpBclk, pins::kAmpLrc, pins::kAmpDin);
  // Mono data on both slots, so the amp's (L+R)/2 mix gets full level.
  speakerOk = speaker.begin(I2S_MODE_STD, dsp::kRate, I2S_DATA_BIT_WIDTH_16BIT,
                            I2S_SLOT_MODE_MONO, I2S_STD_SLOT_BOTH);
  if (speakerOk) writeSilence(100);  // a clean start clears the amp's power-on pop
  return speakerOk;
}

bool beginMic() {
  mic.setPins(pins::kMicSck, pins::kMicWs, -1, pins::kMicSd);
  // INMP441 with L/R to GND: 24-bit samples in 32-bit slots, left slot only.
  micOk = mic.begin(I2S_MODE_STD, dsp::kRate, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO,
                    I2S_STD_SLOT_LEFT);
  return micOk;
}

size_t readMic(int16_t* out, size_t n) {
  if (!micOk) return 0;
  size_t done = 0;
  while (done < n) {
    const size_t want = std::min(kBlock, n - done);
    const size_t got = mic.readBytes(reinterpret_cast<char*>(micRaw), want * sizeof(int32_t)) /
                       sizeof(int32_t);
    if (got == 0) break;
    for (size_t j = 0; j < got; j++) out[done + j] = dsp::micTo16(micRaw[j]);
    done += got;
  }
  return done;
}

void flushMic() {
  if (!micOk) return;
  // The DMA keeps recording while nobody reads; drop that stale audio (up to ~100 ms).
  for (size_t done = 0; done < kMicDmaFrames;) {
    const size_t want = std::min(kBlock, kMicDmaFrames - done);
    const size_t got = mic.readBytes(reinterpret_cast<char*>(micRaw), want * sizeof(int32_t)) /
                       sizeof(int32_t);
    if (got == 0) break;
    done += got;
  }
}

bool speakerReady() { return speakerOk; }

void setVolume(int percent) { vol = std::clamp(percent, 0, 100); }

int volume() { return vol; }

uint32_t writeErrors() { return errors; }

// No trailing silence here: M6 streams speech in chunks, and a gap per chunk would be heard.
void play(const int16_t* pcm, size_t n) {
  if (!speakerOk) return;
  int16_t block[kBlock];
  for (size_t i = 0; i < n; i += kBlock) {
    const size_t m = std::min(kBlock, n - i);
    for (size_t j = 0; j < m; j++) block[j] = dsp::scale(pcm[i + j], vol);
    write(block, m);
  }
}

void playTone(float hz, uint32_t ms) {
  if (!speakerOk) return;
  const size_t total = static_cast<size_t>(static_cast<uint64_t>(dsp::kRate) * ms / 1000);
  const size_t fade = dsp::kRate / 100;  // 10 ms
  int16_t block[kBlock];
  float phase = 0;
  for (size_t i = 0; i < total; i += kBlock) {
    const size_t m = std::min(kBlock, total - i);
    dsp::tone(block, m, hz, dsp::kToneAmp, phase);
    for (size_t j = 0; j < m; j++) {
      const float g = dsp::fadeGain(i + j, total, fade);
      block[j] = dsp::scale(static_cast<int16_t>(block[j] * g), vol);
    }
    write(block, m);
  }
  writeSilence(100);  // return only after the tone has finished playing
}

}  // namespace audio

// ---------------------------------------------------------------------------------------
// Streaming: a playback task drains a PSRAM ring into the speaker, and the mic reader hands
// DC-blocked 20 ms frames to the wake engine, the pre-roll or the link (see audio.h). Both
// run on core 1; the WebSocket and the eyes stay in the main loop, the wake engine on core 0.
namespace {

constexpr size_t kRingSamples = dsp::kRate;  // 1 s; the server paces only ~200 ms ahead
constexpr size_t kMicQueueDepth = 8;         // 160 ms of frames
// 3 s of 20 ms frames (96 KB of PSRAM): the 300 ms lead, a 2 s wait for the server's answer
// and the backlog's drain all fit without overwriting audio that is still to be sent.
constexpr size_t kPreRollFrames = 150;
constexpr UBaseType_t kPlaybackPriority = 5;
constexpr UBaseType_t kMicPriority = 4;
constexpr BaseType_t kAudioCore = 1;

// Each sentence waits for this much audio before it starts, so network jitter right at the
// start doesn't cause a stutter. audio_end plays a shorter sentence anyway.
constexpr size_t kPrimeSamples = dsp::kRate * 60 / 1000;
constexpr size_t kStopFadeSamples = dsp::kRate * 5 / 1000;  // fade-out on an interrupt

Int16Ring* ring = nullptr;
portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;
std::atomic<bool> accepting{false};  // between streamBegin() and streamStop()
std::atomic<bool> priming{false};    // a sentence began and hasn't started playing yet
std::atomic<bool> ended{false};      // audio_end arrived: play whatever is buffered
std::atomic<uint32_t> stopGen{0};    // bumped by streamStop(): cut the block in hand
bool tried = false;                  // startStreaming() has run (it runs once)
bool playbackOn = false;
bool micTried = false;               // startMic() has run (it runs once)
bool micOn = false;

static_assert(kBlock * sizeof(int16_t) == audio::kFrameBytes, "a mic frame is one block");
static_assert(kBlock == audio::kFrameSamples, "a mic frame is one block");

QueueHandle_t micQueue = nullptr;
std::atomic<audio::MicMode> mode{audio::MicMode::Off};
std::atomic<uint32_t> micIdleTicks{0};  // bumped each time the reader sees Off
std::atomic<uint32_t> micDrops{0};
std::atomic<audio::MicSink> wakeSink{nullptr};

// The pre-roll and the backlog flag, under preMux (the reader and the main loop share them).
FrameRing* preRoll = nullptr;
portMUX_TYPE preMux = portMUX_INITIALIZER_UNLOCKED;
bool backlog = false;    // Stream after Hold: the pre-roll goes first, live frames behind it
uint32_t noRingSeq = 0;  // frame numbers if the pre-roll couldn't be allocated

void playbackTask(void*) {
  int16_t block[kBlock];
  for (;;) {
    const uint32_t gen = stopGen.load();
    size_t n = 0;
    portENTER_CRITICAL(&ringMux);
    const bool wait = priming.load() && !ended.load() && ring->available() < kPrimeSamples;
    if (!wait) n = ring->read(block, kBlock);
    portEXIT_CRITICAL(&ringMux);
    if (n == 0) {
      vTaskDelay(pdMS_TO_TICKS(5));  // starved: auto_clear plays silence meanwhile
      continue;
    }
    priming = false;
    const int v = vol.load();
    for (size_t i = 0; i < n; i++) block[i] = dsp::scale(block[i], v);
    if (stopGen.load() != gen) {
      // Interrupted while this block was in hand: fade out its start instead of a click.
      n = std::min(n, kStopFadeSamples);
      for (size_t i = 0; i < n; i++) {
        block[i] = static_cast<int16_t>(block[i] * (1.0f - static_cast<float>(i + 1) / n));
      }
    }
    write(block, n);  // blocks while the DMA is full, which paces this loop
  }
}

// One frame to whoever the mode says (rules in mic_route.h).
void deliver(audio::MicMode m, const int16_t* frame) {
  bool lost = false;
  uint32_t seq = 0;
  portENTER_CRITICAL(&preMux);
  const microute::Dest d =
      microute::route(m, preRoll != nullptr, preRoll ? preRoll->count() : 0, backlog);
  if (d == microute::Dest::History || d == microute::Dest::Keep) {
    seq = preRoll->push(frame, &lost);
    if (d == microute::Dest::History) lost = false;  // overwriting history is normal
  } else if (preRoll == nullptr) {
    seq = noRingSeq++;
  }
  portEXIT_CRITICAL(&preMux);
  if (lost) micDrops++;
  if (d == microute::Dest::Queue && xQueueSend(micQueue, frame, 0) != pdTRUE) micDrops++;
  if (microute::feedsEngine(m)) {
    const audio::MicSink sink = wakeSink.load();
    if (sink != nullptr) sink(frame, kBlock, seq);
  }
}

void micTask(void*) {
  int16_t frame[kBlock];
  DcBlocker dc;
  bool reading = false;  // false: the next frame starts afresh (flush, seed the DC blocker)
  bool seed = false;
  for (;;) {
    const audio::MicMode m = mode.load();
    if (m == audio::MicMode::Off) {
      reading = false;
      micIdleTicks++;
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    if (!reading) {
      audio::flushMic();  // drop what the DMA recorded while nobody listened
      reading = true;
      seed = true;
    }
    if (audio::readMic(frame, kBlock) != kBlock) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    // Seed with the first sample, so the DC offset of the mic doesn't start with a thump.
    if (seed) {
      dc.reset(frame[0]);
      seed = false;
    }
    dc.processBlock(frame, kBlock);
    deliver(m, frame);
  }
}

}  // namespace

namespace audio {

namespace {

bool startPlayback() {
  if (!speakerOk) return false;
  auto* store = static_cast<int16_t*>(ps_malloc(kRingSamples * sizeof(int16_t)));
  if (store == nullptr) return false;
  ring = new Int16Ring(store, kRingSamples);
  if (xTaskCreatePinnedToCore(playbackTask, "playback", 4096, nullptr, kPlaybackPriority,
                              nullptr, kAudioCore) != pdPASS) {
    delete ring;
    ring = nullptr;
    free(store);
    return false;
  }
  return true;
}

}  // namespace

// Runs once: each half starts if its hardware started; a failure is reported once and not
// retried (so nothing is allocated or spawned twice).
bool startStreaming() {
  if (tried) return playbackOn && micOn;
  tried = true;
  playbackOn = startPlayback();
  startMic();
  if (!playbackOn) Serial.println("Audio: streamed playback didn't start (speaker or memory)");
  if (!micOn) Serial.println("Audio: mic streaming didn't start (mic or memory)");
  return playbackOn && micOn;
}

bool streaming() { return tried; }

void streamBegin() {
  ended = false;
  priming = true;
  accepting = true;
}

void streamPush(const uint8_t* pcm, size_t len) {
  if (!playbackOn || !accepting) return;
  len &= ~static_cast<size_t>(1);  // an odd trailing byte isn't a sample
  // Copy through an aligned buffer: the payload may not be 2-byte aligned.
  int16_t chunk[kBlock];
  while (len > 0) {
    const size_t bytes = std::min(len, sizeof(chunk));
    memcpy(chunk, pcm, bytes);
    const size_t n = bytes / sizeof(int16_t);
    portENTER_CRITICAL(&ringMux);
    if (ring->space() < n) ring->drop(n - ring->space());  // full: lose the oldest
    ring->write(chunk, n);
    portEXIT_CRITICAL(&ringMux);
    pcm += bytes;
    len -= bytes;
  }
}

void streamEnd() { ended = true; }  // what's in the ring plays out, even if under kPrime

void streamStop() {
  accepting = false;
  priming = false;
  if (!playbackOn) return;
  stopGen++;
  portENTER_CRITICAL(&ringMux);
  ring->clear();
  portEXIT_CRITICAL(&ringMux);
}

bool startMic() {
  if (micTried) return micOn;
  micTried = true;
  if (!micOk) return false;
  // The pre-roll is optional: without it wake mode still works, it just can't send the audio
  // between the detection and the server's `listening`.
  auto* store = static_cast<int16_t*>(ps_malloc(kPreRollFrames * kFrameBytes));
  if (store != nullptr) {
    preRoll = new FrameRing(store, kPreRollFrames, kBlock);
  } else {
    Serial.println("Audio: no PSRAM for the wake word's pre-roll");
  }
  micQueue = xQueueCreate(kMicQueueDepth, kFrameBytes);
  if (micQueue == nullptr) return false;
  if (xTaskCreatePinnedToCore(micTask, "mic", 6144, nullptr, kMicPriority, nullptr,
                              kAudioCore) != pdPASS) {
    vQueueDelete(micQueue);
    micQueue = nullptr;
    return false;
  }
  micOn = true;
  return true;
}

void setMicMode(MicMode m) {
  if (!micOn) return;
  const MicMode was = mode.load();
  if (m == was) return;
  const microute::Change c = microute::onModeChange(was, m);
  portENTER_CRITICAL(&preMux);
  backlog = c.backlog;
  if (c.clearRing && preRoll != nullptr) preRoll->clear();
  portEXIT_CRITICAL(&preMux);
  if (c.resetQueue) xQueueReset(micQueue);
  mode = m;
}

MicMode micMode() { return mode.load(); }

void waitMicIdle() {
  if (!micOn || mode.load() != MicMode::Off) return;
  const uint32_t ticks = micIdleTicks.load();
  const uint32_t start = millis();
  while (micIdleTicks.load() == ticks && millis() - start < 200) delay(2);
}

void setWakeSink(MicSink s) { wakeSink = s; }

void keepPreRollFrom(uint32_t seq) {
  portENTER_CRITICAL(&preMux);
  if (preRoll != nullptr) preRoll->keepFrom(seq);
  portEXIT_CRITICAL(&preMux);
}

void dropPreRoll() {
  portENTER_CRITICAL(&preMux);
  if (preRoll != nullptr) preRoll->clear();
  backlog = false;
  portEXIT_CRITICAL(&preMux);
}

bool micBacklog() {
  portENTER_CRITICAL(&preMux);
  const bool b = backlog && preRoll != nullptr && preRoll->count() > 0;
  portEXIT_CRITICAL(&preMux);
  return b;
}

bool popMicFrame(uint8_t out[kFrameBytes]) {
  // Only while streaming: a frame the reader queued just as `listening` ended stays put (and
  // the next stream empties the queue first).
  if (!microute::mayPop(mode.load())) return false;
  bool got = false;
  int16_t frame[kBlock];
  portENTER_CRITICAL(&preMux);
  if (backlog && preRoll != nullptr) got = preRoll->pop(frame);
  portEXIT_CRITICAL(&preMux);
  if (got) {
    memcpy(out, frame, kFrameBytes);
    return true;
  }
  return micQueue != nullptr && xQueueReceive(micQueue, out, 0) == pdTRUE;
}

uint32_t micDropped() { return micDrops.load(); }

}  // namespace audio
