#pragma once
// "Hey CARL" wake word engine (Phase 0: the feasibility test). The TFLM audio microfrontend
// turns 10 ms mic frames into 40 features, and the microWakeWord streaming model (TFLite
// Micro, both arenas in internal RAM, sized at begin()) gives a probability per model step. It runs on its own
// task on core 0. The model is compiled in from models/hey_carl_model.h (see
// scripts/model_to_header.py); without it everything builds and begin() says so.
//
// The mic: while run(true) is on, the wake task is the mic's only reader. The serial menu
// refuses `wake` while the robot link is connected (the link's mic stream owns it then).
#include <stddef.h>
#include <stdint.h>

namespace wake {

bool haveModel();  // a model was compiled in

// Loads the model, allocates the arena and starts the (paused) task, once. Returns nullptr
// when ready, else a message saying what's wrong (also on later calls).
const char* begin();

// Prints the model: wake word, size, operators, input/output shapes and quantisation, arena.
void printInfo();

// on: flush the mic, then detect continuously. off: returns once the task has let go of
// the mic (within ~100 ms).
void run(bool on);
bool running();

// Counters since the last takeStats() call.
struct Stats {
  uint32_t ms = 0;           // wall time covered
  uint32_t frames = 0;       // 10 ms audio frames processed
  uint32_t invokes = 0;      // model steps
  uint32_t invokeUsAvg = 0;  // per model step
  uint32_t invokeUsMax = 0;
  uint32_t frontendUsAvg = 0;  // per 10 ms frame
  float load = 0;              // share of core 0 the wake task used (0-1)
  uint32_t shortReads = 0;     // mic reads that came back short
  uint32_t invokeErrors = 0;   // model steps that failed
};
Stats takeStats();

// The newest model probability and the sliding window's mean (0-1).
float probability();
float score();

// True once per detection (the window's mean reached the cutoff), with that mean.
bool takeDetection(float* score);

}  // namespace wake
