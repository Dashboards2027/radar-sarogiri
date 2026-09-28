// Relays the dashboard's AI requests to Gemini so the API key never reaches the browser.
// The key lives only in the Vercel environment variable GEMINI_API_KEY.

const ALLOWED_ORIGINS = [
  "https://dashboards2027.github.io",
  "http://127.0.0.1:8787",
  "http://localhost:8787",
];
const MODELS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-flash-lite-latest"];
const MAX_BODY_BYTES = 4 * 1024 * 1024;
const PER_IP_PER_HOUR = 60;

// Best-effort limiter: resets whenever Vercel starts a new instance, which is enough to stop casual abuse.
const hits = new Map();
function limited(ip) {
  const now = Date.now();
  const recent = (hits.get(ip) || []).filter((t) => now - t < 3600_000);
  recent.push(now);
  hits.set(ip, recent);
  return recent.length > PER_IP_PER_HOUR;
}

function allowedBody(body) {
  // Only plain generateContent payloads: no arbitrary URLs or endpoints can be proxied.
  if (!body || !Array.isArray(body.contents)) return null;
  const clean = { contents: body.contents };
  if (body.system_instruction) clean.system_instruction = body.system_instruction;
  if (body.generationConfig) clean.generationConfig = body.generationConfig;
  if (Array.isArray(body.tools) && body.tools.every((t) => t && Object.keys(t).length === 1 && "google_search" in t)) {
    clean.tools = body.tools;
  }
  return clean;
}

export default async function handler(req, res) {
  const origin = req.headers.origin || "";
  if (ALLOWED_ORIGINS.includes(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
    res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  }
  if (req.method === "OPTIONS") return res.status(204).end();
  if (!ALLOWED_ORIGINS.includes(origin)) return res.status(403).json({ error: { message: "Origen no permitido" } });
  if (req.method !== "POST") return res.status(405).json({ error: { message: "Usá POST" } });

  const key = process.env.GEMINI_API_KEY;
  if (!key) return res.status(500).json({ error: { message: "Falta configurar GEMINI_API_KEY en Vercel" } });

  const ip = (req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "anon";
  if (limited(ip)) return res.status(429).json({ error: { message: "Demasiados pedidos seguidos. Probá en un rato." } });

  if (Number(req.headers["content-length"] || 0) > MAX_BODY_BYTES) {
    return res.status(413).json({ error: { message: "El pedido es muy grande (máx. 4 MB)." } });
  }
  const body = allowedBody(typeof req.body === "string" ? JSON.parse(req.body) : req.body);
  if (!body) return res.status(400).json({ error: { message: "Pedido inválido" } });

  let last = { status: 502, data: { error: { message: "Gemini no respondió" } } };
  for (const model of MODELS) {
    const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": key },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    last = { status: r.status, data };
    if (![404, 429, 500, 503].includes(r.status)) break;
  }
  return res.status(last.status).json(last.data);
}
