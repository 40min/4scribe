"""Deterministic fake Ollama over httpx.MockTransport for hermetic WS tests.

Mirrors the verified chat API shape: user(content="", images=[b64 wav]) with an
optional trailing assistant prefill; the response is
'language <Lang><asr_text>...' on the unforced path and the continuation only
on the forced path. Optional per-call delay supports cancel/generation tests.

Recording/classification support for segmentation tests: every request's WAV
payload is decoded and recorded in `pcm_log`; when `freq_texts` is set, forced
decodes return a text chosen by the DOMINANT FREQUENCY of the audio (so
distinct synthetic phrases yield distinct finals regardless of grouping).
"""
import base64, io, json, time, wave

import httpx

TEXT = {"ru": "Тестовый текст.", "en": "Unforced test text."}
CONT = {"ru": " Финальная гипотеза.", "en": "Forced final hypothesis."}


class MockOllama:
    def __init__(self, delay_s=0.0, freq_texts=None):
        self.delay_s = delay_s
        self.freq_texts = freq_texts or {}
        self.requests = []
        self.pcm_log = []

    def transport(self):
        def handler(request):
            if request.url.path == "/api/chat":
                return self.chat(json.loads(request.content))
            if request.url.path.endswith("/api/generate"):
                return httpx.Response(200, json={})
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": []})
            return httpx.Response(404, json={})
        return httpx.MockTransport(handler)

    def _payload_pcm(self, body):
        try:
            b64 = body["messages"][0]["images"][0]
            with wave.open(io.BytesIO(base64.b64decode(b64))) as w:
                return w.readframes(w.getnframes()), w.getframerate()
        except Exception:  # noqa: BLE001 - malformed payloads are the request's problem
            return b"", 16000

    def _freq_text(self, pcm: bytes, rate: int):
        """Dominant frequency over VOICED frames only (median), so trailing
        silence does not dilute the estimate."""
        if not pcm:
            return None
        n = len(pcm) // 2
        samples = memoryview(pcm).cast("h")
        fs = 320  # 20 ms frames
        frame_hzs = []
        for pos in range(0, n - fs + 1, fs):
            frame = samples[pos:pos + fs]
            rms = (sum(float(s) * float(s) for s in frame) / fs) ** 0.5 / 32768.0
            if rms < 0.02:
                continue
            crossings = sum(1 for i in range(1, fs)
                            if (frame[i - 1] < 0) != (frame[i] < 0))
            frame_hzs.append((crossings / 2) / (fs / rate))
        if not frame_hzs:
            return None
        frame_hzs.sort()
        est = frame_hzs[len(frame_hzs) // 2]
        for f, text in self.freq_texts.items():
            if abs(est - f) <= 40.0:
                return text
        return None

    def chat(self, body):
        self.requests.append(body)
        pcm, rate = self._payload_pcm(body)
        self.pcm_log.append(pcm)
        time.sleep(self.delay_s)
        messages = body.get("messages", [])
        pref = ""
        for m in messages:
            if m.get("role") == "assistant":
                pref = m.get("content") or ""
        if pref:
            freq = self._freq_text(pcm, rate) if self.freq_texts else None
            text = freq if freq is not None else (CONT["ru"] if "language Russian" in pref else CONT["en"])
        else:
            lang = "ru" if self.requests[-1].get("_hint_ru") else "en"
            text = "language " + ("Russian" if lang == "ru" else "English") + "<asr_text>" + TEXT[lang]
            # crude fixture: the fake is forced from the FIRST chat call: ru iff
            # the wav payload bytes contain an audible high amplitude in the
            # first quarter (tests control which by custom text injection below)
        return httpx.Response(200, json={"message": {"content": text}, "done": True,
                                         "prompt_eval_count": 10, "eval_count": 5,
                                         "eval_duration": 1000})
