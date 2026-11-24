#pragma once
// I2S speaker (MAX98357A) and mic (INMP441), both 16 kHz 16-bit mono.
#include <stddef.h>
#include <stdint.h>

namespace audio {

bool beginSpeaker();
bool beginMic();
void setVolume(int percent);  // 0-100, clamped; starts at 20 (5 V into 3 W is loud)
int volume();
void play(const int16_t* pcm, size_t n);  // blocking, scaled by volume
void playTone(float hz, uint32_t ms);     // blocking, with 10 ms fades
size_t readMic(int16_t* out, size_t n);   // blocking; samples read (0 = mic not started)
void flushMic();                          // drop stale audio queued since the last read
bool speakerReady();                      // false if beginSpeaker() failed
uint32_t writeErrors();                   // short I2S writes since boot

}  // namespace audio
