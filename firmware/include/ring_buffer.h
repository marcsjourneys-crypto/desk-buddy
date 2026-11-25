#pragma once
// A fixed-size FIFO of 16-bit samples over storage the caller owns (e.g. PSRAM).
// Writes are partial when it's full. No locking here: the caller locks.
// Pure: no Arduino.
#include <stddef.h>
#include <stdint.h>
#include <string.h>

class Int16Ring {
 public:
  Int16Ring(int16_t* storage, size_t capacity) : buf_(storage), cap_(capacity) {}

  // Returns how many samples fit. At most two copies: up to the end, then from the start.
  size_t write(const int16_t* src, size_t n) {
    if (n > space()) n = space();
    if (n == 0) return 0;
    const size_t first = n < cap_ - head_ ? n : cap_ - head_;
    memcpy(buf_ + head_, src, first * sizeof(int16_t));
    memcpy(buf_, src + first, (n - first) * sizeof(int16_t));
    head_ = (head_ + n) % cap_;
    count_ += n;
    return n;
  }

  // Returns how many samples were read.
  size_t read(int16_t* dst, size_t n) {
    if (n > count_) n = count_;
    if (n == 0) return 0;
    const size_t first = n < cap_ - tail_ ? n : cap_ - tail_;
    memcpy(dst, buf_ + tail_, first * sizeof(int16_t));
    memcpy(dst + first, buf_, (n - first) * sizeof(int16_t));
    tail_ = (tail_ + n) % cap_;
    count_ -= n;
    return n;
  }

  // Discards up to n of the oldest samples (to make room); returns how many.
  size_t drop(size_t n) {
    if (n > count_) n = count_;
    if (n == 0) return 0;
    tail_ = (tail_ + n) % cap_;
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
