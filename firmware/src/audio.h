#pragma once
// I2S speaker (MAX98357A), 16 kHz 16-bit mono. The mic (INMP441) joins in Task 6.
#include <stddef.h>
#include <stdint.h>

namespace audio {

bool beginSpeaker();
void setVolume(int percent);  // 0-100, clamped; starts at 20 (5 V into 3 W is loud)
int volume();
void play(const int16_t* pcm, size_t n);  // blocking, scaled by volume
void playTone(float hz, uint32_t ms);     // blocking, with 10 ms fades
uint32_t writeErrors();                   // short I2S writes since boot

}  // namespace audio
