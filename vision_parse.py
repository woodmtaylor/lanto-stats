"""
vision_parse.py — read the drawn levels off one setup chart.

Sends the local chart image + the trade's surrounding messages to the model and
returns the levels he drew. The chart shows an entry line/zone, a red stop zone,
and a green target zone; reading the stop is the key number (it sets 1R).

Needs ANTHROPIC_API_KEY. Vision accuracy on small axis numbers is good but not
perfect, so each result includes the chart's printed date (cross-checked against
the message timestamp) and a confidence flag.

Set MODEL=claude-opus-4-8 for best chart-reading accuracy (recommended for vision).
"""

import os, json, re, time, base64
import urllib.request

MODEL = os.environ.get("MODEL", "claude-opus-4-8")
API_URL = "https://api.anthropic.com/v1/messages"

PROMPT = """This is a TradingView screenshot of an ES or NQ futures setup that the trader posted with his entry. Read the levels he has DRAWN on the chart.

The chart usually shows: an entry line/zone, a RED zone (stop-loss side), and a GREEN zone (target side). Use the price axis on the right to read values.

Return ONLY a JSON object (no prose, no markdown):
- instrument: "ES" or "NQ" (NQ prices ~18000-36000, ES ~4000-9000; also shown on chart e.g. "NQ1!")
- direction: "long" or "short" (green/target above entry = long; below = short)
- entry: the entry price (the line between the green and red zones), number
- stop: the far edge of the RED zone (the stop), number
- tp1: the near edge / first marked level of the GREEN zone if distinguishable, else null
- tp2: a further green level if marked, else null
- tp3: a still-further level if marked, else null
- chart_date: the date printed on the chart's time axis if visible (e.g. "2026-05-12"), else null
- confidence: "high" | "medium" | "low" (low if zones/axis are hard to read)

The trader's messages around this chart (for context on direction/instrument):
"""


def _media_type(path):
    ext = path.lower().rsplit(".", 1)[-1]
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}.get(ext, "image/png")


def read_chart(image_path, context_text):
    with open(image_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode()
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 600,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": _media_type(image_path), "data": b64}},
                {"type": "text", "text": PROMPT + context_text},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"content-type": "application/json",
                 "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            txt = "".join(b.get("text", "") for b in data.get("content", [])
                          if b.get("type") == "text")
            txt = re.sub(r"^```json|```$", "", txt.strip(), flags=re.M).strip()
            return json.loads(txt)
        except Exception as e:
            if attempt == 3:
                return {"error": str(e), "confidence": "low"}
            time.sleep(2 * (attempt + 1))
