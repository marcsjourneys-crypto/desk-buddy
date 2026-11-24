#include "audio.h"

#include <Arduino.h>
#include <ESP_I2S.h>

#include <algorithm>

#include "dsp.h"
#include "pins.h"

namespace {

constexpr size_t kBlock = dsp::kRate / 50;  // 20 ms
I2SClass speaker;
bool speakerOk = false;
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
