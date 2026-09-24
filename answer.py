"""Gemini answer engine (Google AI Studio free tier).
Client is created once and reused. Includes retry for free-tier rate limits.
Requires GEMINI_API_KEY in the environment."""
import os
import time
import logging

from google import genai
from google.genai import types

import config

_client = None

# The overlay renders Markdown (fenced code blocks with syntax highlighting,
# tables, lists) and treats $...$ / $$...$$ as math. Ask the model to use them.
_FORMAT_RULES = (
    "\n\nFORMATTING: Reply in GitHub-flavoured Markdown. Use **bold** for the "
    "final answer or chosen option, `inline code` for identifiers and short "
    "expressions, and fenced code blocks with a language tag for any code. "
    "Write mathematics with $inline$ or $$display$$ LaTeX. Use short bullet "
    "lists rather than long paragraphs. Never wrap the whole reply in a code "
    "block and never add a preamble such as 'Sure' or 'Here is'."
)

_SYSTEM_INSTRUCTIONS = {
    "tutor": (
        "You are an expert AI tutor. The user will provide text extracted "
        "from an image via OCR. Clean up any obvious OCR typos, identify "
        "the core question, and provide a clear, step-by-step educational "
        "explanation suitable for a student."
    ) + _FORMAT_RULES,
    "exam": (
        "You are an exam assistant. The user will provide text extracted "
        "from an image via OCR (fix obvious OCR typos). Identify every "
        "question and answer each one. For multiple-choice, give the letter "
        "in bold plus a one-line justification. For open-ended questions, "
        "give the shortest complete correct answer. No preamble, keep it "
        "compact and scannable at a glance."
    ) + _FORMAT_RULES,
}


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def answer_educational_question(ocr_text: str) -> str:
    if not os.environ.get("GEMINI_API_KEY"):
        return "ERROR: GEMINI_API_KEY not set in environment."
    return _generate(ocr_text)


def _generate(ocr_text: str) -> str:
    cfg = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTIONS[config.ANSWER_MODE],
        temperature=config.GEMINI_TEMPERATURE,
    )
    models = [config.GEMINI_MODEL, *config.GEMINI_MODEL_FALLBACKS]
    last_err = None
    for model in models:
        for attempt in range(1, config.GEMINI_MAX_RETRIES + 1):
            try:
                response = _get_client().models.generate_content(
                    model=model, contents=ocr_text, config=cfg)
                return response.text or "(empty response)"
            except Exception as e:
                last_err = e
                logging.warning("[answer] %s attempt %d failed (%s); retrying...",
                                model, attempt, e)
                time.sleep(config.GEMINI_RETRY_DELAY_S)

    return f"(answer failed after retries: {last_err})"



if __name__ == "__main__":
    print(answer_educational_question(
        "Examp1e 3: Find the derivat1ve of f(x) = 3x^2 + 5x - 2 with respect t0 x."))
