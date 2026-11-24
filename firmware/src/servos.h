#pragma once
// Pan/tilt MG90S servos: eased, speed-limited moves; no pulses once a servo arrives.

namespace servos {

bool begin();  // attaches both PWM channels once; false if LEDC refused
bool ready();  // true once begin() succeeded

// Blocking demo: pan centre/left/right/centre, then tilt. Calls stop() every few ms and
// returns early if it says true. Returns true only if the whole sweep ran.
bool sweep(bool (*stop)());

}  // namespace servos
