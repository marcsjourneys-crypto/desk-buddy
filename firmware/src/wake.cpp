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
#include <esp_memory_utils.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <stdarg.h>
#include <stdio.h>

#include <atomic>
#include <new>

#include "audio.h"
#include "wake_logic.h"
#include "wake_mode.h"

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

// The streaming state (resource variables) gets its own small arena. Both arenas live in
// internal RAM (W1, after one unexplained "Cache error, MMU entry fault" inside Invoke() with
// them in PSRAM), sized from a measuring pass: begin() first builds the interpreter on
// generous PSRAM arenas, reads what AllocateTensors actually used, then rebuilds it on
// internal arenas of that size plus a margin. If internal RAM is short, an arena stays in
// PSRAM with a warning.
constexpr size_t kMeasureVarArenaBytes = 32 * 1024;
constexpr int kMaxResourceVariables = 32;
constexpr size_t kMaxArenaBytes = 256 * 1024;  // the arena doubles up to this if too small
constexpr uint32_t kInternalCaps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
constexpr uint32_t kPsramCaps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;

// What was used, plus a quarter (at least 2 KB) for alignment and TFLM's own slack, rounded
// up to 16 bytes.
constexpr size_t withMargin(size_t used) {
  return (used + (used / 4 > 2048 ? used / 4 : 2048) + 15) & ~static_cast<size_t>(15);
}

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

struct Arena {
  uint8_t* data = nullptr;
  size_t size = 0;
  size_t used = 0;  // what TFLM had taken once AllocateTensors finished
  bool internal = false;
};
Arena arena;     // the tensor arena
Arena varArena;  // the resource variables (the model's streaming state)
size_t internalFreeBefore = 0;  // internal heap around begin(), for `wake info`
size_t internalFreeAfter = 0;
tflite::MicroInterpreter* interpreter = nullptr;
TfLiteTensor* input = nullptr;
TfLiteTensor* output = nullptr;
int stride = 0;  // feature frames per model step (the input's second dimension)
FrontendState frontend;
wake::Detector detector(kCutoff, kWindow);

// The mic reader (audio.cpp) -> the wake task: 20 ms frames with their pre-roll numbers.
struct Frame {
  uint32_t seq;
  int16_t pcm[audio::kFrameSamples];
};
constexpr UBaseType_t kFeedDepth = 8;  // 160 ms: rides out a slow model step
QueueHandle_t feedQueue = nullptr;

// Task <-> main loop.
std::atomic<bool> wanted{false};
std::atomic<float> threshold{kCutoff};  // replaces the manifest's cutoff (setThreshold)
std::atomic<float> lastProbability{0};
std::atomic<float> lastScore{0};
std::atomic<uint32_t> detections{0};
std::atomic<float> detectedScore{0};
std::atomic<uint32_t> detectedSeq{0};  // the frame being processed when it fired
std::atomic<uint32_t> fedDrops{0};     // frames the full queue refused
uint32_t seenDetections = 0;  // main loop only

portMUX_TYPE statsMux = portMUX_INITIALIZER_UNLOCKED;
struct Totals {
  uint32_t frames, invokes, invokeErrors;
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

void freeArena(Arena& a) {
  heap_caps_free(a.data);
  a = Arena{};
}

void dropInterpreter() {
  delete interpreter;
  interpreter = nullptr;
  freeArena(arena);
  freeArena(varArena);
}

bool allocArena(Arena& a, size_t size, uint32_t caps) {
  a.data = static_cast<uint8_t*>(heap_caps_aligned_alloc(16, size, caps));
  if (a.data == nullptr) return false;
  a.size = size;
  a.used = 0;
  a.internal = esp_ptr_internal(a.data);
  return true;
}

// The interpreter on the arenas in hand. True once AllocateTensors worked; records the use.
tflite::MicroAllocator* varAlloc = nullptr;  // the variable arena's allocator

bool build() {
  varAlloc = tflite::MicroAllocator::Create(varArena.data, varArena.size);
  tflite::MicroResourceVariables* vars =
      varAlloc ? tflite::MicroResourceVariables::Create(varAlloc, kMaxResourceVariables)
               : nullptr;
  if (vars == nullptr) return false;
  interpreter = new (std::nothrow)
      tflite::MicroInterpreter(model, resolver, arena.data, arena.size, vars);
  if (interpreter == nullptr || interpreter->AllocateTensors() != kTfLiteOk) {
    delete interpreter;
    interpreter = nullptr;
    return false;
  }
  arena.used = interpreter->arena_used_bytes();
  varArena.used = varAlloc->used_bytes();
  return true;
}

// The measuring pass: PSRAM arenas, the tensor arena doubling from the manifest's size until
// the model fits. Leaves the interpreter built on them.
const char* measure() {
  for (size_t size = kArenaBytes; size <= kMaxArenaBytes; size *= 2) {
    if (!allocArena(varArena, kMeasureVarArenaBytes, kPsramCaps)) {
      return fail("Wake: no PSRAM for the variable arena");
    }
    if (!allocArena(arena, size, kPsramCaps)) {
      freeArena(varArena);
      return fail("Wake: no PSRAM for a %u-byte arena", static_cast<unsigned>(size));
    }
    // build() reads the use right after AllocateTensors: the resource variables' buffers
    // are allocated in Prepare (ASSIGN_VARIABLE), so that is all of it; CALL_ONCE's first
    // Invoke() only writes their initial values into them.
    if (build()) return nullptr;
    dropInterpreter();
    Serial.printf("Wake: AllocateTensors failed with a %u-byte arena (message above)\n",
                  static_cast<unsigned>(size));
  }
  return fail("Wake: the model doesn't fit a %u KB arena, or a tensor/op failed to prepare",
              static_cast<unsigned>(kMaxArenaBytes / 1024));
}

// Measures, then moves both arenas to internal RAM at their measured size plus a margin.
const char* allocate() {
  internalFreeBefore = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  if (const char* e = measure()) return e;
  const size_t tensorBytes = withMargin(arena.used);
  const size_t varBytes = withMargin(varArena.used);
  dropInterpreter();
  // Each arena goes internal if it can; one that can't goes to PSRAM.
  const bool tensorIn = allocArena(arena, tensorBytes, kInternalCaps);
  if (!tensorIn && !allocArena(arena, tensorBytes, kPsramCaps)) {
    return fail("Wake: no memory for a %u-byte arena", static_cast<unsigned>(tensorBytes));
  }
  const bool varIn = allocArena(varArena, varBytes, kInternalCaps);
  if (!varIn && !allocArena(varArena, varBytes, kPsramCaps)) {
    freeArena(arena);
    return fail("Wake: no memory for a %u-byte variable arena", static_cast<unsigned>(varBytes));
  }
  if (!tensorIn || !varIn) {
    Serial.printf("Wake: WARNING: internal RAM is short, so the %s in PSRAM\n",
                  !tensorIn && !varIn ? "arenas are"
                                      : (!tensorIn ? "tensor arena is" : "variable arena is"));
  }
  if (!build()) {
    // The measured size plus the margin should always fit; if it doesn't, use the
    // measuring pass's roomy PSRAM arenas rather than fail.
    dropInterpreter();
    Serial.println("Wake: WARNING: the sized arenas didn't fit, so both are in PSRAM");
    if (const char* e = measure()) return e;
  }
  internalFreeAfter = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  return nullptr;
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

// One feature frame into the input; a model step once `stride` frames are in. `seq` is the
// mic frame it came from.
void onFeatures(const uint16_t* f, int slot, uint32_t seq, Totals& t) {
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
    detectedSeq = seq;
    detections++;  // last: takeDetection() reads the score and seq once it sees this
  }
  lastScore = detector.score();
}

// Processes the frames the mic reader feeds (it never reads the mic itself). While not
// wanted it throws frames away; each restart begins with a fresh frontend and a hold-off.
void wakeTask(void*) {
  static Frame frame;  // one task, and keeps 644 bytes off its stack
  bool active = false;
  int slot = 0;
  for (;;) {
    const bool got = xQueueReceive(feedQueue, &frame, pdMS_TO_TICKS(20)) == pdTRUE;
    if (!wanted.load()) {
      active = false;
      continue;
    }
    if (!got) continue;
    if (!active) {
      FrontendReset(&frontend);
      slot = 0;
      // The model's streaming state still holds the end of the last run; the hold-off
      // gives it time to fill with fresh audio before anything can fire.
      detector.holdOff(millis());
      active = true;
    }
    detector.setCutoff(threshold.load());
    Totals t = {};
    t.frames = 1;
    const int16_t* p = frame.pcm;
    size_t left = audio::kFrameSamples;
    while (left > 0) {
      size_t used = 0;
      const int64_t f0 = esp_timer_get_time();
      const FrontendOutput out = FrontendProcessSamples(&frontend, p, left, &used);
      t.frontendUs += esp_timer_get_time() - f0;
      p += used;
      left -= used;
      if (out.size == static_cast<size_t>(kChannels)) {
        onFeatures(out.values, slot, frame.seq, t);
        slot = (slot + 1) % stride;
      }
      if (used == 0) break;
    }
    portENTER_CRITICAL(&statsMux);
    totals.frames += t.frames;
    totals.invokes += t.invokes;
    totals.invokeErrors += t.invokeErrors;
    totals.invokeUs += t.invokeUs;
    totals.frontendUs += t.frontendUs;
    if (t.invokeUsMax > totals.invokeUsMax) totals.invokeUsMax = t.invokeUsMax;
    portEXIT_CRITICAL(&statsMux);
  }
}

// The mic reader's sink (its task, core 1): never blocks; a full queue drops the frame.
void feed(const int16_t* pcm, size_t n, uint32_t seq) {
  if (!wanted.load() || n != audio::kFrameSamples) return;
  Frame f;
  f.seq = seq;
  memcpy(f.pcm, pcm, sizeof(f.pcm));
  if (xQueueSend(feedQueue, &f, 0) != pdTRUE) fedDrops++;
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
  feedQueue = xQueueCreate(kFeedDepth, sizeof(Frame));
  if (feedQueue == nullptr) return fail("Wake: no memory for the frame queue");
  if (xTaskCreatePinnedToCore(wakeTask, "wake", kTaskStack, nullptr, kTaskPriority, nullptr,
                              kTaskCore) != pdPASS) {
    return fail("Wake: couldn't start the task");
  }
  audio::setWakeSink(feed);
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

void printArena(const char* label, const Arena& a) {
  Serial.printf("  %s: %u of %u bytes used, in %s\n", label, static_cast<unsigned>(a.used),
                static_cast<unsigned>(a.size), a.internal ? "internal RAM" : "PSRAM (fallback)");
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
  Serial.printf("  cutoff %.2f (in use %.2f), window %u steps, cooldown %lu ms\n", kCutoff,
                threshold.load(), static_cast<unsigned>(kWindow),
                static_cast<unsigned long>(kCooldownMs));
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
  printArena("tensor arena", arena);
  Serial.printf("    (the manifest says %u)\n", static_cast<unsigned>(kArenaBytes));
  printArena("variable arena", varArena);
  Serial.printf("  internal heap: %u KB free before the wake engine, %u KB after; now %u KB "
                "free (min %u KB, largest block %u KB)\n",
                static_cast<unsigned>(internalFreeBefore / 1024),
                static_cast<unsigned>(internalFreeAfter / 1024),
                static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
                static_cast<unsigned>(heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024),
                static_cast<unsigned>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024));
}

bool ready() { return ::ready; }

float modelCutoff() { return kCutoff; }

void setThreshold(float t) {
  if (validThreshold(t)) threshold = t;
}

float currentThreshold() { return threshold.load(); }

void run(bool on) {
  if (!::ready || on == wanted.load()) return;
  if (on) {
    seenDetections = detections.load();  // nothing from an earlier run counts
    takeStats();                         // start the counters now
  }
  wanted = on;
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
  s.dropped = fedDrops.exchange(0);
  s.invokeUsAvg = t.invokes ? static_cast<uint32_t>(t.invokeUs / t.invokes) : 0;
  s.invokeUsMax = t.invokeUsMax;
  s.frontendUsAvg = t.frames ? static_cast<uint32_t>(t.frontendUs / t.frames) : 0;
  s.load = wall > 0 ? static_cast<float>(t.invokeUs + t.frontendUs) / wall : 0;
  return s;
}

float probability() { return lastProbability.load(); }

float score() { return lastScore.load(); }

bool takeDetection(float* out, uint32_t* seq) {
  const uint32_t n = detections.load();
  if (n == seenDetections) return false;
  seenDetections = n;
  if (out != nullptr) *out = detectedScore.load();
  if (seq != nullptr) *seq = detectedSeq.load();
  return true;
}

}  // namespace wake
