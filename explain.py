"""Adds an AI title, explanation, type and category to the top trends using Gemini's free tier.

Runs only when GEMINI_API_KEY is set (a GitHub secret). Results are cached in explanations.json
by trend key, so each trend is explained once and later runs only pay for new trends.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from analyze import norm

CACHE = Path(__file__).with_name("explanations.json")
MODELS = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]
TYPES = ["Formato", "Audio", "Real time", "Cultura pop", "Humor", "Estilo de vida", "Consumo", "Noticia"]
CATEGORIES = ["Belleza y Moda", "Bienestar y Fitness", "Comida y Bebida", "Consumo y Productos", "Cultura y Memes",
              "Deportes", "IA y Tech", "Música y Entretenimiento", "Noticias y Política", "Otros"]
BATCH = 20
MAX_NEW_PER_RUN = 60
KEEP_DAYS = 10


def key_for(t):
    return norm(t["keyword"])[:80]


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _call(api_key, prompt):
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.4},
    }).encode()
    last = None
    for model in MODELS:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return json.loads(text)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 429, 500, 503):
                continue
            raise
    raise last


def _describe(t):
    members = "; ".join(f"{m['source']} {m['region']} #{m['rank']} \"{m['keyword']}\"" for m in t["members"][:6])
    extra = t.get("extra") or {}
    ctx = extra.get("news") or extra.get("artist") or ""
    return f'- id: {key_for(t)} | tendencia: "{t["keyword"]}" | fuentes: {members}' + (f" | contexto: {ctx}" if ctx else "")


def _prompt(batch):
    items = "\n".join(_describe(t) for t in batch)
    return f"""Sos analista de tendencias de SAROGIRI, una agencia creativa de Buenos Aires que hace contenido, comunidad y experiencias para marcas.
Para cada tendencia de la lista devolvé un objeto JSON con:
- "id": el mismo id que te paso
- "titulo": título claro en español, formato "Nombre — de qué se trata en pocas palabras" (máx. 80 caracteres). Si es un meme o formato, describí qué hace la gente.
- "explicacion": 2 oraciones en español rioplatense: qué es y por qué está en tendencia ahora; y cómo podría usarla una marca (o "No conviene para marcas" si es tragedia, política o polémica).
- "tipo": uno de {TYPES}
- "categoria": uno de {CATEGORIES}
- "hashtags": hasta 3 hashtags relevantes
Si no sabés qué es, deducilo de las fuentes y el contexto, y decí que es una suposición. No inventes datos.
Devolvé solo un array JSON.

Tendencias:
{items}"""


def enrich(trends, generate=False):
    """Mutates trends in place with cached explanations; with generate=True also asks Gemini for new ones."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip() if generate else ""
    cache = load_cache()
    now = time.time()
    todo = [t for t in trends if key_for(t) not in cache][:MAX_NEW_PER_RUN] if api_key else []
    made = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        try:
            result = _call(api_key, _prompt(batch))
        except Exception as e:
            print(f"Gemini no respondió ({type(e).__name__}): {e}")
            break
        for r in result if isinstance(result, list) else []:
            if isinstance(r, dict) and r.get("id"):
                r["ts"] = now
                cache[r["id"]] = r
                made += 1
    cache = {k: v for k, v in cache.items() if now - v.get("ts", now) < KEEP_DAYS * 86400}
    if api_key:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    for t in trends:
        info = cache.get(key_for(t))
        if not info:
            continue
        t["title"] = info.get("titulo") or t["keyword"]
        t["explanation"] = info.get("explicacion", "")
        t["type"] = info.get("tipo") if info.get("tipo") in TYPES else None
        t["hashtags"] = [h if h.startswith("#") else "#" + h for h in info.get("hashtags", [])][:3]
        if info.get("categoria") in CATEGORIES and info["categoria"] != "Otros":
            t["category"] = info["categoria"]
    return made
