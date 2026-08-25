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
  ALLOWED_ORIGIN?: string;
}

// ── Constants ────────────────────────────────────────────────

const VALID_QUALITIES = ['best', '1080p', '720p', '480p', '360p', 'audio'] as const;
const MAX_FILE_SIZE = 500 * 1024 * 1024; // 500 MB — oversized guard
const PENDING_TTL = 600; // 10 min — pending entries auto-expire
const FAILED_TTL = 3600; // 1 hour — failed entries auto-expire

function getCorsHeaders(request?: Request, env?: Env): Record<string, string> {
  const origin = request?.headers.get('Origin') || '';
  const allowed = [
    'https://omni-downloader.iorchichristiana.workers.dev',
    'http://localhost:8787',
    'http://127.0.0.1:8787',
  ];
  if (env?.ALLOWED_ORIGIN) allowed.push(env.ALLOWED_ORIGIN);

  const matchedOrigin = allowed.includes(origin) ? origin : 'https://omni-downloader.iorchichristiana.workers.dev';

  return {
    'Access-Control-Allow-Origin': matchedOrigin,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}

// ── Helpers ──────────────────────────────────────────────────

async function sha256(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

function isPrivateOrLocalhost(urlStr: string): boolean {
  try {
    const parsed = new URL(urlStr);
    const hostname = parsed.hostname.toLowerCase();

    // Loopback / localhost names
    if (
      hostname === 'localhost' ||
      hostname.endsWith('.localhost') ||
      hostname.endsWith('.local') ||
      hostname.endsWith('.internal') ||
      hostname === '127.0.0.1' ||
      hostname === '0.0.0.0' ||
      hostname === '::1'
    ) {
      return true;
    }

    // IPv4 private ranges
    const ipv4Match = hostname.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
    if (ipv4Match) {
      const [_, a, b] = ipv4Match.map(Number);
      if (a === 10) return true; // 10.0.0.0/8
      if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12
      if (a === 192 && b === 168) return true; // 192.168.0.0/16
      if (a === 169 && b === 254) return true; // 169.254.0.0/16 (link-local)
      if (a === 127) return true; // 127.0.0.0/8
      if (a === 0) return true; // 0.0.0.0/8
    }

    return false;
  } catch {
    return true;
  }
}

function json(body: unknown, init: ResponseInit = {}, request?: Request, env?: Env): Response {
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  for (const [k, v] of Object.entries(getCorsHeaders(request, env))) headers.set(k, v);
  return new Response(JSON.stringify(body), { ...init, headers });
}

function noStore(body: unknown, status = 200, request?: Request, env?: Env): Response {
  return json(body, { status, headers: { 'Cache-Control': 'no-store' } }, request, env);
}

// ── Route handlers ───────────────────────────────────────────

/** POST /api/download — submit URL + quality */
async function handleDownload(request: Request, env: Env): Promise<Response> {
  let payload: { url?: string; quality?: string; cookies?: string };
  try {
    payload = await request.json();
  } catch {
    return json({ error: 'Invalid JSON body' }, { status: 400 }, request, env);
  }

  const mediaUrl = payload.url?.trim();
  const quality = payload.quality?.trim();
  const cookies = typeof payload.cookies === 'string' && payload.cookies.length < 65536 ? payload.cookies : undefined;

  if (!mediaUrl || !quality) {
    return json({ error: 'Missing "url" or "quality"' }, { status: 400 }, request, env);
  }

  // Validate URL protocol
  let parsed: URL;
  try {
    parsed = new URL(mediaUrl);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return json({ error: 'Invalid URL protocol (must be http or https)' }, { status: 400 }, request, env);
    }
  } catch {
    return json({ error: 'Invalid URL' }, { status: 400 }, request, env);
  }

  // SSRF guard
  if (isPrivateOrLocalhost(mediaUrl)) {
    return json({ error: 'Private or local IP addresses are not permitted' }, { status: 400 }, request, env);
  }

  if (!VALID_QUALITIES.includes(quality as (typeof VALID_QUALITIES)[number])) {
    return json({ error: `Invalid quality. Must be one of: ${VALID_QUALITIES.join(', ')}` }, { status: 400 }, request, env);
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
    }, {}, request, env);
  }

  // 2. Already pending? → return 202 so client polls
  const pendingRaw = await env.INDEX.get(`pending:${keyBase}`);
  if (pendingRaw) {
    return json({ status: 'pending', key_base: keyBase }, { status: 202 }, request, env);
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
  const dispatched = await dispatchGitHub(env, mediaUrl, quality, keyBase, cookies);

  // 6. Optional Render fallback if GitHub dispatch fails
  if (!dispatched && env.RENDER_URL) {
    const renderOk = await dispatchRender(env, mediaUrl, quality, keyBase);
    if (!renderOk) {
      await env.INDEX.delete(`pending:${keyBase}`);
      return json({ error: 'Failed to dispatch fetch job' }, { status: 502 }, request, env);
    }
  } else if (!dispatched) {
    await env.INDEX.delete(`pending:${keyBase}`);
    return json({ error: 'Failed to dispatch fetch job' }, { status: 502 }, request, env);
  }

  return json({ status: 'pending', key_base: keyBase }, { status: 202 }, request, env);
}

async function dispatchGitHub(env: Env, url: string, quality: string, keyBase: string, cookies?: string): Promise<boolean> {
  try {
    const resp = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'Omni-Downloader-Worker',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        event_type: 'fetch-media',
        client_payload: { url, quality, key_base: keyBase, cookies },
      }),
    });
    if (!resp.ok) {
      console.error(`GitHub dispatch failed: ${resp.status} ${await resp.text()}`);
    }
    return resp.ok;
  } catch (err) {
    console.error('GitHub dispatch error:', err);
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
  if (!keyBase) return noStore({ error: 'Missing "key" parameter' }, 400, request, env);

  // 1. Check cached
  const cachedRaw = await env.INDEX.get(`media:${keyBase}`);
  if (cachedRaw) {
    const meta = JSON.parse(cachedRaw);
    return noStore({ status: 'ready', key_base: keyBase, stream_url: `/api/stream/${keyBase}`, ...meta }, 200, request, env);
  }

  // 2. Check failed BEFORE pending — so failed jobs report immediately
  const failedRaw = await env.INDEX.get(`failed:${keyBase}`);
  if (failedRaw) {
    const meta = JSON.parse(failedRaw);
    return noStore({ status: 'failed', key_base: keyBase, ...meta }, 200, request, env);
  }

  // 3. Check pending
  const pendingRaw = await env.INDEX.get(`pending:${keyBase}`);
  if (pendingRaw) return noStore({ status: 'pending', key_base: keyBase }, 200, request, env);

  return noStore({ status: 'unknown' }, 404, request, env);
}

/** GET /api/stream/:key_base — stream from R2 with Range support */
async function handleStream(request: Request, env: Env, keyBase: string): Promise<Response> {
  const cachedRaw = await env.INDEX.get(`media:${keyBase}`);
  if (!cachedRaw) return json({ error: 'Not found in index' }, { status: 404 }, request, env);

  const meta = JSON.parse(cachedRaw) as { ext: string; mime: string; size: number; title?: string };
  const range = request.headers.get('Range');
  const object = await env.CACHE.get(`${keyBase}.${meta.ext}`, {
    range: request.headers,
    onlyIf: request.headers,
  });
  if (!object || !('body' in object)) return json({ error: 'Object not in R2' }, { status: 404 }, request, env);

  // Oversized guard
  if (object.size > MAX_FILE_SIZE) {
    return json({ error: 'File exceeds size limit' }, { status: 413 }, request, env);
  }

  const rawTitle = meta.title || keyBase;
  const safeAscii = rawTitle.replace(/[^\w\s.-]/g, '').trim() || keyBase;
  const filename = `${safeAscii}.${meta.ext}`;
  const utf8Filename = `${rawTitle}.${meta.ext}`;

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set('Content-Type', meta.mime || headers.get('Content-Type') || 'application/octet-stream');
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Cache-Control', 'public, max-age=86400');
  headers.set(
    'Content-Disposition',
    `inline; filename="${filename}"; filename*=UTF-8''${encodeURIComponent(utf8Filename)}`,
  );
  for (const [k, v] of Object.entries(getCorsHeaders(request, env))) headers.set(k, v);

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
      return new Response(null, { status: 204, headers: getCorsHeaders(request, env) });
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

      // Fallback for unmatched API routes
      return json({ error: 'Not found' }, { status: 404 }, request, env);
    } catch (err) {
      console.error('Unhandled error:', err);
      return json({ error: 'Internal server error' }, { status: 500 }, request, env);
    }
  },
};
