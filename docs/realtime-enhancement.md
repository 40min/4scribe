# Realtime opt-in correction

New connections default to enhance=false. session.update.session.enhance accepts
a strict JSON boolean. Omission preserves the value. Strings, numbers and null
produce error.code=invalid_enhance without changing it.
session.updated.applied.enhance reports the applied value.

Each raw conversation.item.input_audio_transcription.completed event retains its
transcript, words, alignment and audio_processed, adding a stable connection-unique
segment_id. IDs stay unique across commits. Raw final and commit ACK emission
precede optional correction. Original word timestamps are never rewritten.

A later optional conversation.item.input_audio_transcription.enhanced event has:

| Field | Value |
| --- | --- |
| event_id | Its own unique ID |
| segment_id | The raw segment ID |
| enhanced_text | Corrected string, or null |
| enhancer_error | Boolean |
| enhancer_ms | Measured milliseconds, numeric |
| status | corrected, unchanged, or error |
| error.code | On errors, a stable engine code |

Unchanged output has null enhanced_text and false enhancer_error. Errors have
null enhanced_text and true enhancer_error. Stable codes include enhancement_busy,
enhancement_timeout, enhancement_unavailable, input_too_large and
invalid_model_output. The event has no words or timestamps. Blank or
verifier-declined text skips correction.

There is no per-session queue: at most one wrapper task is tracked; further
segments may receive an optional busy event (enhancer_ms=0). The configured engine
also shares one slot across standalone HTTP, batch and all realtime sessions.
Existing realtime credentials suffice; no extra client request is needed.
Engine configuration requires the existing dedicated enhancement token.

Queued raw finals, ACKs and other nonoptional events take priority over queued
corrections while preserving their own order. Corrections are best effort:
pressure can drop/evict them to preserve raw finals and ACKs; optional corrections
never trigger overload close. Raw-event overload
remains explicit. response.cancel, disabling enhancement and close cancel wrapper
work, invalidate late results and purge queued corrections. An event already
handed to the WebSocket sender cannot be recalled. Commit alone does not invalidate
pending corrections. Each task captures finalized language, choice and generation;
later updates cannot retarget it. Native work remains shielded and holds engine
capacity until completion, even after wrapper cancellation.

Correction never delays raw finals, ACKs or commit draining. Independent locks
do not promise absence of physical CPU/model contention. This is server source
preparation; deployment and client acceptance are separate.
