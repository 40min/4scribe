> Historical backend-eval manifest, preserved 2026-10-20 from the removed scratch `/tmp/4scribe-backend-eval`. Public fixtures only; no owner audio, raw transcripts, credentials or full pip freeze were copied. Commands and paths below are historical.

# 4scribe eval manifest — 2026-10-16

Изолированная eval-среда этапа 1 задачи Dart o9VVYfQwKGga. Без credentials.
Документы решения: /opt/40zen/docs/4scribe-backend-decision.md,
план: /opt/40zen/docs/stt-service-creation-plan-2026-10-16.md (редакция 5),
review Codex: /opt/40zen/docs/4scribe-backend-handoff-review-2026-10-16.md.

## 1. Runtime / версии (venv: /tmp/4scribe-backend-eval/venv, python 3.12.13)

- torch 2.14.1+cpu, torchaudio 2.11.0+cpu (CPU-only, без CUDA-пакетов)
- transformers 4.57.6, qwen-asr 0.0.6, qwen-omni-utils 0.0.9, numpy 2.5.4
- huggingface_hub 0.36.2, librosa 1.0.0, soundfile 0.14.0, safetensors 0.8.0
- Полный freeze: см. `pip-freeze.txt` рядом с этим файлом
- Ollama host: 0.32.12 (systemd, config не менялся), модель ASR frozenlab/qwen3-asr:1.7b digest bded0cc5db8a…

## 2. Aligner модель (HF_HOME=/tmp/4scribe-backend-eval/hf)

- Qwen/Qwen3-ForcedAligner-0.6B, snapshot dir `hf/hub/models--Qwen--Qwen3-ForcedAligner-0.6B/snapshots/c7cbfc2048c462b0d63a45797104fc9db3ad62b7`
- model.safetensors: 1,835,544,544 B, sha256 `47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7`
- Рабочая конфигурация: dtype=bfloat16, device_map="cpu"; timestamps идентичны fp32, RAM peak 2.6 GB (fp32 5.9 GB — не использовать)
- Cold load bf16: ~3.0 c; warm align 0.3–1.0 c (6–40 c сегмент); 2 одновременных ≤2.1 c

## 3. Команды запуска

```bash
# ASR (batch/rolling final): POST http://127.0.0.1:11434/api/chat
#   model=frozenlab/qwen3-asr:1.7b, stream=false, keep_alive=600,
#   options={temperature:0, num_predict:2048, num_ctx:8192},
#   messages=[{role:"user", content:"", images:["<b64 WAV PCM16 mono 16kHz ≤60s>"]}]
#   ЯВНЫЙ ЯЗЫК: добавить {"role":"assistant","content":"language Russian<asr_text>"} или English
#   Ответ: "language <Lang><asr_text><text>" — отделять по <asr_text>

cd /tmp/4scribe-backend-eval
./venv/bin/python asr.py ru_short.wav          # одиночный batch-запрос (autodetect)
./venv/bin/python rolling.py                   # rolling decode: revisions, seam, 2 потока
./venv/bin/python -c "import align; m,_,_=align.load_model(); print(align.align_one(m,'jfk.wav','And so, my fellow Americans, ask not what your country can do for you; ask what you can do for your country.','English'))"
```

Параметры rolling-прототипа: растущее окно full re-decode; prefix/rollback — assistant-prefill
«language X<asr_text>» + гипотеза минус ~5 токенов (прототип: 20–24 символа, в проде — токенайзер Qwen);
bounded-окно с slide (RU 0–20→8–26, EN 0–16→6–22).

## 4. Fixtures (public, без записей владельца) и результатов — sha256

| Файл | sha256 |
|---|---|
| ru_short.wav (6.4s, VoxForge Amin ru_0068) | bd23541559ac7a815d34417ad8c8632b7fbb1edfc06e0a83915213eb7dae713d |
| ru_cont.wav (34.2s, 5 предл., паузы 0.5s) | 36a632d2739f612653e3cfb62a4ddc9c351a3afcad3c19de9ea24794ea7226b4 |
| ru_nogap.wav (38.5s, непрерывно) | 97bdd24b2570a7ce522a3084af1615463d6d76ad6c0b6fc5a0ee4b446383c7a3 |
| ru_tail_cut.wav (27.7s, обрыв в фразе) | 7bed66111c7121c57da24e5830880a5b5c6d52799f19f72bd71da699aeb43e1c |
| en_cont.wav (40.6s, 5 предл., паузы 0.5s) | ab45487f592fd9dd1fcf245356c30c3f808a1312efb72e49413be23b928a7c6d |
| en_nogap.wav (31.1s, непрерывно) | be7ad0b9e107da19ef10667688960060d87c1052ba69ff9e4d8fe39aabbc46ac |
| jfk.wav (11s, whisper.cpp sample) | 59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e |
| silence.wav (5s) | 262d1f34f300560a4c82ba8127ddea9e1d72946f187fdbd3e8f70ee096077aec |
| asr.py | 2eb90d0f302020867967764a5da8ffecb7da424502acee7f4b64a8423ce66b41 |
| rolling.py | 1327fe576771f4184b7010d96321fb650b00c303e02b1a58ccbc35eb0a126a9a |
| align.py | b541e6bcba4faadf1bac312f8144e9af86682bace8b3f04620d7b72b77ddb9de |
| final_texts.json (exact ASR finals для align) | cf353a73b896c4278223ef44c5619f563e06f8c575d8fa9a6acb2370d574acfb |
| align_results.json | ed5dfc74f1ec1dac8b600de80d722f23a5f04cf0c25cf76785b87d9a31884a11 |
| latency_baseline.json | b2354b6bd49257df1cf1fde54d599f7035e999675009ca4684c560c4b892b567 |
| qwen3_asr_inf.py (официальный source для сверки) | 0b1770f8e907b6c5a0a1e9ebce037cb63f48555f3cd15eaf6ea2078e9df41a7b |

Источники fixtures: VoxForge repo (repository.voxforge1.org) RU `Amin-20110509-hio.tgz` / EN
`1snoke-20120412-hge.tgz` (оригиналы: ru_spk.tgz, en_spk.tgz; известные тексты — etc/PROMPTS);
jfk.wav — github ggml-org/whisper.cpp samples. Композиты собраны stdlib-wave (см. историю в git? нет —
скрипт сборки в выводах сессии; правила сборки: 1.0s lead-in + N предложений + 0.5s паузы).

## 5. Footprint

- Disk: всего 3.7 GB = venv 2.0 GB + hf 1.8 GB + fixtures/результаты ~10 MB
- RAM при работе: aligner bf16 peak 2.6 GB; ASR-модель Ollama 2.5 GB через штатный keep_alive (сейчас выгружена; residency чужих моделей не менялась)
- Процессы по завершении: собственных экспериментальных процессов нет

## 6. Статус

- Этап 1 backend-доказательства завершены (decision §8, Go по capability)
- Open gates: owner latency-acceptance на replay; сервисный lifecycle; правки плана по review Codex
  (drain вариант (б), max segment ≤20–25 c, delta audio_processed > last_final_end, event_id на коннекцию)
  — правки вносит следующий агент; remote.py/vad.py/types.py/session-doc/replay-pipeline с Mac — список в комментарии задачи
- Этап 2 не начат; production не менялся
