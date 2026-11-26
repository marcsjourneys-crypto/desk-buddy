# Test fixtures

## `speech.wav`

Synthetic speech, not a recording of a person: Piper output from the voice
`en_GB-northern_english_male-medium` (CARL's voice), made once with
`desk_buddy.tts.PiperSynthesizer` and resampled to 16 kHz, 16-bit mono. It says "Right.
What's the weather going to be like in Leeds tomorrow afternoon?" (3.70 s). Used by
`tests/test_vad.py`, so those tests need no voice download and no network.

The voice's model card (`rhasspy/piper-voices`, `en/en_GB/northern_english_male/medium/MODEL_CARD`
on Hugging Face, read 2026-11-26) says:
- Dataset: http://www.openslr.org/83/ (OpenSLR SLR83, "Crowdsourced high-quality UK and
  Ireland English Dialect speech data set"), licence "CC-BY-SA 4.0 International". The
  OpenSLR page also gives "Attribution-ShareAlike 4.0 International".
- Training: fine-tuned from the "rdh" base model (nl_BE, one Flemish speaker, CC0).

## `morning_brief/`

Saved responses for the morning brief's tests.
