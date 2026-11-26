#pragma once
// I2S speaker (MAX98357A) and mic (INMP441), both 16 kHz 16-bit mono.
//
// Two ways to use them:
// - The M5 blocking functions (play, playTone, readMic, flushMic) for the serial tests.
// - Streaming for the robot link: startStreaming() starts a playback task that drains a
//   PSRAM ring into the speaker, and the mic reader (startMic()).
//
// Who reads the mic (W2): while the reader runs (any mode but Off) it is the I2S mic's only
// reader, and it hands each DC-blocked 20 ms frame to whoever the mode says:
// - Off:    nobody reads; the blocking tests (mic, rec) may.
// - Wake:   the wake engine (setWakeSink), and the 3 s pre-roll ring as history.
// - Hold:   only the pre-roll: a wake was sent, the server hasn't said `listening` yet.
// - Stream: the link (popMicFrame). Coming from Hold, the pre-roll's kept frames go first
//           and live frames queue behind them in the ring until it has drained. The link
//           takes frames only in Stream mode, so nothing goes out after `listening` ends.
// The rules are pure, in include/mic_route.h.
// So the wake engine and the link never read the mic themselves, and switching between them
// loses no audio (no flush, the DC blocker keeps running). Only a start from Off flushes.
// The pre-roll's audio is sent only in that Hold -> Stream backlog, never in Wake mode.
//
// The blocking functions must not overlap the reader: main.cpp refuses tone/mic/rec/play
// while the link is connected, the link sets the mode to Off when it drops, and wake test
// waits for the reader to let go with waitMicIdle(). Playback touches I2S only while there
// is audio to play.
#include <stddef.h>
#include <stdint.h>

#include "mic_route.h"

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

// Mic -> wake engine / server.
constexpr size_t kFrameSamples = kFrameBytes / 2;
using MicMode = microute::Mode;
using MicSink = void (*)(const int16_t* pcm, size_t n, uint32_t seq);

bool startMic();              // the reader task and the pre-roll ring, once; true if running
void setMicMode(MicMode m);   // from the main loop; see the comment at the top
MicMode micMode();
void waitMicIdle();           // after Off: returns once the reader has let go (<= 200 ms)
void setWakeSink(MicSink s);  // gets each frame in Wake mode, with its pre-roll number
void keepPreRollFrom(uint32_t seq);  // a detection: drop the pre-roll older than `seq`
void dropPreRoll();           // the wake went unanswered: forget the kept audio
bool micBacklog();            // kept pre-roll audio is still waiting to be sent
bool popMicFrame(uint8_t out[kFrameBytes]);  // the next frame to send, if any (never blocks)
uint32_t micDropped();        // frames lost: queue full, or kept pre-roll overwritten

}  // namespace audio
