"""Energy/ZCR speech gate + endpointing (stdlib only; 20 ms frames).

Two cooperating roles, per plan 6.1/7: (a) the decode gate - silence must never
reach the decoders (measured silence hallucination); (b) the single source of
final decision - a phrase closes after `endpointing_ms` of contiguous silence
(clamped >= 800 ms; effective server endpointing mirrors the deployed
baseline a12b237 policy). Rolling partials NEVER finalize a segment.

Frame-accurate segmentation: feed() classifies fixed 20 ms frames on a stable
internal grid (carry keeps the grid aligned across arbitrary transport chunk
sizes), and reports EVERY closure inside one message plus the trailing open
run. Event positions are ABSOLUTE frame indices counted since gate
construction, so identical PCM yields identical segment spans regardless of
20 ms / 100 ms / irregular / large message grouping. reset_state() clears the
speech state machine but keeps the frame grid, carry and adaptive floor.
"""
import array
from dataclasses import dataclass, field

from .wavwrap import seconds_of, InvalidAudio

_INT16 = 32768.0


def frame_features(samples: array.array) -> tuple:
    energy = 0.0
    crossings = 0
    prev = samples[0]
    energy += float(prev) * float(prev)
    for i in range(1, len(samples)):
        s = samples[i]
        energy += float(s) * float(s)
        if (prev < 0) != (s < 0):
            crossings += 1
        prev = s
    rms = (energy / len(samples)) ** 0.5 / _INT16
    zcr = crossings / (len(samples) - 1) if len(samples) > 1 else 0.0
    return rms, zcr


@dataclass
class EndpointConfig:
    frame_ms: float = 20.0
    onset_ms: float = 60.0
    endpointing_ms: int = 800
    min_rms: float = 0.012
    max_zcr: float = 0.60
    noise_adapt: float = 0.05


@dataclass
class GateEvents:
    speech_s: float = 0.0                     # speech-like seconds in this message
    closes: list = field(default_factory=list)  # [(run_start_frame, end_frame)] absolute
    opened: int = None                        # absolute frame index of the trailing open run
    hangover: bool = False                    # silence run pending an endpoint (keep recording)


class SpeechGate:
    """Streaming speech gate: per-message endpoint events on absolute frames."""

    def __init__(self, endpointing_ms: int = 800, sample_rate: int = 16000):
        self.rate = sample_rate
        self.cfg = EndpointConfig(endpointing_ms=max(endpointing_ms, 800))
        self.frame_samples = max(1, round(self.rate * (self.cfg.frame_ms / 1000)))
        self.onset_frames = max(1, round(self.cfg.onset_ms / self.cfg.frame_ms))
        self._set_hangover(self.cfg.endpointing_ms)
        self._noise = self.cfg.min_rms
        self._carry = array.array("h")
        self._idx = 0  # absolute frames processed since construction; never rewound
        self._in_speech = False
        self._run = 0
        self._sil = 0
        self._open_start = None

    def _set_hangover(self, endpointing_ms):
        self.hangover_frames = max(1, round(endpointing_ms / self.cfg.frame_ms))

    def set_endpointing_ms(self, endpointing_ms: int):
        """Live endpointing change WITHOUT resetting the frame grid/carry
        (replacing the gate would break absolute-frame bookkeeping)."""
        self.cfg.endpointing_ms = max(endpointing_ms, 800)
        self._set_hangover(self.cfg.endpointing_ms)

    def reset_state(self):
        """Clear the speech state machine; frame grid, carry and noise floor
        survive so residual samples stay on the same grid."""
        self._in_speech = False
        self._run = 0
        self._sil = 0
        self._open_start = None

    def classify(self, rms, zcr):
        thr = max(self.cfg.min_rms, self._noise * 3.0)
        if rms < thr:
            return False
        if zcr > self.cfg.max_zcr:
            return False
        return True

    def feed_speech_seconds(self, pcm: bytes) -> float:
        """Stateless speech-duration of one chunk (no state advance)."""
        usable = len(pcm) - (len(pcm) % 2)
        if not usable:
            return 0.0
        samples = array.array("h")
        samples.frombytes(pcm[:usable])
        speech = 0
        fs = self.frame_samples
        for pos in range(0, len(samples) - fs + 1, fs):
            rms, zcr = frame_features(samples[pos:pos + fs])
            if rms < self.cfg.min_rms:
                continue
            if zcr > self.cfg.max_zcr and rms < self.cfg.min_rms * 2:
                continue
            speech += fs
        return speech / self.rate

    def feed(self, pcm: bytes) -> GateEvents:
        """Process one transport message of PCM16; return all endpoint events.
        Absolute frame f covers session samples [f*fs, (f+1)*fs)."""
        usable = len(pcm) - (len(pcm) % 2)
        buf = self._carry
        buf.extend(array.array("h", pcm[:usable] if usable else b""))
        self._carry = array.array("h")
        ev = GateEvents()
        fs = self.frame_samples
        pos = 0
        while len(buf) - pos >= fs:
            frame = buf[pos:pos + fs]
            pos += fs
            rms, zcr = frame_features(frame)
            speech = self.classify(rms, zcr)
            # Adapt the noise floor ONLY on genuine quiet OUTSIDE speech and
            # outside the hangover countdown: adapting on intra-speech dips or
            # hangover frames escalates the floor to speech level and silently
            # drops every following phrase (measured tail loss).
            if (not speech and not self._in_speech and self._sil == 0
                    and rms < max(self.cfg.min_rms, self._noise * 3.0)):
                self._noise = (1 - self.cfg.noise_adapt) * self._noise + self.cfg.noise_adapt * rms
            ev.speech_s += self.cfg.frame_ms / 1000.0 if speech else 0.0
            if not self._in_speech:
                if speech:
                    self._run += 1
                    if self._run >= self.onset_frames:
                        self._in_speech = True
                        self._sil = 0
                        # run began (run-1) frames before the current one
                        self._open_start = self._idx - (self._run - 1)
                else:
                    self._run = 0
            else:
                if speech:
                    self._sil = 0
                else:
                    self._sil += 1
                    if self._sil >= self.hangover_frames:
                        ev.closes.append((self._open_start, self._idx + 1))
                        self._open_start = None
                        self._in_speech = False
                        self._run = 0
                        self._sil = 0
            self._idx += 1
        if self._in_speech and self._open_start is not None:
            ev.opened = self._open_start
        ev.hangover = self._sil > 0
        self._carry = array.array("h", buf[pos:])
        return ev
