#pragma once
// A fixed-size FIFO of 16-bit samples over storage the caller owns (e.g. PSRAM).
// Writes are partial when it's full. No locking here: the caller locks.
// Pure: no Arduino.
#include <stddef.h>
#include <stdint.h>

class Int16Ring {
 public:
  Int16Ring(int16_t* storage, size_t capacity) : buf_(storage), cap_(capacity) {}

  // Returns how many samples fit.
  size_t write(const int16_t* src, size_t n) {
    if (n > space()) n = space();
    for (size_t i = 0; i < n; i++) {
      buf_[head_] = src[i];
      head_ = head_ + 1 == cap_ ? 0 : head_ + 1;
    }
    count_ += n;
    return n;
  }

  // Returns how many samples were read.
  size_t read(int16_t* dst, size_t n) {
    if (n > count_) n = count_;
    for (size_t i = 0; i < n; i++) {
      dst[i] = buf_[tail_];
      tail_ = tail_ + 1 == cap_ ? 0 : tail_ + 1;
    }
    count_ -= n;
    return n;
  }

  size_t available() const { return count_; }
  size_t space() const { return cap_ - count_; }
  size_t capacity() const { return cap_; }
  void clear() { head_ = tail_ = count_ = 0; }

 private:
  int16_t* buf_;
  size_t cap_;
  size_t head_ = 0;  // next write
  size_t tail_ = 0;  // next read
  size_t count_ = 0;
};
