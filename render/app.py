#!/usr/bin/env python3
"""
Render fallback service — a minimal Flask app that receives a fetch request
from the Worker when GitHub Actions dispatch fails, downloads media with
yt-dlp, uploads to R2, and updates the KV index.

Deploy to Render as a free web service (512 MB RAM, sleeps after 15 min idle).

Environment variables (set in Render dashboard):
    R2_ACCOUNT_ID
    R2_ACCESS_KEY
    R2_SECRET_KEY
    CF_KV_NAMESPACE_ID
    CF_API_TOKEN
    PORT (set automatically by Render)
"""

import os
import json
import subprocess
import tempfile
import urllib.parse
import urllib.request
import glob
from flask import Flask, request, jsonify

app = Flask(__name__)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

QUALITY_MAP = {
    "best": "bestvideo*+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "audio": "bestaudio/best",
}

MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB


def configure_rclone():
    conf_dir = os.path.expanduser("~/.config/rclone")
    os.makedirs(conf_dir, exist_ok=True)
    conf_path = os.path.join(conf_dir, "rclone.conf")
    with open(conf_path, "w") as f:
        f.write(f"""[r2]
type = s3
provider = Cloudflare
access_key_id = {R2_ACCESS_KEY}
secret_access_key = {R2_SECRET_KEY}
endpoint = https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com
""")


def kv_put(key, value, ttl=None):
    encoded_key = urllib.parse.quote(key, safe="")
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{R2_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{encoded_key}"
    )
    if ttl:
        url += f"?expirationTtl={ttl}"
    req = urllib.request.Request(
        url,
        data=value.encode("utf-8") if isinstance(value, str) else json.dumps(value).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def kv_delete(key):
    encoded_key = urllib.parse.quote(key, safe="")
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{R2_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{encoded_key}"
    )
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


@app.route("/fetch", methods=["POST"])
def fetch():
    data = request.get_json(force=True)
    media_url = data.get("url")
    quality = data.get("quality")
    key_base = data.get("key_base")

    if not all([media_url, quality, key_base]):
        return jsonify({"error": "Missing url, quality, or key_base"}), 400

    if quality not in QUALITY_MAP:
        return jsonify({"error": f"Invalid quality: {quality}"}), 400

    fmt = QUALITY_MAP[quality]

    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "output")

        try:
            if quality == "audio":
                subprocess.run(
                    [
                        "yt-dlp", "-f", fmt,
                        "--extract-audio", "--audio-format", "mp3",
                        "--audio-quality", "0",
                        "-o", f"{outfile}.%(ext)s",
                        "--no-playlist",
                        media_url,
                    ],
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
                ext = "mp3"
                mime = "audio/mpeg"
            else:
                subprocess.run(
                    [
                        "yt-dlp", "-f", fmt,
                        "--merge-output-format", "mp4",
                        "--embed-subs", "--embed-metadata",
                        "-o", f"{outfile}.%(ext)s",
                        "--no-playlist",
                        media_url,
                    ],
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
                ext = "mp4"
                mime = "video/mp4"

            # Find the actual output file
            files = glob.glob(f"{outfile}.*")
            if not files:
                raise RuntimeError("No output file produced")
            filepath = files[0]

            # Oversized guard
            filesize = os.path.getsize(filepath)
            if filesize > MAX_FILE_SIZE:
                raise RuntimeError(f"File too large: {filesize} bytes")

            # Upload to R2 via rclone
            configure_rclone()
            subprocess.run(
                ["rclone", "copyto", filepath, f"r2:dl-cache/{key_base}.{ext}"],
                check=True,
                capture_output=True,
                timeout=120,
            )

            # Update KV index
            kv_put(
                f"media:{key_base}",
                json.dumps({"ext": ext, "mime": mime, "size": filesize, "cached_at": int(__import__("time").time())}),
            )

            # Clean up pending entry
            kv_delete(f"pending:{key_base}")

            return jsonify({"status": "ready", "key_base": key_base})

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode("utf-8", errors="replace") if e.stderr else str(e)
            # Write failed entry
            kv_put(
                f"failed:{key_base}",
                json.dumps({"error": "yt-dlp fetch failed", "detail": error_msg[:500]}),
                ttl=3600,
            )
            kv_delete(f"pending:{key_base}")
            return jsonify({"status": "failed", "error": error_msg[:500]}), 500

        except Exception as e:
            kv_put(
                f"failed:{key_base}",
                json.dumps({"error": str(e)}),
                ttl=3600,
            )
            kv_delete(f"pending:{key_base}")
            return jsonify({"status": "failed", "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
