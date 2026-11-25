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
int vol = 20;
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
// Streaming: a playback task drains a PSRAM ring into the speaker, and a mic task queues
// DC-blocked 20 ms frames for the main loop to send. Both run on core 1; the WebSocket and
// the eyes stay in the main loop.
namespace {

constexpr size_t kRingSamples = dsp::kRate;  // 1 s; the server paces only ~200 ms ahead
constexpr size_t kMicQueueDepth = 8;         // 160 ms of frames
constexpr UBaseType_t kPlaybackPriority = 5;
constexpr UBaseType_t kMicPriority = 4;
constexpr BaseType_t kAudioCore = 1;

Int16Ring* ring = nullptr;
portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;
std::atomic<bool> accepting{false};  // between streamBegin() and streamStop()
bool started = false;

static_assert(kBlock * sizeof(int16_t) == audio::kFrameBytes, "a mic frame is one block");

QueueHandle_t micQueue = nullptr;
std::atomic<bool> micWanted{false};
std::atomic<uint32_t> micStarts{0};  // bumped per setMicStreaming(true): a fresh flush
std::atomic<uint32_t> micDrops{0};

void playbackTask(void*) {
  int16_t block[kBlock];
  for (;;) {
    portENTER_CRITICAL(&ringMux);
    const size_t n = ring->read(block, kBlock);
    portEXIT_CRITICAL(&ringMux);
    if (n == 0) {
      vTaskDelay(pdMS_TO_TICKS(5));  // starved: auto_clear plays silence meanwhile
      continue;
    }
    const int v = vol;
    for (size_t i = 0; i < n; i++) block[i] = dsp::scale(block[i], v);
    write(block, n);  // blocks while the DMA is full, which paces this loop
  }
}

void micTask(void*) {
  int16_t frame[kBlock];
  DcBlocker dc;
  uint32_t seen = 0;  // the micStarts this stream belongs to
  bool seed = false;  // the next frame starts a stream
  for (;;) {
    if (!micWanted.load()) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    const uint32_t starts = micStarts.load();
    if (starts != seen) {
      audio::flushMic();  // drop what the DMA recorded while nobody listened
      seen = starts;
      seed = true;
    }
    if (audio::readMic(frame, kBlock) != kBlock) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    // Seed with the first sample, so the mic's DC offset doesn't start with a thump.
    if (seed) {
      dc.reset(frame[0]);
      seed = false;
    }
    dc.processBlock(frame, kBlock);
    if (xQueueSend(micQueue, frame, 0) != pdTRUE) micDrops++;
  }
}

}  // namespace

namespace audio {

bool startStreaming() {
  if (started) return true;
  if (!speakerOk || !micOk) return false;
  auto* store = static_cast<int16_t*>(ps_malloc(kRingSamples * sizeof(int16_t)));
  if (store == nullptr) return false;
  ring = new Int16Ring(store, kRingSamples);
  micQueue = xQueueCreate(kMicQueueDepth, kFrameBytes);
  if (micQueue == nullptr) return false;
  if (xTaskCreatePinnedToCore(playbackTask, "playback", 4096, nullptr, kPlaybackPriority,
                              nullptr, kAudioCore) != pdPASS ||
      xTaskCreatePinnedToCore(micTask, "micstream", 4096, nullptr, kMicPriority, nullptr,
                              kAudioCore) != pdPASS) {
    return false;  // out of memory at boot-ish time: not worth unwinding
  }
  started = true;
  return true;
}

bool streaming() { return started; }

void streamBegin() { accepting = true; }

void streamPush(const uint8_t* pcm, size_t len) {
  if (!started || !accepting) return;
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

void streamEnd() {}  // nothing to do: what's in the ring plays out

void streamStop() {
  accepting = false;
  if (!started) return;
  portENTER_CRITICAL(&ringMux);
  ring->clear();
  portEXIT_CRITICAL(&ringMux);
}

void setMicStreaming(bool on) {
  if (!started) return;
  if (on && !micWanted.load()) {
    xQueueReset(micQueue);  // no stale frames from last time
    micStarts++;
  }
  micWanted = on;
}

bool popMicFrame(uint8_t out[kFrameBytes]) {
  return micQueue != nullptr && xQueueReceive(micQueue, out, 0) == pdTRUE;
}

uint32_t micDropped() { return micDrops.load(); }

}  // namespace audio
