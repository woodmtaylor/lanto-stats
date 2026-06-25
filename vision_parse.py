"""
vision_parse.py — read the drawn levels off one setup chart.

Sends the local chart image + the trade's surrounding messages to the model and
returns the levels he drew (entry line, red stop zone, green target zone). The
image is normalized to a clean PNG first, so any format the downloader saved
(WebP from forwarded embeds, odd extensions, oversized screenshots) still works.
On failure it returns a SPECIFIC error string so the daily log shows the real
cause instead of a generic parse error.

Needs ANTHROPIC_API_KEY. MODEL defaults to claude-opus-4-8 (best for vision).
"""
import os, json, re, time, base64, io
import urllib.request, urllib.error

MODEL = os.environ.get("MODEL", "claude-opus-4-8")
API_URL = "https://api.anthropic.com/v1/messages"
MAX_EDGE = 1568  # Anthropic's recommended max long edge

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

If this image is not a setup chart with drawn zones (e.g. it's a commentary or review screenshot), return {"entry": null, "confidence": "low"}.

The trader's messages around this chart (for context on direction/instrument):
"""


def _load_image_b64(path):
    """Normalize any image to a clean, reasonably sized PNG (handles WebP/JPEG/
    odd extensions). Raises if the file isn't a decodable image."""
    from PIL import Image
    im = Image.open(path)
    im.load()
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    long_edge = max(im.size)
    if long_edge > MAX_EDGE:
        s = MAX_EDGE / long_edge
        im = im.resize((max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s))))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _extract_json(txt):
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S)
    m = re.search(r"\{.*\}", txt, re.S)  # first JSON object, even if prose-wrapped
    return json.loads(m.group(0) if m else txt)


def read_chart(image_path, context_text):
    try:
        b64 = _load_image_b64(image_path)
    except Exception as e:
        return {"error": f"bad image: {e}", "confidence": "low"}

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 600,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/png", "data": b64}},
                {"type": "text", "text": PROMPT + (context_text or "")},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"content-type": "application/json",
                 "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"})

    last = "no response"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            if data.get("type") == "error":
                last = "api error: " + str(data.get("error", {}).get("message", "?"))[:140]
                break
            txt = "".join(b.get("text", "") for b in data.get("content", [])
                          if b.get("type") == "text")
            if not txt.strip():
                last = f"empty reply (stop={data.get('stop_reason')})"
                break
            try:
                return _extract_json(txt)
            except Exception:
                last = "non-JSON reply: " + repr(txt[:140])
                break  # deterministic; retrying won't help
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:200].decode("utf-8", "replace")
            except Exception:
                detail = ""
            last = f"HTTP {e.code}: {detail}"
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            break
        except Exception as e:
            last = str(e)
            if attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            break
    return {"error": last, "confidence": "low"}
