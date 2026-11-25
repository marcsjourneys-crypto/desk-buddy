# microfrontend (vendored)

TensorFlow Lite Micro's audio microfrontend: the fixed-point C feature generator (window, FFT, mel filterbank, noise reduction, PCAN, log scale) that microWakeWord's training data goes through. The wake word code (`src/wake.cpp`) runs it on the robot so the model sees the same features it was trained on.

It is vendored because nothing ships it for us any more:
- TFLM removed `tensorflow/lite/experimental/microfrontend` on 2026-10-15 (commit `754cdd503e178c842115a7f59102314def572eac`, "Remove legacy experimental/microfrontend").
- Espressif's `esp-tflite-micro` 1.4.0 (prebuilt into Arduino core 3.3.12, which we use for TFLite Micro itself) no longer carries it either.

## Where each file comes from (pinned)

| Files | Upstream | Licence |
|---|---|---|
| `src/tensorflow/lite/experimental/microfrontend/lib/*` | `github.com/tensorflow/tflite-micro` at commit `5d5a2e9afce50bf2dc75f5e8b10776aef13157b6` (2026-10-15, the last commit with the microfrontend; its files last changed in `183da3e3021192d05502013d3f2df62bf63ca351`) | Apache-2.0 (`LICENSE`) |
| `src/third_party/kissfft/*` | KISS FFT as patched by TFLM, from `github.com/espressif/esp-tflite-micro` tag `v1.4.0` (commit `75e189e376132a2dcc572995d1b73b5dae6229ec`), the same copy whose headers the core ships | BSD-3-Clause (`src/third_party/kissfft/COPYING`) |

Only the library sources were copied, unchanged: the `*_io`, `*_main`, memmap generator, tests and Bazel files were left out. KISS FFT is not compiled on its own (`srcFilter` in `library.json`): `kiss_fft_int16.cc` includes its `.c` files inside the `kissfft_fixed16` namespace, as upstream does.

## Updating

Copy the same file list from a newer source, update the commits above, rebuild, and re-run the feature check (see `../../README.md`, "Wake word").
