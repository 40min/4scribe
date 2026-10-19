"""Ollama chat decode client (stage-1-verified request shape).

Request per decision doc 2.1/2.2 / plan 5: POST /api/chat, stream=false,
options {temperature:0, num_predict, num_ctx}, messages = [user(content="",
images=[b64 wav])] (+ optional trailing assistant prefill
`language Russian<asr_text>` / `language English<asr_text>`). Response contains
`language <Lang><asr_text>...` on unforced paths; the forced path yields the
continuation only. Blocking HTTP work happens in the caller thread (the service
runs this through a bounded thread pool, never on the event loop).
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
        content = (payload.get("message") or {}).get("content") or ""
        wall = time.perf_counter() - t0
        if payload.get("done") is False or not content:
            raise OllamaDecodeError("empty/undone ollama response")
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
