#pragma once
// Both GC9A01 eyes: panels, frame buffers, expressions, blending and idle blinks.
#include <stdint.h>

namespace eyes {

bool begin();                   // false if the frame buffer can't be allocated
bool show(const char* name);    // a look name (see main.cpp help); false if unknown
void update(uint32_t now);      // call often: redraws only while something moves
float measureFps(uint32_t ms);  // forced redraws of both eyes for `ms`, returns frames/s
uint32_t frames();              // frames update() has drawn since boot (for live fps)

}  // namespace eyes
