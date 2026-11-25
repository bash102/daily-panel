"""Static audio configuration: ALSA devices, mixer controls, volume ceilings.

Card names come from `aplay -l` / `arecord -l` on the Pi (the `[...]` short
name, e.g. `card 1: Phone [POP Phone]` -> "Phone"). Using CARD=<name> rather
than a card index keeps working when USB enumeration order changes.

`max_fraction` is the hearing-safe ceiling for each output, as a fraction of
the mixer's (mapped, perceptual) range. Calibrate it once on the real box with
a sound level meter (target ~75-80 dBA at the earpiece). The dashboard volume
(0..1) only ever scales below it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CardConfig:
    card: str  # ALSA card short name (CARD=...)
    device: int = 0
    # Mixer control for volume, e.g. "PCM", "Speaker", "Headphone". None = no
    # hardware volume control (the ceiling must then be set in asound.conf).
    mixer_control: str | None = "PCM"
    max_fraction: float = 0.6  # hearing-safe ceiling, 0..1 of mixer range

    @property
    def pcm(self) -> str:
        # plughw: ALSA converts rate/format/channels to whatever the card wants.
        return f"plughw:CARD={self.card},DEV={self.device}"


@dataclass(frozen=True)
class MicConfig:
    card: str = "Phone"
    device: int = 0
    mixer_control: str | None = "Mic"
    capture_fraction: float = 0.8  # capture gain set once at start; None-control skips
    rate: int = 16000
    channels: int = 1
    chunk_s: float = 0.05  # 50 ms reads: stop/max checks and RMS granularity
    # Silence detection (SPEC 2.2): a chunk counts as speech if its RMS is above
    # speech_dbfs; the take is "silent" unless speech adds up to min_speech_s.
    speech_dbfs: float = -40.0
    min_speech_s: float = 0.3

    @property
    def pcm(self) -> str:
        return f"plughw:CARD={self.card},DEV={self.device}"


@dataclass(frozen=True)
class AudioConfig:
    # UNVERIFIED names: check `aplay -l` on the Pi and adjust.
    handset: CardConfig = field(
        default_factory=lambda: CardConfig(card="Phone", mixer_control="PCM", max_fraction=0.5)
    )
    speaker: CardConfig = field(
        default_factory=lambda: CardConfig(card="Device", mixer_control="PCM", max_fraction=0.7)
    )
    mic: MicConfig = field(default_factory=MicConfig)
    default_volume: float = 0.7  # dashboard volume until the store says otherwise
    aplay: str = "aplay"
    arecord: str = "arecord"
    amixer: str = "amixer"
