// worker.js — FormRescue receiving box (Cloudflare Worker + D1).
//
// Endpoints:
//   POST /submit  browser direct POST, gated by Turnstile siteverify + CORS
//   GET  /items   paginated pull (Bearer PULL_TOKEN), never deletes
//   POST /ack     delete rows by id after human confirmation (only delete path)
//
// Plain module fetch handler. No framework, no npm packages. Bindings:
//   DB (D1), ALLOWED_ORIGIN (plain_text), TURNSTILE_SECRET (secret),
//   PULL_TOKEN (secret). See the design doc for the full spec.

const SUBMIT_MAX_CHARS = 20000;
const NAME_MAX = 100;
const EMAIL_MAX = 254;
const BODY_MAX = 5000;
const RATE_WINDOW_MIN = 10;
const RATE_MAX = 3;
const ITEMS_MAX_LIMIT = 500;
const SITEVERIFY_URL =
  "https://challenges.cloudflare.com/turnstile/v0/siteverify";

const NAME_KEYS = ["your-name", "name", "お名前"];
const EMAIL_KEYS = ["your-email", "email", "メール", "メールアドレス"];
const BODY_KEYS = ["your-message", "message", "本文", "お問い合わせ内容"];

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    try {
      if (path === "/submit") return await handleSubmit(request, env);
      if (path === "/items") return await handleItems(request, env);
      if (path === "/ack") return await handleAck(request, env);
      return new Response("Not found", { status: 404 });
    } catch (err) {
      // 内部エラーの文言(D1/SQLのメッセージ等)は無認証の応答に出さない。
      // 詳細はログ(wrangler tail / ダッシュボード)で見る。
      console.error((err && err.stack) || err);
      return new Response("Internal error", { status: 500 });
    }
  },
};

// ── CORS ────────────────────────────────────────────────────────────────
function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function jsonResponse(obj, status, extraHeaders) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...(extraHeaders || {}) },
  });
}

// ── POST /submit ──────────────────────────────────────────────────────────
async function handleSubmit(request, env) {
  // fail-closed: ALLOWED_ORIGIN バインディングを忘れた手動デプロイで
  // Origin検査ごとスキップ+CORS全開("*")に落ちるのは危険なので、
  // 設定されるまで受け付けない(deploy.py 経由なら必ず設定される)。
  if (!env.ALLOWED_ORIGIN) {
    return new Response("ALLOWED_ORIGIN is not configured", { status: 500 });
  }
  const cors = corsHeaders(env);

  // 0. CORS preflight / method gate
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors });
  }
  if (request.method !== "POST") {
    return new Response("Method not allowed", { status: 405, headers: cors });
  }

  // 1. Origin check (weak defense-in-depth; the real gate is Turnstile)
  if (env.ALLOWED_ORIGIN) {
    if (request.headers.get("Origin") !== env.ALLOWED_ORIGIN) {
      return new Response("Forbidden", { status: 403, headers: cors });
    }
  }

  // 2. Size check — Content-Length で先に弾く(全読込前。UTF-8は最大4B/字)。
  //    宣言なし・偽装の場合に備え、読込後の実長検査も残す。
  const clen = parseInt(request.headers.get("Content-Length") || "0", 10);
  if (clen > SUBMIT_MAX_CHARS * 4) {
    return new Response("Payload too large", { status: 400, headers: cors });
  }
  const raw = await request.text();
  if (raw.length > SUBMIT_MAX_CHARS) {
    return new Response("Payload too large", { status: 400, headers: cors });
  }
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return new Response("Invalid JSON", { status: 400, headers: cors });
  }
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return new Response("Invalid JSON", { status: 400, headers: cors });
  }

  // 3. Turnstile siteverify (single-use token, ~5 min validity)
  const token =
    typeof data["cf-turnstile-response"] === "string"
      ? data["cf-turnstile-response"]
      : "";
  const visitorIp = request.headers.get("CF-Connecting-IP") || "";
  if (
    !(await verifyTurnstile(
      env.TURNSTILE_SECRET,
      token,
      visitorIp,
      originHostname(env.ALLOWED_ORIGIN)
    ))
  ) {
    return new Response("Turnstile verification failed", {
      status: 403,
      headers: cors,
    });
  }

  // 4. Extract (drop the consumed token before storing the payload)
  // name/email/body は一覧表示用の「見つかれば拾う」列。フィールド名は
  // [.form] のスキーマで著者が自由に決めるので、既知キーに一致しなくても
  // 実質的な入力が1つでもあれば受理する(以前は name/email/body が全部
  // 空だと 400 で、既知キーを使わない正規のフォームが全滅した)。
  delete data["cf-turnstile-response"];
  const name = pickField(data, NAME_KEYS, NAME_MAX);
  const email = pickField(data, EMAIL_KEYS, EMAIL_MAX) || firstEmail(data);
  const body = pickField(data, BODY_KEYS, BODY_MAX);
  const hasContent =
    name ||
    email ||
    body ||
    Object.values(data).some(
      (v) =>
        (typeof v === "string" && v.trim() !== "") ||
        typeof v === "number" ||
        typeof v === "boolean" ||
        (Array.isArray(v) && v.length > 0)
    );
  if (!hasContent) {
    return new Response("Empty submission", { status: 400, headers: cors });
  }
  const payload = JSON.stringify(data);

  // 5. Rate limit by real visitor IP (now that it's a direct browser POST)
  if (visitorIp) {
    const row = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM inbox " +
        "WHERE ip = ? AND created_at > datetime('now', ?)"
    )
      .bind(visitorIp, `-${RATE_WINDOW_MIN} minutes`)
      .first();
    if (row && row.n >= RATE_MAX) {
      return new Response("Too many requests", { status: 429, headers: cors });
    }
  }

  // 6. Insert (507 when D1 is at its size limit)
  try {
    await env.DB.prepare(
      "INSERT INTO inbox (name, email, body, payload, ip) VALUES (?, ?, ?, ?, ?)"
    )
      .bind(name, email, body, payload, visitorIp || null)
      .run();
  } catch (err) {
    if (/maximum DB size|SQLITE_FULL/i.test((err && err.message) || "")) {
      return new Response("Inbox full", { status: 507, headers: cors });
    }
    throw err;
  }
  return jsonResponse({ ok: true }, 200, cors);
}

function pickField(data, keys, max) {
  for (const k of keys) {
    const v = data[k];
    if (typeof v === "string" && v.trim() !== "") return v.slice(0, max);
  }
  return "";
}

const EMAIL_RE = /[^\s@]+@[^\s@]+/;
function firstEmail(data) {
  for (const v of Object.values(data)) {
    if (typeof v === "string") {
      const m = v.match(EMAIL_RE);
      if (m) return m[0].slice(0, EMAIL_MAX);
    }
  }
  return "";
}

function originHostname(origin) {
  try {
    return new URL(origin).hostname;
  } catch {
    return "";
  }
}

async function verifyTurnstile(secret, token, ip, expectedHostname) {
  if (!secret || !token) return false;
  const form = new URLSearchParams();
  form.append("secret", secret);
  form.append("response", token);
  if (ip) form.append("remoteip", ip);
  const res = await fetch(SITEVERIFY_URL, { method: "POST", body: form });
  if (!res.ok) return false;
  const out = await res.json();
  if (out.success !== true) return false;
  // sitekey は公開値なので、ウィジェット設定が緩いと他サイトで解かせた
  // トークンが通り得る。siteverify が返す hostname を突き合わせる(重ね掛け)。
  if (expectedHostname && out.hostname && out.hostname !== expectedHostname) {
    return false;
  }
  return true;
}

// ── GET /items ────────────────────────────────────────────────────────────
async function handleItems(request, env) {
  if (request.method !== "GET") {
    return new Response("Method not allowed", { status: 405 });
  }
  if (!checkBearer(request, env)) {
    return new Response("Unauthorized", { status: 401 });
  }
  const params = new URL(request.url).searchParams;
  const after = parseIntOr(params.get("after"), 0);
  let limit = parseIntOr(params.get("limit"), ITEMS_MAX_LIMIT);
  if (limit < 1) limit = 1;
  if (limit > ITEMS_MAX_LIMIT) limit = ITEMS_MAX_LIMIT;

  const { results } = await env.DB.prepare(
    "SELECT id, created_at, email, payload FROM inbox " +
      "WHERE id > ? ORDER BY id ASC LIMIT ?"
  )
    .bind(after, limit)
    .all();

  return jsonResponse({ items: results || [] }, 200);
}

// ── POST /ack ─────────────────────────────────────────────────────────────
async function handleAck(request, env) {
  if (request.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }
  if (!checkBearer(request, env)) {
    return new Response("Unauthorized", { status: 401 });
  }
  let data;
  try {
    data = JSON.parse(await request.text());
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }
  const ids = data && data.ids;
  if (
    !Array.isArray(ids) ||
    ids.length === 0 ||
    !ids.every((x) => Number.isInteger(x))
  ) {
    return new Response("Invalid ids", { status: 400 });
  }

  // Delete in chunks to stay under SQLite's bound-variable limit.
  let deleted = 0;
  const CHUNK = 100;
  for (let i = 0; i < ids.length; i += CHUNK) {
    const chunk = ids.slice(i, i + CHUNK);
    const placeholders = chunk.map(() => "?").join(",");
    const result = await env.DB.prepare(
      `DELETE FROM inbox WHERE id IN (${placeholders})`
    )
      .bind(...chunk)
      .run();
    deleted += (result.meta && result.meta.changes) || 0;
  }
  return jsonResponse({ deleted }, 200);
}

// ── helpers ───────────────────────────────────────────────────────────────
function checkBearer(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (!env.PULL_TOKEN) return false;
  return timingSafeEqual(auth, "Bearer " + env.PULL_TOKEN);
}

// 文字列比較(===)は不一致位置で早期リターンするため、比較時間から
// トークンを1文字ずつ当てられる余地がある。Workers ランタイムの
// crypto.subtle.timingSafeEqual(Cloudflare拡張)で定数時間比較にする。
// 長さの一致・不一致だけは漏れるが、トークン長は秘密ではない。
function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  if (ab.byteLength !== bb.byteLength) return false;
  return crypto.subtle.timingSafeEqual(ab, bb);
}

function parseIntOr(s, dflt) {
  if (s === null) return dflt;
  const n = parseInt(s, 10);
  return Number.isNaN(n) ? dflt : n;
}
