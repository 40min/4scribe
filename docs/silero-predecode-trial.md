# Silero pre-decode verification (stabilization)

**✅ Change:** shared `fourscribe.verifier` seam (`src/fourscribe/verifier.py`).
`app.create_app()` builds one verifier per process and injects it into every
session. Sessions call it on the **exact segment PCM** before ASR, for partials
and finals. Rejection means: no partial text, no final text, no aligner call.
A commit still ACKs and the epoch clock continues.

**🔧 Behaviour preserved:** energy segmentation, onset/endpointing/hangover,
absolute clock, buffer/ring, queue/drain ordering, commit-before-ACK ordering,
and word timestamps are unchanged. The verifier only gates emission; it never
trims, re-times, concatenates, or mutates session state.

## Dependency and reproducible installation

| Item | Value |
| --- | --- |
| Package | `silero-vad==6.2.3` |
| Wheel SHA256 | `7b7f5436cfcb02fae583a05b512ea96467fd449fe54cb49a5e4f06c51a1e43b8` |
| License | MIT (wheel `dist-info/LICENSE`) |
| Backend | bundled JIT `silero_vad.jit`, SHA256 `e1122837f4154c511485fe0b9c64455f7b929c96fbb8d79fbdb336383ebd3720` |
| Declared deps | `packaging`, `torch>=1.12.0` (both already present) |
| Accepted host | `fourscribe.service` user `opencode`; venv `/opt/40zen/services/4scribe/.venv`; Python 3.14.4; torch 2.14.1+cpu; numpy 2.5.4; packaging 26.3 |

Reproduce against the existing CPU torch (no new runtime, no ONNX/torchaudio,
no model download at runtime; Silero JIT weights are inside the wheel):

```bash
python -m pip download --no-deps --dest /tmp/silero-wheel silero-vad==6.2.3
sha256sum /tmp/silero-wheel/silero_vad-6.2.3-py3-none-any.whl
# expect 7b7f5436cfcb02fae583a05b512ea96467fd449fe54cb49a5e4f06c51a1e43b8
python -m pip install --no-deps /tmp/silero-wheel/silero_vad-6.2.3-py3-none-any.whl
```

Verified installation on this host only; a clean-room install was **not**
performed. Verifier config is frozen at threshold `0.5`,
`min_speech_duration_ms=96`, `min_silence_duration_ms=100`, `speech_pad_ms=0`.

## Activation, health and operations

- Env-driven production startup (`Config.from_env`) enables the verifier by
  default. Explicit `Config()` construction (tests/embedding) defaults to off.
- **Off switch:** `FSCRIBE_SILERO_ENABLED=0` in the service environment plus
  restart; `get_verifier` then returns the accept-all `NullVerifier`. The
  installed package may stay (the off path does not import it).
- `/healthz`: liveness plus aligner stack name and `verifier` name. It does NOT
  load the aligner model (lazy) and does not prove decode readiness.
- `/readyz`: Ollama `/api/tags` reachability only; it does not load the aligner
  or prove decode readiness. True readiness evidence is an authenticated
  partial+final smoke through the WSS route.
- Safe service actions (existing unit): `sudo systemctl restart fourscribe.service`,
  `sudo systemctl stop fourscribe.service`, `sudo systemctl start fourscribe.service`.
  Never restart while a recording is active.

## Failure policy

If the verifier is enabled but its package/model cannot load, startup fails
explicitly (`VerifierUnavailable`) - never a silent rejection path. A verifier
fault at runtime fails **open** (audio is still decoded) and is recorded
server-side only: `logger.warning` with the sanitized exception class,
`stats["verifier_faults"]`, and a tracer record. It deliberately does NOT send a
wire `type:error`, because the Loqui client treats every wire error as fatal and
would close the socket / lose the generation.

## Empty ASR response (separate, minimal)

`asr.OllamaChat.decode` now requires a completed contract (`done is True`,
`message` object, `message.content` string) and treats empty/whitespace content
(and the official `language None<asr_text>` marker) as a valid no-transcription
result, not an error. Missing/malformed fields, `done=false`, and HTTP/transport
failures remain errors. Valid empty results emit no final and call no aligner;
commit still ACKs (finals-before-ACK preserved).

## Accepted limitations

- The old energy gate can still miss quiet speech; Silero does not recover those
  misses. This stage is ghost suppression only.
- The owner's real two-source 87 s meeting was accepted with small errors and
  quiet-speech flaws explicitly accepted. This is not a claim of zero
  hallucinations.
- Noise/typing validation used synthetic fixtures plus public speech, not a
  real-world noise campaign.

## Tests

- `tests/test_silero_verify.py`: injected accept/reject/fault, rejected
  partial/final never calling ASR, rejected commit ACKing with advancing clock,
  accepted finals keeping timestamps, startup failure when unavailable, env
  off-switch, shared-verifier two-session reset isolation.
- `tests/test_asr_contract.py`: valid text, valid empty/whitespace/no-speech
  marker, missing/malformed/truncated fields, HTTP/transport errors, and a live
  WS valid-empty commit that ACKs with no wire error and no disconnect.
- Core suite: **69 passed** (`--ignore` the local-only aligner-cache/diag/trace
  modules per README).

## Rollback (BOTH Silero commits)

```bash
git -C /opt/40zen/services/4scribe revert --no-edit \
  cce6479ebb0549d248b8f40de2ee2fd35d78e913 \
  584815b32ef6b7f1dfcab25cb2578ec7604d8d6b
sudo systemctl restart fourscribe.service
```

Reverting only `cce6479` would reintroduce the fatal wire error; the command
above removes both the trial and the fault-path fix. For an immediate disable
without reverting, use `FSCRIBE_SILERO_ENABLED=0` plus a service restart.
