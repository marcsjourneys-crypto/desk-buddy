#pragma once
// Wi-Fi and the WebSocket link to the server's /robot endpoint (docs/protocol.md).
// Everything here runs in the main loop.

namespace net {

// After settings::load(); waits in NoConfig until settings are complete. `fwVersion` must
// outlive the program (it's sent in every hello).
void begin(const char* fwVersion);
void loop();                // every main-loop pass: the link, its state machine, mic frames
void restart(bool wifi);    // settings changed: reconnect (and rejoin Wi-Fi if `wifi`)
void stop();                // settings forgotten: drop the link and leave Wi-Fi
bool connected();           // a WebSocket is open (the streaming tasks may own the audio)
void printStatus();         // the `net` command, wake mode included
bool wakeFromServer();      // the server's wake_config overrides the saved wake settings
float wakeThresholdInUse(); // the server's, else the saved one, else the model's cutoff

}  // namespace net
