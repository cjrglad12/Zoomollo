"""Draft message templates with an AI model that runs on your own computer.

Uses Ollama (free, open source): https://ollama.com. Install it, then run
`ollama pull llama3.2` once. Nothing leaves your computer: Zoomollo talks to
Ollama at http://localhost:11434. Set OLLAMA_MODEL / OLLAMA_URL in
zoomollo-settings.txt to use a different model or address.
"""

from __future__ import annotations

import os
import re

import requests

from .messaging import FIELDS

DEFAULT_MODEL = "llama3.2"

SETUP_HELP = ("AI drafting runs on your own computer with Ollama (free). Install it from ollama.com, "
              "open it, then run `ollama pull llama3.2` once in Terminal / Command Prompt. "
              "Restart Zoomollo afterwards.")


class AIUnavailable(RuntimeError):
    pass


def _url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def _model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)


def available() -> bool:
    try:
        return requests.get(_url() + "/api/tags", timeout=2).ok
    except requests.RequestException:
        return False


PROMPT = """You write short, friendly, professional text messages (SMS) for a recruiter
reaching out to a hiring manager at a company.

Write ONE message template for this goal: {goal}
Tone: {tone}. Length: {length}.

Rules:
- Use these placeholders exactly where the details belong, in curly brackets: {fields}
- Most important: {{first_name}}, {{company}}, {{role}}. Sign off or introduce with {{my_name}} and {{my_company}}.
- Do not invent names, numbers, salaries, candidates' details or facts.
- Plain text only: no subject line, no hashtags, no emojis unless the tone asks for them, no quotes around it.
- End with a simple opt-out such as "Reply STOP to opt out."
- Output only the message.
"""

LENGTHS = {"short": "1-2 sentences, under 280 characters", "medium": "2-4 sentences, under 500 characters"}


def draft_template(goal: str, tone: str = "warm and direct", length: str = "short") -> str:
    """Return a template body drafted by the local model."""
    fields = ", ".join("{" + f + "}" for f in FIELDS)
    prompt = PROMPT.format(goal=goal.strip() or "introduce yourself and ask for a quick call about their open role",
                           tone=tone, length=LENGTHS.get(length, LENGTHS["short"]), fields=fields)
    try:
        r = requests.post(_url() + "/api/generate", timeout=180,
                          json={"model": _model(), "prompt": prompt, "stream": False,
                                "options": {"temperature": 0.7}})
    except requests.RequestException as e:
        raise AIUnavailable(SETUP_HELP) from e
    if r.status_code == 404:
        raise AIUnavailable(f"The AI model '{_model()}' isn't downloaded yet. Run `ollama pull {_model()}` once.")
    r.raise_for_status()
    return clean(r.json().get("response", ""))


def clean(text: str) -> str:
    text = re.sub(r"^\s*(here('| i)s|sure)[^:\n]*:\s*", "", text, flags=re.I)  # "Here's a message:"
    text = text.strip().strip('"\u201c\u201d').strip()
    # Normalise placeholder spellings the model sometimes uses: {{name}}, [first_name], {First Name}
    text = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"{\1}", text)
    text = re.sub(r"[\[{]\s*([A-Za-z][A-Za-z _]*?)\s*[\]}]",
                  lambda m: "{" + m.group(1).strip().lower().replace(" ", "_") + "}", text)
    return text.strip()
