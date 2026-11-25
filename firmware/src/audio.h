#pragma once
// I2S speaker (MAX98357A) and mic (INMP441), both 16 kHz 16-bit mono.
//
// Two ways to use them:
// - The M5 blocking functions (play, playTone, readMic, flushMic) for the serial tests.
// - Streaming for the robot link: startStreaming() starts a playback task that drains a
//   PSRAM ring into the speaker, and a mic task that queues 20 ms frames.
//
// They must not overlap. startStreaming() is called only when the link first connects, and
// main.cpp refuses tone/mic/rec/play while the link is connected. When the link drops,
// streamStop() and setMicStreaming(false) leave both tasks idle (they touch I2S only while
// there is audio to play or the mic is streaming), so the blocking tests work again.
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

// --- Streaming (the robot link). Call these from the main loop. ---
constexpr size_t kFrameBytes = 640;  // 20 ms of 16 kHz 16-bit mono

// Runs once (later calls just report): starts playback if the speaker works and the mic
// stream if the mic works, independently. True if both are running.
bool startStreaming();
bool streaming();  // startStreaming() has run

// Server -> speaker.
void streamBegin();  // audio_start: accept frames; playback starts once ~60 ms is buffered
void streamPush(const uint8_t* pcm, size_t);  // a binary frame; drops the oldest when full
void streamEnd();   // audio_end: the ring plays out (even a sentence shorter than 60 ms)
void streamStop();  // audio_stop: clear, fade out the block in hand, drop until Begin

// Mic -> server.
void setMicStreaming(bool on);  // on: flush stale audio, reset the DC blocker, start queueing
bool popMicFrame(uint8_t out[kFrameBytes]);  // a queued frame, if any (never blocks)
uint32_t micDropped();                       // frames dropped because the queue was full

}  // namespace audio
