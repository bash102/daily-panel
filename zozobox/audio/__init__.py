"""Audio (SPEC section 5, PLAN.md M2).

- `Output.BOTH` (speech) plays on the handset earpiece and the box speaker;
  `Output.SPEAKER` (chimes, clicks) on the box speaker only.
- The box speaker is muted while recording (`create_alsa_audio()` wires the
  recorder to the player). A SPEAKER sound requested while muted is not played
  (`Playback.suppressed`); M5 should play a stop-press click after `record()`
  returns.
- Each output has a fixed hearing-safe ceiling (`CardConfig.max_fraction`);
  `set_volume(0..1)` scales below it.
- Assets are pre-normalised to -16 LUFS 48 kHz 16-bit WAV with `normalize()`.

Real backends: `AlsaAudioOut` / `AlsaAudioIn` (alsa-utils). Test doubles:
`FakeAudioOut` / `FakeAudioIn`.
"""

from .alsa import AlsaAudioIn, AlsaAudioOut, ArecordSource, create_alsa_audio, volume_percent
from .base import AudioIn, AudioOut, Output, Playback, Recording, SpeakerMuter
from .config import AudioConfig, CardConfig, MicConfig
from .fake import FakeAudioIn, FakeAudioOut, FakeProc, FakeTake
from .loudness import NormalizeError, normalize
from .recording import PcmSource, record_pcm, rms_dbfs

__all__ = [
    "AlsaAudioIn", "AlsaAudioOut", "ArecordSource", "AudioConfig", "AudioIn", "AudioOut",
    "CardConfig", "FakeAudioIn", "FakeAudioOut", "FakeProc", "FakeTake", "MicConfig",
    "NormalizeError", "Output", "PcmSource", "Playback", "Recording", "SpeakerMuter",
    "create_alsa_audio", "normalize", "record_pcm", "rms_dbfs", "volume_percent",
]
