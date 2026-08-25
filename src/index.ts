/**
 * Omni-Platform Downloader — Cloudflare Worker
 *
 * Endpoints:
 *   POST /api/download   — submit a URL+quality, returns cached link or dispatches a fetch job
 *   GET  /api/meta       — poll job status by key_base
 *   GET  /api/stream/:key — stream a cached file from R2 (supports Range)
 *   OPTIONS *             — CORS preflight
 *
 * Bindings:
 *   CACHE  — R2 bucket "dl-cache"
 *   INDEX  — KV namespace "dl-index"
 *   GITHUB_TOKEN — fine-grained PAT with repo access (secret)
 *   GITHUB_REPO  — "owner/repo" of the public GitHub repo (secret or var)
 *   RENDER_URL   — optional Render fallback URL (secret, leave unset to skip)
 */

export interface Env {
  CACHE: R2Bucket;
  INDEX: KVNamespace;
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
  RENDER_URL?: string;
}

// ── Constants ────────────────────────────────────────────────

const CORS_HEADERS: Record<string, string> = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Access-Control-Max-Age': '86400',
};

const VALID_QUALITIES = ['1080p', '720p', '480p', '360p', 'audio'] as const;
const MAX_FILE_SIZE = 500 * 1024 * 1024; // 500 MB — oversized guard
const PENDING_TTL = 600; // 10 min — pending entries auto-expire
const FAILED_TTL = 3600; // 1 hour — failed entries auto-expire

// ── Helpers ──────────────────────────────────────────────────

async function sha256(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

function json(body: unknown, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  for (const [k, v] of Object.entries(CORS_HEADERS)) headers.set(k, v);
  return new Response(JSON.stringify(body), { ...init, headers });
}

function noStore(body: unknown, status = 200): Response {
  return json(body, { status, headers: { 'Cache-Control': 'no-store' } });
}

// ── Route handlers ───────────────────────────────────────────

/** POST /api/download — submit URL + quality */
async function handleDownload(request: Request, env: Env): Promise<Response> {
  let payload: { url?: string; quality?: string };
  try {
    payload = await request.json();
  } catch {
    return json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  const mediaUrl = payload.url?.trim();
  const quality = payload.quality?.trim();

  if (!mediaUrl || !quality) {
    return json({ error: 'Missing "url" or "quality"' }, { status: 400 });
  }

  // Validate URL
  let parsed: URL;
  try {
    parsed = new URL(mediaUrl);
    if (!parsed.protocol.startsWith('http')) throw new Error();
  } catch {
    return json({ error: 'Invalid URL' }, { status: 400 });
  }

  if (!VALID_QUALITIES.includes(quality as (typeof VALID_QUALITIES)[number])) {
    return json({ error: `Invalid quality. Must be one of: ${VALID_QUALITIES.join(', ')}` }, { status: 400 });
  }

  const keyBase = await sha256(`${mediaUrl}|${quality}`);

  // 1. Already cached? → return stream link immediately
  const cachedRaw = await env.INDEX.get(`media:${keyBase}`);
  if (cachedRaw) {
    const meta = JSON.parse(cachedRaw);
    return json({
      status: 'ready',
      key_base: keyBase,
      stream_url: `/api/stream/${keyBase}`,
      ...meta,
    });
  }

  // 2. Already pending? → return 202 so client polls
  const pendingRaw = await env.INDEX.get(`pending:${keyBase}`);
  if (pendingRaw) {
    return json({ status: 'pending', key_base: keyBase }, { status: 202 });
  }

  // 3. Previously failed? → clear and re-dispatch
  const failedRaw = await env.INDEX.get(`failed:${keyBase}`);
  if (failedRaw) {
    await env.INDEX.delete(`failed:${keyBase}`);
  }

  // 4. Create pending entry (auto-expires in 10 min)
  await env.INDEX.put(
    `pending:${keyBase}`,
    JSON.stringify({ key_base: keyBase, url: mediaUrl, quality, dispatched_at: Date.now() }),
    { expirationTtl: PENDING_TTL },
  );

  // 5. Dispatch GitHub Actions repository_dispatch
  const dispatched = await dispatchGitHub(env, mediaUrl, quality, keyBase);

  // 6. Optional Render fallback if GitHub dispatch fails
  if (!dispatched && env.RENDER_URL) {
    const renderOk = await dispatchRender(env, mediaUrl, quality, keyBase);
    if (!renderOk) {
      await env.INDEX.delete(`pending:${keyBase}`);
      return json({ error: 'Failed to dispatch fetch job' }, { status: 502 });
    }
  } else if (!dispatched) {
    await env.INDEX.delete(`pending:${keyBase}`);
    return json({ error: 'Failed to dispatch fetch job' }, { status: 502 });
  }

  return json({ status: 'pending', key_base: keyBase }, { status: 202 });
}

async function dispatchGitHub(env: Env, url: string, quality: string, keyBase: string): Promise<boolean> {
  try {
    const resp = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        event_type: 'fetch-media',
        client_payload: { url, quality, key_base: keyBase },
      }),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

async function dispatchRender(env: Env, url: string, quality: string, keyBase: string): Promise<boolean> {
  try {
    const resp = await fetch(`${env.RENDER_URL}/fetch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, quality, key_base: keyBase }),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

/** GET /api/meta?key=<key_base> — poll status */
async function handleMeta(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const keyBase = url.searchParams.get('key');
  if (!keyBase) return noStore({ error: 'Missing "key" parameter' }, 400);

  // Check cached
  const cachedRaw = await env.INDEX.get(`media:${keyBase}`);
  if (cachedRaw) {
    const meta = JSON.parse(cachedRaw);
    return noStore({ status: 'ready', key_base: keyBase, stream_url: `/api/stream/${keyBase}`, ...meta });
  }

  // Check pending
  const pendingRaw = await env.INDEX.get(`pending:${keyBase}`);
  if (pendingRaw) return noStore({ status: 'pending', key_base: keyBase });

  // Check failed
  const failedRaw = await env.INDEX.get(`failed:${keyBase}`);
  if (failedRaw) {
    const meta = JSON.parse(failedRaw);
    return noStore({ status: 'failed', ...meta });
  }

  return noStore({ status: 'unknown' }, 404);
}

/** GET /api/stream/:key_base — stream from R2 with Range support */
async function handleStream(request: Request, env: Env, keyBase: string): Promise<Response> {
  const cachedRaw = await env.INDEX.get(`media:${keyBase}`);
  if (!cachedRaw) return json({ error: 'Not found in index' }, { status: 404 });

  const meta = JSON.parse(cachedRaw) as { ext: string; mime: string; size: number };
  const range = request.headers.get('Range');
  const object = await env.CACHE.get(`${keyBase}.${meta.ext}`, {
    range: request.headers,
    onlyIf: request.headers,
  });
  if (!object || !('body' in object)) return json({ error: 'Object not in R2' }, { status: 404 });

  // Oversized guard
  if (object.size > MAX_FILE_SIZE) {
    return json({ error: 'File exceeds size limit' }, { status: 413 });
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set('Content-Type', meta.mime || headers.get('Content-Type') || 'application/octet-stream');
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Cache-Control', 'public, max-age=86400');
  for (const [k, v] of Object.entries(CORS_HEADERS)) headers.set(k, v);

  if (range && 'range' in object && object.range) {
    const r = object.range as { offset?: number; length?: number };
    const offset = r.offset ?? 0;
    const length = r.length ?? object.size;
    headers.set('Content-Range', `bytes ${offset}-${offset + length - 1}/${object.size}`);
    headers.set('Content-Length', length.toString());
    return new Response(object.body, { status: 206, headers });
  }

  headers.set('Content-Length', object.size.toString());
  return new Response(object.body, { status: 200, headers });
}

// ── Entrypoint ───────────────────────────────────────────────

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const method = request.method;

    // CORS preflight
    if (method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    try {
      // POST /api/download
      if (method === 'POST' && url.pathname === '/api/download') {
        return await handleDownload(request, env);
      }

      // GET /api/meta
      if (method === 'GET' && url.pathname === '/api/meta') {
        return await handleMeta(request, env);
      }

      // GET /api/stream/:key_base
      if (method === 'GET' && url.pathname.startsWith('/api/stream/')) {
        const keyBase = url.pathname.slice('/api/stream/'.length);
        return await handleStream(request, env, keyBase);
      }

      // Let static assets handle everything else (frontend)
      // If no assets binding, return 404
      return json({ error: 'Not found' }, { status: 404 });
    } catch (err) {
      console.error('Unhandled error:', err);
      return json({ error: 'Internal server error' }, { status: 500 });
    }
  },
};
