# Silero pre-decode verification trial (owner test)

**✅ Change:** added a shared `fourscribe.verifier` seam (`src/fourscribe/verifier.py`).
`app.create_app()` builds one verifier per process and injects it into every
session. Sessions call it on the **exact segment PCM** before ASR, for partials
and finals. Rejection means: no partial text, no final text, no aligner call.
A commit still ACKs and the epoch clock continues.

**🔧 Behaviour preserved:** energy segmentation, onset/endpointing/hangover,
absolute clock, buffer/ring, queue/drain ordering, commit-before-ACK ordering,
and word timestamps are unchanged. The verifier only gates emission; it never
trims, re-times, concatenates, or mutates session state.

**🔧 Dependency:** `silero-vad==6.2.3`, wheel SHA256
`7b7f5436cfcb02fae583a05b512ea96467fd449fe54cb49a5e4f06c51a1e43b8`,
license MIT (wheel `dist-info/LICENSE`), installed into the production venv with
`pip install --no-deps`. Runtime deps `packaging` and `torch` were already
present; no torch/ONNX/torchaudio/runtime/container change.

**🔧 Activation:** env-driven production startup (`Config.from_env`) enables the
verifier by default, so the normal `uvicorn fourscribe.app:create_app --factory`
launch is active without extra config. Explicit `Config()` construction (tests,
embedding) defaults to off. **Off switch:** set `FSCRIBE_SILERO_ENABLED=0` and
restart; `get_verifier` then returns the accept-all `NullVerifier`.

**⚠️ Failure policy:** if the verifier is enabled but its package/model cannot
load, startup fails explicitly (`VerifierUnavailable`) - never a silent
rejection path. A verifier fault at runtime emits an explicit `verifier_error`
wire event and fails **open** (audio is still decoded): a broken verifier cannot
delete real speech.

**⚠️ Accepted limitation:** the old energy gate can still miss quiet speech; this
trial tests ghost suppression only and does not recover those misses. The
separate empty-ASR protocol fix is deliberately NOT part of this trial.

**🧪 Tests:** `tests/test_silero_verify.py` covers injected accept/reject/fault,
rejected partial/final never calling ASR, rejected commit still ACKing and the
clock continuing, accepted finals keeping timestamps, startup failure when
enabled but unavailable, the env off switch, and two sessions sharing one
verifier without state-reset overlap. Existing core suite: 54 passed.

**🧪 Real-model smoke (no ASR):** `fixtures` from the isolated experiment:
real EN/RU speech and quiet digital-gain variants accepted; 60 ms 220 Hz tone,
click train, typing-like bursts, pink noise and silence rejected. See
`/tmp/opencode/4scribe-vad-shadow/results/verifier_smoke.json`.

**↩️ Rollback:** find the trial commit and revert it, then restart:

```bash
git -C /opt/40zen/services/4scribe log --oneline -5
git -C /opt/40zen/services/4scribe revert --no-edit \
  "$(git -C /opt/40zen/services/4scribe log --grep='trial: Silero predecode' -1 --format=%H)"
```

For an immediate disable without reverting: `FSCRIBE_SILERO_ENABLED=0` plus a
service restart. The installed `silero-vad` package may stay; the off path does
not import it.
