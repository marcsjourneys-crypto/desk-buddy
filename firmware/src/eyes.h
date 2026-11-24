#pragma once
// Both GC9A01 eyes: panels, frame buffers, expressions, blending and idle blinks.
#include <stdint.h>

namespace eyes {

bool begin();                   // false if the frame buffers can't be allocated
bool show(const char* name);    // idle, happy, sleep, blink, test; false if unknown
void update(uint32_t now);      // call often: redraws only while something moves
float measureFps(uint32_t ms);  // forced redraws of both eyes for `ms`, returns frames/s

}  // namespace eyes
