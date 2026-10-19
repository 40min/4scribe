#!/usr/bin/env python3
"""Fetch two pinned PUBLIC trial fixtures into a caller-owned directory.

No private audio, no cloud ASR calls. Sources and SHA256 are pinned from the
accepted Silero experiment manifest; total download is ~2 MB, well under the
20 MB trial cap. Output is PCM16 mono 16 kHz, matching deployment/smoke_wss.py.
"""
import argparse
import hashlib
import json
import pathlib
import urllib.parse
import urllib.request

import numpy as np
import soundfile as sf

try:
    import librosa
except ImportError:  # resampling is only needed if a source is not 16 kHz
    librosa = None

SOURCES = [
    {
        "id": "ru_normal_01",
        "dataset": "istupakov/russian_librispeech",
        "config": "default",
        "split": "test",
        "row": 1,
        "sha256": "e6ba59a17622b32f2cc61c6e783a0b27001cdd403f7f463031b478adbaf8aa0f",
        "license": "CC BY 4.0 (Russian LibriSpeech upstream)",
    },
    {
        "id": "ru_long_01",
        "dataset": "istupakov/russian_librispeech",
        "config": "default",
        "split": "test",
        "row": 0,
        "sha256": "14d2f1b21d7e48e04732ca489e66b6ca084446526d54fbf2d7716b5145f841d5",
        "license": "CC BY 4.0 (Russian LibriSpeech upstream)",
    },
    {
        "id": "en_normal_01",
        "dataset": "hf-internal-testing/librispeech_asr_dummy",
        "config": "clean",
        "split": "validation",
        "row": 0,
        "sha256": "5bc474ec55cd50e7192d793a63c1b456fd205ea05737fc3fdffb99f46f61500a",
        "license": "CC BY 4.0 (LibriSpeech upstream)",
    },
]


def fetch_one(spec, out_dir):
    q = urllib.parse.quote(spec["dataset"], safe="")
    url = (f"https://datasets-server.huggingface.co/rows?dataset={q}"
           f"&config={spec['config']}&split={spec['split']}"
           f"&offset=0&length={spec['row'] + 1}")
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    rows = {x["row_idx"]: x["row"] for x in data["rows"]}
    row = rows[spec["row"]]
    with urllib.request.urlopen(row["audio"][0]["src"], timeout=60) as r:
        raw = r.read()
    tmp = out_dir / (spec["id"] + ".orig")
    tmp.write_bytes(raw)
    try:
        audio, sr = sf.read(tmp, dtype="float32", always_2d=False)
    finally:
        tmp.unlink()
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        if librosa is None:
            raise SystemExit(f"resample required for {spec['id']} but librosa is unavailable")
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    peak = float(np.max(np.abs(audio))) or 1.0
    if peak > 0.89:
        audio = audio * (0.89 / peak)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    path = out_dir / (spec["id"] + ".wav")
    sf.write(path, pcm, 16000, subtype="PCM_16")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != spec["sha256"]:
        raise SystemExit(f"hash mismatch for {spec['id']}: {digest}")
    print(f"{spec['id']} {digest} {spec['license']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="caller-owned fixture directory")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for spec in SOURCES:
        fetch_one(spec, out)


if __name__ == "__main__":
    main()
