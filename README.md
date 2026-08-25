# Omni-Platform Downloader

A 100% free, always-active media downloader. Paste a URL from any of yt-dlp's 1,700+ supported sites, get a streamable download link. Cached files are served instantly from Cloudflare R2; new fetches run on GitHub Actions.

## Architecture

```
Browser (Pages/Worker static) → Worker (API routing) → KV (index) + R2 (cache)
                                      ↓ (cache miss)
                               GitHub Actions (yt-dlp) → R2 + KV
                                      ↓ (dispatch fails, optional)
                               Render fallback (yt-dlp) → R2 + KV
```

## Components

| Layer | Tech | Free Tier |
|-------|------|-----------|
| Frontend | Static HTML/CSS/JS served by Worker | Workers free |
| API routing | Cloudflare Worker | 100K req/day |
| Index | Workers KV | 100K reads/day, 1K writes/day |
| Cache | R2 bucket `dl-cache` | 10 GB storage, zero egress |
| Fetch engine | GitHub Actions (public repo) | Unlimited minutes, 20 concurrent |
| Fallback | Render free web service | 512 MB RAM, sleeps after 15 min |

## Setup

### 1. Prerequisites

- Cloudflare account (free)
- GitHub account with a **public** repo (for unlimited Actions minutes)
- Render account (optional, for fallback)

### 2. Create Cloudflare resources

```bash
npm install
npx wrangler login

# Create R2 bucket
npx wrangler r2 bucket create dl-cache

# Create KV namespace — copy the ID from the output
npx wrangler kv namespace create INDEX
```

Take the KV namespace ID from the output and paste it into `wrangler.toml`:
```toml
[[kv_namespaces]]
binding = "INDEX"
id = "PASTE_THE_ID_HERE"
```

### 3. Create R2 API tokens (for GitHub Actions / Render to upload)

In the Cloudflare dashboard:
1. Go to **R2** → **Manage R2 API tokens** → **Create API token**
2. Permissions: **Object Read & Write** on bucket `dl-cache`
3. Copy the **Access Key ID**, **Secret Access Key**, and your **Account ID**

### 4. Create a Cloudflare API token (for KV writes from Actions/Render)

In the Cloudflare dashboard:
1. Go to **My Profile** → **API Tokens** → **Create Token**
2. Use template: **Edit Workers KV Storage**
3. Copy the token

### 5. Create a GitHub fine-grained PAT

1. GitHub → Settings → Developer settings → Fine-grained tokens
2. Repository access: your public repo
3. Permissions: **Actions: Read and Write**
4. Copy the token

### 6. Set GitHub Actions secrets

In your GitHub repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|--------|-------|
| `R2_ACCOUNT_ID` | Your Cloudflare account ID |
| `R2_ACCESS_KEY` | R2 API token access key |
| `R2_SECRET_KEY` | R2 API token secret key |
| `CF_KV_NAMESPACE_ID` | KV namespace ID from step 2 |
| `CF_API_TOKEN` | Cloudflare API token from step 4 |

### 7. Set Worker secrets

```bash
npx wrangler secret put GITHUB_TOKEN    # paste the GitHub PAT from step 5
npx wrangler secret put GITHUB_REPO     # e.g. yourusername/omni-downloader
npx wrangler secret put RENDER_URL      # optional: your Render service URL
```

### 8. Deploy the Worker

```bash
npx wrangler deploy
```

Your app is live at `https://omni-downloader.<your-subdomain>.workers.dev`.

### 9. (Optional) Deploy Render fallback

```bash
cd render
# Connect this directory to Render as a new web service
# Set the same env vars as the GitHub secrets (minus GITHUB_TOKEN/GITHUB_REPO)
# Set RENDER_URL in your Worker to the Render service URL
```

## How it works

1. User submits a URL + quality on the frontend.
2. Worker computes `key_base = sha256(url|quality)`.
3. Worker checks KV for `media:<key_base>`:
   - **Hit** → returns `/api/stream/<key_base>` instantly (served from R2).
   - **Miss** → checks for `pending:<key_base>`:
     - **Pending** → returns 202, client polls `/api/meta`.
     - **New** → writes `pending:<key_base>`, dispatches `repository_dispatch` to GitHub Actions, returns 202.
4. GitHub Actions runs yt-dlp, uploads to R2 as `<key_base>.<ext>`, writes `media:<key_base>` to KV, deletes `pending:`.
5. Client polls `/api/meta?key=<key_base>` every 3 seconds until `status: "ready"`.
6. Client opens `/api/stream/<key_base>` — Worker streams from R2 with Range support.

## Known limitations

- **YouTube datacenter-IP blocking**: GitHub Actions runners use datacenter IPs that YouTube actively blocks. YouTube downloads may fail with "Sign in to confirm you're not a bot." Other platforms (TikTok, Reddit, Vimeo, SoundCloud, etc.) are generally unaffected. See `AGENT_BUILD_PLAN.md` for mitigation options.
- **GitHub Actions concurrency**: 20 concurrent jobs on the Free plan. Burst traffic beyond 20 simultaneous new fetches will queue.
- **Render free tier sleeps** after 15 min idle — first request after sleep takes ~30s to wake.
- **R2 10 GB limit**: This plan assumes cache eviction keeps the bucket under 9.5 GB. A lifecycle/cleanup mechanism should be added for production use.

## File structure

```
├── src/index.ts              # Worker — API routing + R2 streaming
├── public/index.html         # Frontend — static download page
├── .github/workflows/
│   └── fetch-media.yml       # GitHub Actions — yt-dlp fetch job
├── scripts/kv_put.py         # Python helper — write to KV
├── render/
│   ├── app.py                # Render fallback — Flask yt-dlp service
│   ├── requirements.txt
│   └── render.yaml           # Render deployment config
├── wrangler.toml             # Worker config + bindings
├── package.json
└── tsconfig.json
```

## License

MIT
