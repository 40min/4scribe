"""Race regression (owner/Codex instruction): a partial job started BEFORE a
commit admission must NOT submit its (stale) executor decode after the commit
job was admitted - the commit job waits behind it FIFO (decode order ==
emission order preserved by decode_lock), so the fence waits on the stale
decode. Deterministic: the session is built with an EXTERNALY HELD global
decode semaphore; the partial starts (sem unavailable), the commit job is
admitted, the semaphore is released, and the partial must decline BEFORE
submission (not-yet-started work only; submitted compute never canceled)."""
import asyncio
import concurrent.futures as cf
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fourscribe.config import Config  # noqa: E402
from fourscribe.session import RealtimeSession  # noqa: E402
from fourscribe.asr import OllamaChat  # noqa: E402
from tests.conftest import FakeAligner, speech_pcm  # noqa: E402
from tests.mock_ollama import MockOllama  # noqa: E402


class RecordingSink:
    def __init__(self):
        self.events = []

    def __call__(self, ev):
        self.events.append(ev)


class _AdaptedAsr:
    """OllamaChat around the mock transport so decode requests count."""

    def __init__(self, mock):
        self._chat = OllamaChat(
            Config(token="x", ollama_url="http://mock-ollama"),
            http=httpx.Client(transport=mock.transport(),
                              base_url="http://mock-ollama"))


async def build_session():
    mock = MockOllama(delay_s=0.0)
    cfg = Config(token="x", ollama_url="http://mock-ollama",
                 min_endpointing_ms=400, partial_interval_s=0.0)
    sink = RecordingSink()
    sem = asyncio.Semaphore(1)
    pool = cf.ThreadPoolExecutor(max_workers=2)
    align_pool = cf.ThreadPoolExecutor(max_workers=1)
    asr = OllamaChat(cfg, http=httpx.Client(transport=mock.transport(),
                                            base_url="http://mock-ollama"))
    session = RealtimeSession(cfg, asr, FakeAligner(), pool, sink,
                              align_pool=align_pool, decode_sem=sem)
    await session.handle_session_update(
        {"type": "session.update",
         "session": {"sample_rate": 16000, "language": "ru",
                     "endpointing_ms": 400, "word_timestamps": True}})
    return session, mock, sink


async def race_scenario():
    session, mock, sink = await build_session()
    # hold the GLOBAL decode semaphore from outside (a peer session's decode)
    await session._decode_sem.acquire()
    # 1) start the partial the way maybe_partial() would (final_jobs empty)
    await session.handle_binary(speech_pcm(1.5))
    partial_task = asyncio.create_task(session.partial_job(
        bytes(session.segbuf), session.epoch_samples))
    await asyncio.sleep(0.05)  # partial is inside run_decode, waiting on sem
    # 2) commit job ADMISSION: fence will wait FIFO behind the partial
    # (1600 samples = 1600 B mono s16 = 0.1 s of nonempty silence-tail PCM)
    session._admit_final(b"\x00\x00" * 1600,
                         session.epoch_samples, session.epoch_samples, True)
    assert any(j[3] for j in session.final_jobs)
    # 3) release the sem: post-fix, the partial declines BEFORE submission
    await asyncio.sleep(0.02)
    session._decode_sem.release()
    await asyncio.wait_for(partial_task, timeout=10)
    # finish the admitted commit job queue (its final decodes + ACK)
    session._ensure_runner()
    deadline = asyncio.get_event_loop().time() + 8
    want = "input_audio_buffer.committed"
    while (not any(e.get("type") == want for e in sink.events)
           and asyncio.get_event_loop().time() < deadline):
        await asyncio.sleep(0.02)
    return session, mock, sink


def test_stale_partial_declines_before_submission_after_commit_admission():
    async def run():
        return await race_scenario()

    session, mock, sink = asyncio.run(run())
    assert session.stats["declined_stale_partial"] >= 1, \
        f"stale partial was NOT declined: {session.stats}"
    # exactly ONE decode request: the admitted commit job's nonempty tail;
    # the stale partial(s) must NOT add submissions
    assert len(mock.requests) == 1, \
        f"expected only the commit decode; got {len(mock.requests)}"
    kinds = [e.get("type") for e in sink.events]
    assert kinds.count("input_audio_buffer.committed") == 1
    assert "conversation.item.input_audio_transcription.completed" not in kinds \
        or kinds.count("conversation.item.input_audio_transcription.completed") <= 1


def test_commit_pending_lifecycle_cancel_close_and_finish():
    async def run():
        session, mock, sink = await build_session()
        # (1) normal finish: the runner clears the flag once the commit is done
        session._admit_final(b"\x00\x00" * 1600, session.epoch_samples,
                             session.epoch_samples, True)
        session._ensure_runner()
        deadline = asyncio.get_event_loop().time() + 8
        while (not any(e.get("type") == "input_audio_buffer.committed"
                       for e in sink.events)
               and asyncio.get_event_loop().time() < deadline):
            await asyncio.sleep(0.02)
        assert session.commit_pending is False, "finish must clear flag"
        # (2) cancel unblocks immediately
        session._admit_final(b"\x00\x00" * 1600, session.epoch_samples,
                             session.epoch_samples, True)
        assert session.commit_pending is True
        await session.handle_cancel()
        assert session.commit_pending is False
        # (3) close clears unconditionally (even admitted after close)
        session._admit_final(b"\x00\x00" * 1600, session.epoch_samples,
                             session.epoch_samples, True)
        await session.close()
        assert session.commit_pending is False
        return session, mock, sink

    session, mock, sink = asyncio.run(run())
    assert any(e.get("type") == "input_audio_buffer.committed"
               for e in sink.events)
