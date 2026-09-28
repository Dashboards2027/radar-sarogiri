// Relays the dashboard's AI requests to Gemini so the API key never reaches the browser.
// The key lives only in the Vercel environment variable GEMINI_API_KEY.

const ALLOWED_ORIGINS = [
  "https://dashboards2027.github.io",
  "http://127.0.0.1:8787",
  "http://localhost:8787",
];
const MODELS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-flash-lite-latest"];
// Backup when Gemini is overloaded: Groq's free tier (needs GROQ_API_KEY in Vercel).
const GROQ_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant"];
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

function toGroqMessages(body) {
  const text = (parts) => (parts || []).map((p) => p.text || "").join("\n").trim();
  const messages = [];
  const system = text(body.system_instruction?.parts);
  if (system) messages.push({ role: "system", content: system });
  for (const c of body.contents) {
    const t = text(c.parts);
    if (t) messages.push({ role: c.role === "model" ? "assistant" : "user", content: t });
  }
  return messages;
}

async function askGroq(body) {
  const key = process.env.GROQ_API_KEY || process.env.GROQ_API || process.env.Groq;
  if (!key) return null;
  const wantsJson = body.generationConfig?.response_mime_type === "application/json";
  for (const model of GROQ_MODELS) {
    const r = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({ model, messages: toGroqMessages(body), temperature: 0.5, ...(wantsJson ? { response_format: { type: "json_object" } } : {}) }),
    });
    if (!r.ok) continue;
    const data = await r.json().catch(() => null);
    const out = data?.choices?.[0]?.message?.content;
    // Same shape the dashboard expects from Gemini.
    if (out) return { candidates: [{ content: { role: "model", parts: [{ text: out }] } }], servedBy: `groq:${model}` };
  }
  return null;
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
  if ([404, 429, 500, 503].includes(last.status)) {
    const backup = await askGroq(body).catch(() => null);
    if (backup) return res.status(200).json(backup);
  }
  return res.status(last.status).json(last.data);
}
