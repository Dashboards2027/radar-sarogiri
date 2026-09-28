"""Describes the look of image-based trends (memes, Pinterest) with a vision model: aesthetic, format, colors, tags.

Runs in the hourly job when GEMINI_API_KEY (or GROQ_API_KEY as backup) is set. Cached in visual.json by image URL.
"""
import base64
import json
import os
import time
import urllib.request
from pathlib import Path

CACHE = Path(__file__).with_name("visual.json")
GEMINI_MODELS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-flash-lite-latest"]
GROQ_VISION = ["meta-llama/llama-4-scout-17b-16e-instruct", "meta-llama/llama-4-maverick-17b-128e-instruct"]
BATCH = 6
MAX_IMAGES_PER_RUN = 36
KEEP_DAYS = 14
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

PROMPT = """Sos directora de arte de una agencia creativa argentina. Para cada imagen (numeradas en orden) describí la tendencia visual.
Devolvé un objeto JSON {"items": [...]} con un elemento por imagen, en el mismo orden, cada uno con:
- "n": número de imagen
- "estetica": nombre corto de la estética en español (ej: "Y2K", "minimalismo cálido", "cottagecore", "meme de reacción", "collage editorial")
- "formato": qué tipo de pieza es (ej: "captura de pantalla con texto", "foto de producto", "ilustración", "video de reacción", "tutorial paso a paso")
- "colores": hasta 3 colores dominantes en español
- "descripcion": una oración sobre qué muestra y cómo podría adaptarla una marca
No inventes: si no se ve bien, decilo."""


def _image_urls(t):
    e = t.get("extra") or {}
    return [u for u in ([e.get("image")] + (e.get("images") or [])) if u and u.startswith("https://")]


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.pinterest.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        mime = r.headers.get_content_type() or "image/jpeg"
        return mime, r.read()


def _gemini(key, images):
    parts = [{"text": PROMPT}]
    for i, (mime, data) in enumerate(images, 1):
        parts += [{"text": f"Imagen {i}:"}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}]
    body = json.dumps({"contents": [{"role": "user", "parts": parts}],
                       "generationConfig": {"response_mime_type": "application/json", "temperature": 0.3}}).encode()
    for model in GEMINI_MODELS:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.loads(r.read())
            text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
            return json.loads(text).get("items", [])
        except Exception as e:
            print(f"  visual {model}: {type(e).__name__}")
    return None


def _groq(key, urls):
    content = [{"type": "text", "text": PROMPT}]
    for i, u in enumerate(urls, 1):
        content += [{"type": "text", "text": f"Imagen {i}:"}, {"type": "image_url", "image_url": {"url": u}}]
    for model in GROQ_VISION:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps({"model": model, "temperature": 0.3, "response_format": {"type": "json_object"},
                             "messages": [{"role": "user", "content": content}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "radar-sarogiri"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"]
            return json.loads(text).get("items", [])
        except Exception as e:
            print(f"  visual groq {model}: {type(e).__name__}")
    return None


def describe_visuals(trends, generate=False):
    """Attaches t["visual"] to trends with images; with generate=True analyzes new images."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    now = time.time()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip() if generate else ""
    groq_key = os.environ.get("GROQ_API_KEY", "").strip() if generate else ""
    made = 0
    if gemini_key or groq_key:
        todo = [(t, _image_urls(t)[0]) for t in trends if _image_urls(t) and _image_urls(t)[0] not in cache][:MAX_IMAGES_PER_RUN]
        for i in range(0, len(todo), BATCH):
            batch = todo[i : i + BATCH]
            images, urls = [], []
            for _, url in batch:
                try:
                    images.append(_download(url))
                    urls.append(url)
                except Exception:
                    cache[url] = {"error": True, "ts": now}
            if not urls:
                continue
            items = (_gemini(gemini_key, images) if gemini_key else None) or (_groq(groq_key, urls) if groq_key else None)
            if not items:
                break
            for it in items:
                n = it.get("n") if isinstance(it, dict) else None
                if isinstance(n, int) and 1 <= n <= len(urls):
                    cache[urls[n - 1]] = {k: it.get(k) for k in ("estetica", "formato", "colores", "descripcion")} | {"ts": now}
                    made += 1
        cache = {k: v for k, v in cache.items() if now - v.get("ts", now) < KEEP_DAYS * 86400}
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    for t in trends:
        urls = _image_urls(t)
        info = cache.get(urls[0]) if urls else None
        if info and not info.get("error"):
            t["visual"] = {k: info.get(k) for k in ("estetica", "formato", "colores", "descripcion")}
    return made
