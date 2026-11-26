#pragma once
// "Hey CARL" wake word engine. The TFLM audio microfrontend turns mic audio into 40 features
// every 10 ms, and the microWakeWord streaming model (TFLite Micro, both arenas in internal
// RAM, sized at begin()) gives a probability per model step. It runs on its own
// task on core 0. The model is compiled in from models/hey_carl_model.h (see
// scripts/model_to_header.py); without it everything builds and begin() says so.
//
// The mic: the wake task never reads it. The mic reader (audio.cpp) feeds it 20 ms frames in
// audio::MicMode::Wake; run(true) makes the task process them. Wake mode (net.cpp) decides
// when; `wake test`/`wake raw` (main.cpp) do the same by hand while the link is down.
#include <stddef.h>
#include <stdint.h>

namespace wake {

bool haveModel();  // a model was compiled in

// Loads the model, allocates the arena and starts the (paused) task, once. Returns nullptr
// when ready, else a message saying what's wrong (also on later calls).
const char* begin();

// Prints the model: wake word, size, operators, input/output shapes and quantisation, arena.
void printInfo();

bool ready();  // begin() worked: the task runs

// The model's own cutoff (the manifest's), and the threshold detections use now. A value
// outside 0.5-0.99 is ignored. Takes effect at the next model step.
float modelCutoff();
void setThreshold(float t);
float currentThreshold();

// on: process the frames the mic reader feeds (a fresh start: frontend reset, a 2 s
// hold-off); off: throw them away. Detections from before run(true) are forgotten.
void run(bool on);
bool running();

// Counters since the last takeStats() call.
struct Stats {
  uint32_t ms = 0;           // wall time covered
  uint32_t frames = 0;       // 20 ms mic frames processed
  uint32_t invokes = 0;      // model steps
  uint32_t invokeUsAvg = 0;  // per model step
  uint32_t invokeUsMax = 0;
  uint32_t frontendUsAvg = 0;  // per 20 ms frame
  float load = 0;              // share of core 0 the wake task used (0-1)
  uint32_t dropped = 0;        // frames lost because the task fell behind
  uint32_t invokeErrors = 0;   // model steps that failed
};
Stats takeStats();

// The newest model probability and the sliding window's mean (0-1).
float probability();
float score();

// True once per detection (the window's mean reached the threshold), with that mean and the
// pre-roll number of the mic frame that was being processed when it fired.
bool takeDetection(float* score, uint32_t* seq = nullptr);

}  // namespace wake
