"""Structured slide extraction for the Knowledge Library.

Produces a canonical, per-slide JSON ("slides.json") as the source of truth,
via a tiered extractor: a free local vision model (Qwen3-VL over Ollama) on
every slide, escalating only hard visual slides to Claude Haiku. Downstream
chunking (slide / section / summary) reads slides.json — so re-chunking never
re-extracts.
"""
