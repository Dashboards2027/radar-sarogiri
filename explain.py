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
MODELS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
# Lite models answer big classification batches much faster.
LITE_MODELS = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"] + MODELS
TYPES = ["Formato", "Audio", "Real time", "Cultura pop", "Humor", "Estilo de vida", "Consumo", "Noticia"]
CATEGORIES = ["Belleza y Moda", "Bienestar y Fitness", "Comida y Bebida", "Consumo y Productos", "Cultura y Memes",
              "Deportes", "IA y Tech", "Música y Entretenimiento", "Noticias y Política", "Otros"]
BATCH = 15
FULL_TOP = 150       # trends that get a full explanation
LITE_BATCH = 60      # the rest get a quick title/type/category in large batches
MAX_CALLS_PER_RUN = 18  # keeps each hourly run well inside Gemini's free quota
KEEP_DAYS = 10


def key_for(t):
    return norm(t["keyword"])[:80]


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _call(api_key, prompt, models=MODELS):
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.4},
    }).encode()
    last = None
    for attempt in range(2):
        result = _try_models(api_key, body, models)
        if not isinstance(result, Exception):
            return result
        last = result
        backup = _groq(prompt)
        if backup is not None:
            return backup
        time.sleep(30)  # Gemini's free tier is often briefly overloaded (503/429)
    raise last


GROQ_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant"]


def _groq(prompt):
    """Backup model when Gemini is overloaded; only used if the GROQ_API_KEY secret exists."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return None
    wrapped = prompt + '\n\nDevolvé un objeto JSON con la forma {"items": [ ... ]}.'
    for model in GROQ_MODELS:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps({"model": model, "temperature": 0.4, "response_format": {"type": "json_object"},
                             "messages": [{"role": "user", "content": wrapped}]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "radar-sarogiri"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                content = json.loads(r.read())["choices"][0]["message"]["content"]
            data = json.loads(content)
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                print(f"  respondió Groq ({model})")
                return items
        except Exception as e:
            print(f"  groq {model}: {type(e).__name__}")
    return None


def _try_models(api_key, body, models):
    last = None
    for model in models:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.loads(r.read())
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return json.loads(text)
        except urllib.error.HTTPError as e:
            last = e
            print(f"  {model}: HTTP {e.code}")
            if e.code in (404, 429, 500, 503):
                continue
            return e
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError) as e:
            last = e  # slow or malformed answer: try the next model
            continue
    return last


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


def _lite_prompt(batch):
    items = "\n".join(_describe(t) for t in batch)
    return f"""Clasificá estas tendencias para una agencia creativa de Buenos Aires. Para cada una devolvé un objeto JSON con:
"id" (el mismo), "titulo" (formato "Nombre — de qué se trata", máx. 70 caracteres, en español), "tipo" (uno de {TYPES}) y "categoria" (uno de {CATEGORIES}).
Si no sabés qué es, deducilo de las fuentes. Devolvé solo un array JSON.

Tendencias:
{items}"""


def _run(api_key, batches, prompt_fn, cache, now, lite, budget):
    made = 0
    for batch in batches:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            result = _call(api_key, prompt_fn(batch), LITE_MODELS if lite else MODELS)
        except Exception as e:
            print(f"Gemini no respondió ({type(e).__name__}): {e}")
            continue
        for r in result if isinstance(result, list) else []:
            if isinstance(r, dict) and r.get("id"):
                r["ts"] = now
                r["lite"] = lite
                cache[r["id"]] = r
                made += 1
    return made


def enrich(trends, generate=False):
    """Mutates trends in place with cached explanations; with generate=True also asks Gemini for new ones."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip() if generate else ""
    cache = load_cache()
    now = time.time()
    made = 0
    if api_key:
        budget = [MAX_CALLS_PER_RUN]
        top, rest = trends[:FULL_TOP], trends[FULL_TOP:]
        full = [t for t in top if key_for(t) not in cache or cache[key_for(t)].get("lite")]
        made += _run(api_key, [full[i : i + BATCH] for i in range(0, len(full), BATCH)], _prompt, cache, now, False, budget)
        lite = [t for t in rest if key_for(t) not in cache]
        made += _run(api_key, [lite[i : i + LITE_BATCH] for i in range(0, len(lite), LITE_BATCH)], _lite_prompt, cache, now, True, budget)
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
