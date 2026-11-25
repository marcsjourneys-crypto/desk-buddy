#include "wake.h"

// TFLM and the microfrontend first, before Arduino.h's macros.
#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tensorflow/lite/schema/schema_utils.h"

#include <Arduino.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <stdarg.h>
#include <stdio.h>

#include <atomic>
#include <new>

#include "audio.h"
#include "dc_block.h"
#include "wake_logic.h"

// The model is optional at build time: scripts/model_to_header.py writes this header.
#if __has_include("../models/hey_carl_model.h")
#include "../models/hey_carl_model.h"
#define WAKE_HAVE_MODEL 1
#else
#define WAKE_HAVE_MODEL 0
#endif

namespace {

// --- The microfrontend, set up exactly as microWakeWord's training does
// (microwakeword/audio/audio_utils.py: tf audio_microfrontend with sample_rate 16000,
// window_size 30, window_step 10, num_channels 40, upper_band_limit 7500,
// lower_band_limit 125, enable_pcan True, min_signal_remaining 0.05, out_scale 1,
// out_type uint16). The rest are the op's defaults, which equal the C library's
// *FillConfigWithDefaults() values in lib/microfrontend (TFLM 5d5a2e9); all are set
// explicitly here so nothing depends on a default changing.
constexpr int kSampleRate = 16000;
constexpr size_t kFrameSamples = kSampleRate / 100;  // 10 ms = the frontend's step
constexpr int kChannels = 40;

bool startFrontend(FrontendState* state) {
  FrontendConfig c;
  FrontendFillConfigWithDefaults(&c);
  c.window.size_ms = 30;
  c.window.step_size_ms = 10;
  c.filterbank.num_channels = kChannels;
  c.filterbank.lower_band_limit = 125.0f;
  c.filterbank.upper_band_limit = 7500.0f;
  // Noise reduction: TFLM defaults (noise_reduction_util.c), min_signal_remaining as trained.
  c.noise_reduction.smoothing_bits = 10;
  c.noise_reduction.even_smoothing = 0.025f;
  c.noise_reduction.odd_smoothing = 0.06f;
  c.noise_reduction.min_signal_remaining = 0.05f;
  // PCAN on (trained so); strength, offset and gain bits are TFLM defaults
  // (pcan_gain_control_util.c).
  c.pcan_gain_control.enable_pcan = 1;
  c.pcan_gain_control.strength = 0.95f;
  c.pcan_gain_control.offset = 80.0f;
  c.pcan_gain_control.gain_bits = 21;
  // Log scale: TFLM defaults (log_scale_util.c). out_scale 1 means no further scaling.
  c.log_scale.enable_log = 1;
  c.log_scale.scale_shift = 6;
  return FrontendPopulateState(&c, state, kSampleRate) != 0;
}

// --- The model.
// The ops microWakeWord's streaming models use (the streaming state lives in resource
// variables: CALL_ONCE initialises them, VAR_HANDLE/READ_VARIABLE/ASSIGN_VARIABLE carry them
// between steps), plus a few close relatives so a retrained model rarely needs a new
// firmware. `wake info` lists the model's ops and marks any that aren't registered;
// scripts/model_to_header.py --ops lists them on the PC. To add one: raise kOpCount and add
// it below.
constexpr unsigned kOpCount = 22;
using Resolver = tflite::MicroMutableOpResolver<kOpCount>;
Resolver resolver;

bool registerOps() {
  const TfLiteStatus results[] = {
      resolver.AddCallOnce(),       resolver.AddVarHandle(),     resolver.AddReadVariable(),
      resolver.AddAssignVariable(), resolver.AddReshape(),       resolver.AddStridedSlice(),
      resolver.AddConcatenation(),  resolver.AddConv2D(),        resolver.AddDepthwiseConv2D(),
      resolver.AddFullyConnected(), resolver.AddLogistic(),      resolver.AddQuantize(),
      resolver.AddDequantize(),     resolver.AddMul(),           resolver.AddAdd(),
      resolver.AddMean(),           resolver.AddAveragePool2D(), resolver.AddMaxPool2D(),
      resolver.AddPad(),            resolver.AddPack(),          resolver.AddSplit(),
      resolver.AddSplitV(),
  };
  static_assert(sizeof(results) / sizeof(results[0]) == kOpCount, "kOpCount = ops added");
  for (TfLiteStatus s : results) {
    if (s != kTfLiteOk) return false;
  }
  return true;
}

// The streaming state (resource variables) gets its own small arena. Both arenas are in
// PSRAM; if the step time is too slow, moving the tensor arena to internal RAM is the
// first thing to try.
constexpr size_t kVarArenaBytes = 32 * 1024;
constexpr int kMaxResourceVariables = 32;
constexpr size_t kMaxArenaBytes = 256 * 1024;  // the arena doubles up to this if too small

constexpr UBaseType_t kTaskPriority = 4;  // below Wi-Fi (23) and lwIP (18) on core 0
constexpr BaseType_t kTaskCore = 0;
constexpr uint32_t kTaskStack = 8192;

#if WAKE_HAVE_MODEL
const uint8_t* const kModelData = wake_model::kModel;
constexpr float kCutoff = wake_model::kProbabilityCutoff;
constexpr size_t kWindow = wake_model::kSlidingWindowSize;
constexpr size_t kArenaBytes = wake_model::kTensorArenaSize;
constexpr const char* kWakeWord = wake_model::kWakeWord;
constexpr size_t kModelBytes = wake_model::kModelSize;
#else
const uint8_t* const kModelData = nullptr;
constexpr float kCutoff = 0.97f;
constexpr size_t kWindow = 5;
constexpr size_t kArenaBytes = 0;
constexpr const char* kWakeWord = "(none)";
constexpr size_t kModelBytes = 0;
#endif

bool tried = false;
bool ready = false;  // start() finished: the task runs and the tensors are checked
char error[200] = "";
const tflite::Model* model = nullptr;
uint8_t* arena = nullptr;
size_t arenaBytes = 0;
uint8_t* varArena = nullptr;
tflite::MicroInterpreter* interpreter = nullptr;
TfLiteTensor* input = nullptr;
TfLiteTensor* output = nullptr;
int stride = 0;  // feature frames per model step (the input's second dimension)
FrontendState frontend;
wake::Detector detector(kCutoff, kWindow);

// Task <-> main loop.
std::atomic<bool> wanted{false};
std::atomic<uint32_t> idleTicks{0};  // bumped each time the task sees !wanted
std::atomic<float> lastProbability{0};
std::atomic<float> lastScore{0};
std::atomic<uint32_t> detections{0};
std::atomic<float> detectedScore{0};
uint32_t seenDetections = 0;  // main loop only

portMUX_TYPE statsMux = portMUX_INITIALIZER_UNLOCKED;
struct Totals {
  uint32_t frames, invokes, invokeErrors, shortReads;
  uint64_t invokeUs, frontendUs;
  uint32_t invokeUsMax;
};
Totals totals = {};
int64_t statsSince = 0;

const char* fail(const char* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  vsnprintf(error, sizeof(error), fmt, args);
  va_end(args);
  return error;
}

// The first op the model needs that isn't registered, or nullptr.
const char* missingOp() {
  const auto* codes = model->operator_codes();
  if (codes == nullptr) return nullptr;
  for (const auto* code : *codes) {
    const tflite::BuiltinOperator op = tflite::GetBuiltinCode(code);
    if (op == tflite::BuiltinOperator_CUSTOM) return "a custom op";
    if (resolver.FindOp(op) == nullptr) return tflite::EnumNameBuiltinOperator(op);
  }
  return nullptr;
}

// Builds the interpreter, doubling the arena from the manifest's size until it fits.
const char* allocate() {
  varArena = static_cast<uint8_t*>(
      heap_caps_aligned_alloc(16, kVarArenaBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (varArena == nullptr) return fail("Wake: no PSRAM for the variable arena");
  for (size_t size = kArenaBytes; size <= kMaxArenaBytes; size *= 2) {
    arena = static_cast<uint8_t*>(
        heap_caps_aligned_alloc(16, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (arena == nullptr) return fail("Wake: no PSRAM for a %u-byte arena", (unsigned)size);
    tflite::MicroAllocator* varAlloc = tflite::MicroAllocator::Create(varArena, kVarArenaBytes);
    tflite::MicroResourceVariables* vars =
        varAlloc ? tflite::MicroResourceVariables::Create(varAlloc, kMaxResourceVariables)
                 : nullptr;
    if (vars == nullptr) return fail("Wake: couldn't set up the resource variables");
    interpreter = new (std::nothrow) tflite::MicroInterpreter(model, resolver, arena, size, vars);
    if (interpreter != nullptr && interpreter->AllocateTensors() == kTfLiteOk) {
      arenaBytes = size;
      return nullptr;
    }
    delete interpreter;
    interpreter = nullptr;
    heap_caps_free(arena);
    arena = nullptr;
    Serial.printf("Wake: AllocateTensors failed with a %u-byte arena (message above)\n",
                  (unsigned)size);
  }
  return fail("Wake: the model doesn't fit a %u KB arena, or a tensor/op failed to prepare",
              (unsigned)(kMaxArenaBytes / 1024));
}

const char* checkTensors() {
  input = interpreter->input(0);
  output = interpreter->output(0);
  if (input == nullptr || output == nullptr) return fail("Wake: the model has no input/output");
  const TfLiteIntArray* d = input->dims;
  if (d->size != 3 || d->data[0] != 1 || d->data[2] != kChannels || d->data[1] < 1 ||
      d->data[1] > 10) {
    return fail("Wake: input shape isn't [1, stride, %d]", kChannels);
  }
  stride = d->data[1];
  if (input->type != kTfLiteInt8 && input->type != kTfLiteFloat32) {
    return fail("Wake: input type %s, expected int8 or float32", TfLiteTypeGetName(input->type));
  }
  if (input->type == kTfLiteInt8 && !(input->params.scale > 0)) {
    return fail("Wake: the int8 input has no quantisation scale");
  }
  if (output->bytes == 0 ||
      (output->type != kTfLiteUInt8 && output->type != kTfLiteInt8 &&
       output->type != kTfLiteFloat32)) {
    return fail("Wake: output type %s, expected uint8, int8 or float32",
                TfLiteTypeGetName(output->type));
  }
  return nullptr;
}

float readProbability() {
  switch (output->type) {
    case kTfLiteUInt8:
      return wake::dequantize(output->data.uint8[0], output->params.scale,
                              output->params.zero_point);
    case kTfLiteInt8:
      return wake::dequantize(output->data.int8[0], output->params.scale,
                              output->params.zero_point);
    default:
      return output->data.f[0];
  }
}

// One feature frame into the input; a model step once `stride` frames are in.
void onFeatures(const uint16_t* f, int slot, Totals& t) {
  if (input->type == kTfLiteInt8) {
    int8_t* dst = input->data.int8 + slot * kChannels;
    const float scale = input->params.scale;
    const int32_t zp = input->params.zero_point;
    for (int i = 0; i < kChannels; i++) dst[i] = wake::quantizeFeature(f[i], scale, zp);
  } else {
    float* dst = input->data.f + slot * kChannels;
    for (int i = 0; i < kChannels; i++) dst[i] = f[i] * wake::kFeatureToReal;
  }
  if (slot + 1 < stride) return;
  const int64_t t0 = esp_timer_get_time();
  const TfLiteStatus s = interpreter->Invoke();
  const uint32_t us = static_cast<uint32_t>(esp_timer_get_time() - t0);
  if (s != kTfLiteOk) {
    t.invokeErrors++;
    return;
  }
  t.invokes++;
  t.invokeUs += us;
  if (us > t.invokeUsMax) t.invokeUsMax = us;
  const float p = readProbability();
  lastProbability = p;
  if (detector.step(p, millis())) {
    detectedScore = detector.detectedScore();
    detections++;
  }
  lastScore = detector.score();
}

void wakeTask(void*) {
  int16_t pcm[kFrameSamples];
  DcBlocker dc;
  bool active = false;
  bool seed = false;
  int slot = 0;
  for (;;) {
    if (!wanted.load()) {
      active = false;
      idleTicks++;
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    if (!active) {
      audio::flushMic();
      FrontendReset(&frontend);
      slot = 0;
      seed = true;
      // The model's streaming state still holds the end of the last run; the hold-off
      // gives it time to fill with fresh audio before anything can fire.
      detector.holdOff(millis());
      active = true;
    }
    Totals t = {};
    if (audio::readMic(pcm, kFrameSamples) != kFrameSamples) {
      t.shortReads++;
      vTaskDelay(pdMS_TO_TICKS(10));
    } else {
      // The INMP441 has a DC offset that the training audio didn't.
      if (seed) {
        dc.reset(pcm[0]);
        seed = false;
      }
      dc.processBlock(pcm, kFrameSamples);
      t.frames = 1;
      const int16_t* p = pcm;
      size_t left = kFrameSamples;
      while (left > 0) {
        size_t used = 0;
        const int64_t f0 = esp_timer_get_time();
        const FrontendOutput out = FrontendProcessSamples(&frontend, p, left, &used);
        t.frontendUs += esp_timer_get_time() - f0;
        p += used;
        left -= used;
        if (out.size == static_cast<size_t>(kChannels)) {
          onFeatures(out.values, slot, t);
          slot = (slot + 1) % stride;
        }
        if (used == 0) break;
      }
    }
    portENTER_CRITICAL(&statsMux);
    totals.frames += t.frames;
    totals.invokes += t.invokes;
    totals.invokeErrors += t.invokeErrors;
    totals.shortReads += t.shortReads;
    totals.invokeUs += t.invokeUs;
    totals.frontendUs += t.frontendUs;
    if (t.invokeUsMax > totals.invokeUsMax) totals.invokeUsMax = t.invokeUsMax;
    portEXIT_CRITICAL(&statsMux);
  }
}

const char* start() {
  if (!WAKE_HAVE_MODEL) {
    return fail(
        "No wake word model: put hey_carl.tflite in firmware/models, run "
        "scripts/model_to_header.py, then rebuild and flash");
  }
  if (!psramFound()) return fail("Wake: no PSRAM");
  model = tflite::GetModel(kModelData);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    return fail("Wake: model schema %lu, this TFLM reads %d",
                static_cast<unsigned long>(model->version()), TFLITE_SCHEMA_VERSION);
  }
  if (!registerOps()) return fail("Wake: couldn't register the ops (raise kOpCount)");
  if (const char* op = missingOp()) {
    return fail("Wake: the model needs op %s, which isn't registered (see wake.cpp)", op);
  }
  if (const char* e = allocate()) return e;
  if (const char* e = checkTensors()) return e;
  if (!startFrontend(&frontend)) return fail("Wake: the microfrontend didn't start (memory?)");
  if (xTaskCreatePinnedToCore(wakeTask, "wake", kTaskStack, nullptr, kTaskPriority, nullptr,
                              kTaskCore) != pdPASS) {
    return fail("Wake: couldn't start the task");
  }
  ready = true;
  return nullptr;
}

const char* typeName(const TfLiteTensor* t) { return TfLiteTypeGetName(t->type); }

void printTensor(const char* label, const TfLiteTensor* t) {
  Serial.printf("  %s: %s [", label, typeName(t));
  for (int i = 0; i < t->dims->size; i++) {
    Serial.printf(i ? ", %d" : "%d", t->dims->data[i]);
  }
  Serial.printf("], scale %.6f, zero point %ld\n", t->params.scale,
                static_cast<long>(t->params.zero_point));
}

}  // namespace

namespace wake {

bool haveModel() { return WAKE_HAVE_MODEL; }

const char* begin() {
  if (!tried) {
    tried = true;
    if (start() == nullptr) error[0] = '\0';
  }
  return error[0] ? error : nullptr;
}

void printInfo() {
  Serial.printf("Wake word: %s (model %u bytes)\n", kWakeWord, static_cast<unsigned>(kModelBytes));
  Serial.printf("  cutoff %.2f, window %u steps, cooldown %lu ms\n", kCutoff,
                static_cast<unsigned>(kWindow), static_cast<unsigned long>(kCooldownMs));
  if (model != nullptr && model->operator_codes() != nullptr) {
    Serial.print("  ops:");
    for (const auto* code : *model->operator_codes()) {
      const tflite::BuiltinOperator op = tflite::GetBuiltinCode(code);
      const bool have = op != tflite::BuiltinOperator_CUSTOM && resolver.FindOp(op) != nullptr;
      Serial.printf(" %s%s", tflite::EnumNameBuiltinOperator(op), have ? "" : "(MISSING)");
    }
    Serial.println();
  }
  if (interpreter == nullptr || input == nullptr || output == nullptr) return;
  printTensor("input", input);
  printTensor("output", output);
  Serial.printf("  %d feature frames per step (%d ms)\n", stride, stride * 10);
  Serial.printf("  arena: %u of %u bytes used (manifest says %u), in PSRAM\n",
                static_cast<unsigned>(interpreter->arena_used_bytes()),
                static_cast<unsigned>(arenaBytes), static_cast<unsigned>(kArenaBytes));
}

void run(bool on) {
  if (!ready) return;
  if (on) {
    seenDetections = detections.load();
    takeStats();  // start the counters now
    wanted = true;
    return;
  }
  if (!wanted.load()) return;
  const uint32_t ticks = idleTicks.load();
  wanted = false;
  // The task bumps idleTicks only after it has seen !wanted, so from then on it's off the mic.
  const uint32_t start = millis();
  while (idleTicks.load() == ticks && millis() - start < 500) delay(2);
}

bool running() { return wanted.load(); }

Stats takeStats() {
  portENTER_CRITICAL(&statsMux);
  const Totals t = totals;
  totals = {};
  portEXIT_CRITICAL(&statsMux);
  const int64_t now = esp_timer_get_time();
  const int64_t wall = statsSince ? now - statsSince : 0;
  statsSince = now;
  Stats s;
  s.ms = static_cast<uint32_t>(wall / 1000);
  s.frames = t.frames;
  s.invokes = t.invokes;
  s.invokeErrors = t.invokeErrors;
  s.shortReads = t.shortReads;
  s.invokeUsAvg = t.invokes ? static_cast<uint32_t>(t.invokeUs / t.invokes) : 0;
  s.invokeUsMax = t.invokeUsMax;
  s.frontendUsAvg = t.frames ? static_cast<uint32_t>(t.frontendUs / t.frames) : 0;
  s.load = wall > 0 ? static_cast<float>(t.invokeUs + t.frontendUs) / wall : 0;
  return s;
}

float probability() { return lastProbability.load(); }

float score() { return lastScore.load(); }

bool takeDetection(float* out) {
  const uint32_t n = detections.load();
  if (n == seenDetections) return false;
  seenDetections = n;
  if (out != nullptr) *out = detectedScore.load();
  return true;
}

}  // namespace wake
