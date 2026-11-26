#pragma once
// The wake word's pre-roll: a ring of fixed-size mic frames (storage the caller owns, e.g.
// PSRAM), each numbered with a sequence number so a detection can say "keep from frame N".
// Full: a push overwrites the oldest frame. No locking here: the caller locks.
// Pure: no Arduino.
#include <stddef.h>
#include <stdint.h>
#include <string.h>

class FrameRing {
 public:
  // `frames` slots of `frameSamples` samples each; the first push gets `firstSeq`.
  FrameRing(int16_t* storage, size_t frames, size_t frameSamples, uint32_t firstSeq = 0)
      : buf_(storage), frames_(frames ? frames : 1), n_(frameSamples), next_(firstSeq) {}

  // Adds a frame; returns its sequence number. `overwrote` says whether the oldest was lost.
  uint32_t push(const int16_t* frame, bool* overwrote = nullptr) {
    memcpy(buf_ + head_ * n_, frame, n_ * sizeof(int16_t));
    head_ = (head_ + 1) % frames_;
    const bool full = count_ == frames_;
    if (!full) count_++;
    if (overwrote != nullptr) *overwrote = full;
    return next_++;
  }

  // The oldest frame, if any.
  bool pop(int16_t* out) {
    if (count_ == 0) return false;
    const size_t tail = (head_ + frames_ - count_) % frames_;
    memcpy(out, buf_ + tail * n_, n_ * sizeof(int16_t));
    count_--;
    return true;
  }

  // Drops every frame older than `seq` (a frame from the future empties the ring).
  // Wrap-safe: sequence numbers are compared by their difference.
  void keepFrom(uint32_t seq) {
    const int32_t d = static_cast<int32_t>(seq - oldestSeq());
    if (d <= 0) return;
    count_ -= static_cast<size_t>(d) < count_ ? static_cast<size_t>(d) : count_;
  }

  void clear() { count_ = 0; }  // the sequence numbers carry on

  size_t count() const { return count_; }
  size_t capacity() const { return frames_; }
  uint32_t oldestSeq() const { return next_ - static_cast<uint32_t>(count_); }
  uint32_t nextSeq() const { return next_; }

 private:
  int16_t* buf_;
  size_t frames_;
  size_t n_;
  size_t head_ = 0;  // the slot the next push writes
  size_t count_ = 0;
  uint32_t next_;    // the sequence number of the next push
};
