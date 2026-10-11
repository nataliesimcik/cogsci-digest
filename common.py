"""Shared helpers: config, Claude calls, JSON parsing."""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"


def load_config():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def parse_json(text, kind="["):
    text = re.sub(r"```(?:json)?", "", text).strip()
    close = "]" if kind == "[" else "}"
    start, end = text.find(kind), text.rfind(close)
    return json.loads(text[start:end + 1])


def ask(client, model, system, user, max_tokens=4000, tools=None):
    """One Claude call. Handles server-tool pauses (web search) and returns the text."""
    messages = [{"role": "user", "content": user}]
    kwargs = {"tools": tools} if tools else {}
    for _ in range(5):
        msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                     messages=messages, **kwargs)
        if getattr(msg, "stop_reason", None) != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": msg.content}]
    # with web search, the answer is the last text block(s) after the tool results
    texts = [b.text for b in msg.content if b.type == "text"]
    return "".join(texts)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
