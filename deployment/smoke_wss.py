#!/usr/bin/env python
"""Authenticated WSS smoke + auth-rejection checks for the 4scribe deployment.

Real TLS is always on ("no -k"): the HTTPS transport is only reachable from a
tailnet MEMBER; running this from a NON-member (or the host itself, whose
:443 locally terminates on a different proxy) must FAIL loudly rather than
skip TLS. Loopback preflight uses a ws:// URL so no TLS claim is made there.
Public fixtures only, no cloud calls, no private recordings. Never prints the
token (token is read in-process from the file; not a CLI value).

Usage:
    .venv/bin/python deployment/smoke_wss.py \
        --url ws://127.0.0.1:8093/4scribe/v1/audio/transcriptions/realtime \
        --token-file /opt/40zen/secrets/4scribe.env \
        --fixture /tmp/4scribe-backend-eval/ru_short.wav --fixture ru \
        --fixture /tmp/4scribe-backend-eval/jfk.wav --fixture en
  fixtures are given as (path, language) pairs; the file is envfile format
  (FSCRIBE_TOKEN=<value>); a single non-empty line is used verbatim.

Checks (hard asserts inside; non-zero exit on any failure):
- handshakes without a valid Bearer are refused (HTTP 401/403 at handshake on
  real servers; a 1008 close before accept also counts); anything else
  (502, open sockets, TLS/cert/transport errors) FAILS;
- session.updated APPLIED contract of THIS service (session.py): language and
  `words` boolean (NOT word_timestamps) must be truthfully True here;
- paced public-fixture streaming yields at least one provisional revision
  (delta) with ADVANCING audio_processed, no error events;
- ALL completed finals carry a non-empty transcript, word list COVERS the
  tokenized final text (punctuation stripped exactly as the aligner library
  does; missing/reordered/extra words FAIL), finite/ordered/non-overlapping
  times within the audio duration, and strictly precede the REQUIRED commit ACK.
"""
import argparse
import asyncio
import json
import math
import sys
import wave

import websockets

DEFAULT_TEST_TOKEN = "test-token"


def _header_kwargs(headers):
    """websockets version compatibility (header kw arg renamed across majors)."""
    import inspect
    sig = inspect.signature(websockets.connect)
    for name in ("additional_headers", "extra_headers"):
        if name in sig.parameters:
            return {name: headers}
    raise RuntimeError("unsupported websockets client version")


async def expect_reject(url, name, headers):
    """Accepted results must be: handshake-refused (401/403) or a 1008 close.
    Anything else - open silent socket, 502, TLS error, timeout - is a FAIL."""
    try:
        ws = await asyncio.wait_for(websockets.connect(url, **_header_kwargs(headers)), timeout=15)
    except websockets.exceptions.InvalidStatus as e:
        code = getattr(getattr(e.response, "status_code", None), "value", None) \
            or getattr(e.response, "status_code", "")
        if code in (401, 403):
            print(f"[PASS] {name}: handshake refused ({code})")
            return True
        print(f"[FAIL] {name}: unexpected handshake status {code}")
        return False
    except websockets.exceptions.InvalidHandshake as e:
        print(f"[FAIL] {name}: unexpected handshake failure {type(e).__name__}")
        return False
    except asyncio.TimeoutError:
        print(f"[FAIL] {name}: handshake neither accepted nor refused (timeout)")
        return False
    except Exception as e:  # noqa: BLE001 - cert/TLS/transport errors FAIL
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
        return False
    try:
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"[FAIL] {name}: session served without authorization")
            return False
        except websockets.exceptions.ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd is not None else None
            print(f"[PASS] {name}: closed before accept (code {code})")
            return code == 1008
        except asyncio.TimeoutError:
            print(f"[FAIL] {name}: open and silent without explicit reject")
            return False
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def _norm_tokens(tokens):
    """Library-consistent tokenization (qwen_asr tokenize_space_lang): only
    letters/digits/apostrophes are kept per item. The aligner therefore
    returns final-text words with PUNCTUATION STRIPPED (case preserved) -
    documented in the backend decision (punctuation/case do not interfere).
    Comparison must use the same normalization, missing/reordered/extra words
    still FAIL."""
    import unicodedata
    out = []
    for t in tokens:
        cleaned = "".join(ch for ch in t if ch == "'" or unicodedata.category(ch)[0] in ("L", "N"))
        if cleaned:
            out.append(cleaned)
    return out


def _words_ok(words, transcript, audio_processed):
    """Client word-time invariants (plan 6.1) + token correspondence modulo
    punctuation: finite, 0 <= start <= end, ordered/non-overlapping, inside
    audio bounds, and the word sequence equals the normalized final text
    tokens (missing/reordered/extra = FAIL)."""
    want = _norm_tokens(transcript.split())
    if not want:
        return "no transcript text"
    got = [w.get("word") for w in words]
    if got != want:
        missing = [t for t in want if t not in got][:3]
        extra = [t for t in got if t not in want][:3]
        return f"word list does not match tokenized final text (missing {missing}, extra {extra})"
    prev_end = None
    for w in words:
        a, b = w.get("start"), w.get("end")
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
            return "non-numeric word time"
        if not (math.isfinite(a) and math.isfinite(b)) or a < 0 or b < a:
            return "invalid word interval"
        if prev_end is not None and a < prev_end - 1e-6:
            return "overlapping/unordered word times"
        if b > audio_processed + 0.05:
            return "end time exceeds audio_processed (+0.05 s)"
        prev_end = b
    return ""


async def auth_session(url, token, fixture_path, language, ack_timeout_s: float = 180.0,
                       require_partials: bool = True):
    """Paced authenticated session: session.update -> realtime-paced PCM
    frames -> client-side commit -> collect until the REQUIRED ACK event.

    ``require_partials=False`` is for short fixtures where a sub-2.5 s phrase
    can legitimately close before the first partial cadence; final/words/ACK
    checks remain mandatory.
    """
    with wave.open(fixture_path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            print(f"[FAIL] fixture {fixture_path}: must be PCM16 mono 16 kHz")
            return False
        pcm = w.readframes(w.getnframes())
        audio_processed = w.getnframes() / w.getframerate()
    headers = {"authorization": "Bearer " + token}
    events = []
    problems = []
    ws = await asyncio.wait_for(
        websockets.connect(url, open_timeout=15, **_header_kwargs(headers)), timeout=20)
    try:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "sample_rate": 16000, "language": language, "endpointing_ms": 800,
            "word_timestamps": True}}))

        async def reader():
            while True:
                ev = json.loads(await ws.recv())
                events.append(ev)
                if ev.get("type") == "input_audio_buffer.committed":
                    return

        rt = asyncio.create_task(reader())
        # real-time pacing in 100 ms frames: live partials are the contract.
        # Reader cancellation is safe: on timeout the inner reader task is
        # cancelled and re-awaited with suppression; Task.exception() is only
        # consulted on a completed non-cancelled task, so CancelledError
        # never escapes.
        for off in range(0, len(pcm), 3200):
            await ws.send(pcm[off:off + 3200])
            await asyncio.sleep(0.1)
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        try:
            await asyncio.wait_for(asyncio.shield(rt), timeout=ack_timeout_s)
        except asyncio.TimeoutError:
            rt.cancel()
            try:
                await rt  # re-await the cancelled reader quietly
            except BaseException:
                pass
        if rt.done() and not rt.cancelled():
            exc = rt.exception()
            if exc is not None:
                raise exc
        ack_required = any(e.get("type") == "input_audio_buffer.committed" for e in events)
        if not ack_required:
            problems.append("required commit ACK event never arrived")

        upd = next((e for e in events if e.get("type") == "session.updated"), None)
        deltas = [e for e in events if e.get("type").endswith(".delta")]
        finals = [e for e in events if e.get("type") == "conversation.item.input_audio_transcription.completed"]
        errs = [e for e in events if e.get("type") == "error"]
        ack = next((e for e in events if e.get("type") == "input_audio_buffer.committed"), None)

        if errs:
            problems.append(f"error event(s): {errs[:2]}")
        if upd is None:
            problems.append("no session.updated")
        else:
            applied = upd.get("applied") or {}
            if applied.get("language") != language:
                problems.append(f"session.updated applied language {applied.get('language')!r} != {language!r}")
            # wire contract of THIS service (session.py): `words` boolean,
            # NOT word_timestamps. Truthful capability must be True here.
            if applied.get("words") is not True:
                problems.append(f"session.updated applied words {applied.get('words')!r} is not True")
        if not deltas:
            if require_partials:
                problems.append("no provisional revisions (deltas) during paced speech")
        else:
            aps = [d.get("audio_processed") for d in deltas]
            if any(not isinstance(a, (int, float)) for a in aps):
                problems.append("delta missing numeric audio_processed")
            elif aps[-1] <= aps[0] and len(aps) > 1:
                problems.append("audio_processed not advancing across deltas")
        final_all_before_ack = bool(finals) and ack_required and \
            events.index(finals[-1]) < events.index(ack)
        if not finals:
            problems.append("no completed final")
        else:
            if not ack_required:
                pass  # recorded above (ACK required; missing is already a problem)
            for f in finals:
                if events.index(f) > events.index(ack):
                    problems.append("a completed final occurred after the commit ACK")
                text = f.get("transcript") or ""
                if not text.strip():
                    problems.append("final transcript empty")
                werr = _words_ok(f.get("words") or [], text, f.get("audio_processed") if f.get("audio_processed") is not None else audio_processed)
                if werr:
                    problems.append(f"word check: {werr}")
                # sanitized diagnosability ONLY: alignment status/reason/
                # detail from the service (no credentials/private audio).
                al_meta = f.get("alignment") or {}
                if al_meta.get("status") in ("fault", "unavailable"):
                    problems.append("aligner fault (reason={} detail={})".format(
                        al_meta.get("reason", "<unset>"),
                        al_meta.get("detail", "<unset>")))

        summary = {"language": language, "updates": upd and (upd.get("applied") or {}),
                   "deltas": len(deltas),
                   "final_text": finals[-1].get("transcript") if finals else None,
                   "words": len(finals[-1]["words"]) if finals else 0,
                   "finals_before_ack": final_all_before_ack}
        print(json.dumps(summary, ensure_ascii=False))
        if problems:
            for p in problems:
                print(f"[FAIL] {language}: {p}")
        else:
            print(f"[PASS] {language}: authenticated realtime contract (rejects errors, deltas advance, final valid and precedes ACK)")
        return not problems
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True,
                    help="wss://.../4scribe/v1/audio/transcriptions/realtime (real TLS) or loopback ws:// for preflight")
    ap.add_argument("--token-file", required=True,
                    help="read-only path to the credential file; read in-process, never printed")
    ap.add_argument("--fixture", action="append", required=True,
                    help="public PCM16/16k mono WAV fixture; given as path then language code")
    ap.add_argument("--allow-no-deltas", action="store_true",
                    help="short fixture may legitimately close before the first partial cadence; final/words/ACK remain mandatory")
    args = ap.parse_args()
    ok = False
    try:
        with open(args.token_file) as f:
            raw = f.read()
    except OSError as e:
        print(f"[FAIL] cannot read token file: {e.strerror}")
        return 2
    token = ""
    for line in raw.splitlines():
        if line.startswith("FSCRIBE_TOKEN="):
            token = line.split("=", 1)[1].strip()
            break
    if not token:
        print("[FAIL] token file has no FSCRIBE_TOKEN line")
        return 2
    if token == DEFAULT_TEST_TOKEN:
        print("[FAIL] production must reject the default test token")
        return 2

    ok = True
    for name, headers in (("no-token", {}),
                          ("wrong-token", {"authorization": "Bearer wrong-token"}),
                          ("default-test-token", {"authorization": "Bearer " + DEFAULT_TEST_TOKEN}),
                          ("no-bearer-scheme", {"authorization": token})):
        ok &= await expect_reject(args.url, name, headers)

    if not ok:
        print("AUTH-PHASE FAILED; not proceeding to session checks")
    for i in range(0, len(args.fixture), 2):
        ok &= await auth_session(args.url, token, args.fixture[i], args.fixture[i + 1],
                                 require_partials=not args.allow_no_deltas)
    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
