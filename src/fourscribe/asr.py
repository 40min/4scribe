"""Ollama chat decode client (stage-1-verified request shape).

Request per decision doc 2.1/2.2 / plan 5: POST /api/chat, stream=false,
options {temperature:0, num_predict, num_ctx}, messages = [user(content="",
images=[b64 wav])] (+ optional trailing assistant prefill
`language Russian<asr_text>` / `language English<asr_text>`). Response contains
`language <Lang><asr_text>...` on unforced paths; the forced path yields the
continuation only. Blocking HTTP work happens in the caller thread (the service
runs this through a bounded thread pool, never on the event loop).
A completed response with empty/whitespace content is a valid no-transcription
result (returned as empty text), not a decode error; malformed/truncated
responses and HTTP/transport failures stay errors.
"""
import json
import time

import httpx

from .wavwrap import wav_b64

ASR_TEXT = "<asr_text>"
LANG_PREFILLS = {"ru": "language Russian" + ASR_TEXT, "en": "language English" + ASR_TEXT}


class OllamaDecodeError(RuntimeError):
    pass


class DecodeResult:
    __slots__ = ("text", "prompt_tok", "eval_tok", "wall_s")

    def __init__(self, text, prompt_tok, eval_tok, wall_s):
        self.text = text
        self.prompt_tok = prompt_tok
        self.eval_tok = eval_tok
        self.wall_s = wall_s


def split_asr(content: str) -> str:
    if ASR_TEXT in content:
        return content.split(ASR_TEXT, 1)[1].strip()
    return content.strip()


class OllamaChat:
    def __init__(self, cfg, http=None):
        self.cfg = cfg
        self._http = http or httpx.Client(base_url=cfg.ollama_url, timeout=httpx.Timeout(600, connect=10))

    def decode(self, pcm: bytes, language: str = None, num_predict: int = None) -> DecodeResult:
        messages = [{"role": "user", "content": "",
                     "images": [wav_b64(pcm).decode("ascii")]}]
        if language:
            if language not in LANG_PREFILLS:
                raise OllamaDecodeError(f"unsupported forced language: {language!r}")
            messages.append({"role": "assistant", "content": LANG_PREFILLS[language]})
        body = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive_s,
            "options": {
                "temperature": 0,
                "num_predict": num_predict or self.cfg.num_predict,
                "num_ctx": self.cfg.num_ctx,
            },
            "messages": messages,
        }
        t0 = time.perf_counter()
        resp = self._http.post("/api/chat", content=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        payload = resp.json()
        wall = time.perf_counter() - t0
        # Valid-response contract. A completed (done=true) response whose
        # message.content is a string is valid even when it is empty or
        # whitespace: that is a normal "no transcription" result, not an
        # error. Missing/malformed fields and truncated (done=false)
        # responses remain explicit errors, as do HTTP/transport failures.
        if not isinstance(payload, dict):
            raise OllamaDecodeError("malformed ollama response: not an object")
        if payload.get("done") is not True:
            raise OllamaDecodeError("incomplete ollama response (done != true)")
        message = payload.get("message")
        if not isinstance(message, dict):
            raise OllamaDecodeError("malformed ollama response: missing message")
        if not isinstance(message.get("content"), str):
            raise OllamaDecodeError("malformed ollama response: content not a string")
        content = message["content"]
        return DecodeResult(text=split_asr(content),
                            prompt_tok=payload.get("prompt_eval_count"),
                            eval_tok=payload.get("eval_count"),
                            wall_s=round(wall, 3))

    def reachable(self) -> bool:
        try:
            return self._http.get("/api/tags").status_code == 200
        except httpx.HTTPError:
            return False

    def unload(self, timeout=60):
        """Best-effort unload of our ASR model (bounded keep_alive)."""
        try:
            body = {"model": self.cfg.model, "keep_alive": 0}
            self._http.post("/api/generate", content=json.dumps(body).encode(),
                            headers={"Content-Type": "application/json"}, timeout=timeout)
        except httpx.HTTPError:
            pass
