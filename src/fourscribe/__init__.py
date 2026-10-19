"""4scribe: local STT service for Loqui (stage-2 isolated implementation).

Bound to loopback only. Inference via local Ollama (frozenlab/qwen3-asr:1.7b).
Word timestamps via internal forced aligner seam (see aligner.py).
"""
