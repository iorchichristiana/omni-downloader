#!/usr/bin/env python3
"""
download_media.py — Hybrid downloader that uses direct API resolvers for YouTube
and falls back to yt-dlp with BGUtil BotGuard POT provider for 1,700+ platforms.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request


def is_youtube(url: str) -> bool:
    return bool(re.search(r'(youtube\.com|youtu\.be)', url, re.I))


def try_cobalt(url: str, quality: str) -> bool:
    """Attempt extraction via known public Cobalt API instances."""
    instances = [
        "https://api.cobalt.tools",
        "https://cobalt.canine.tools",
        "https://cobalt.tools"
    ]
    
    # Map quality string to Cobalt videoQuality integer
    q_map = {"best": "1080", "1080p": "1080", "720p": "720", "480p": "480", "360p": "360"}
    is_audio = (quality == "audio")
    vq = q_map.get(quality, "720")

    payload = {"url": url}
    if is_audio:
        payload["downloadMode"] = "audio"
    else:
        payload["videoQuality"] = vq

    data_bytes = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    for inst in instances:
        try:
            req = urllib.request.Request(f"{inst}/", data=data_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                status = data.get("status")
                dl_url = data.get("url")
                filename = data.get("filename", "media")
                
                if (status in ("redirect", "stream", "tunnel")) and dl_url:
                    print(f"✅ Cobalt API resolved via {inst}: downloading stream...")
                    ext = "mp3" if is_audio else "mp4"
                    out_name = f"output.{ext}"
                    subprocess.run(["curl", "-L", "-s", "-o", out_name, dl_url], check=True)
                    with open("title.txt", "w", encoding="utf-8") as tf:
                        tf.write(filename.rsplit(".", 1)[0])
                    return True
        except Exception as e:
            continue
    return False


def run_ytdlp(url: str, quality: str, cookie_flag: str, server_home: str):
    format_map = {
        "best": "bestvideo*+bestaudio/best/bestvideo/bestaudio",
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        "audio": "bestaudio/best"
    }
    fmt = format_map.get(quality, format_map["best"])
    is_audio = (quality == "audio")
    
    base_args = [
        "yt-dlp",
        "-f", fmt,
        "--remote-components", "ejs:github",
        "--print-to-file", "%(title)s", "title.txt",
        "--no-playlist"
    ]
    
    if os.path.exists(server_home):
        base_args.extend(["--extractor-args", f"youtubepot-bgutilscript:server_home={server_home}"])
    
    base_args.extend(["--extractor-args", "youtube:player_client=web_embedded,tv,android,ios"])

    def execute_ytdlp(extra_cookies=True):
        cmd = list(base_args)
        if extra_cookies and cookie_flag and os.path.exists("cookies.txt"):
            cmd.extend(["--cookies", "cookies.txt"])
            
        if is_audio:
            cmd.extend([
                "--extract-audio", "--audio-format", "mp3",
                "--audio-quality", "0", "--embed-metadata",
                "-o", "output.%(ext)s", url
            ])
        else:
            cmd.extend([
                "--merge-output-format", "mp4",
                "--embed-subs", "--embed-metadata",
                "-o", "output.%(ext)s", url
            ])
        return subprocess.run(cmd)

    # First try with cookies if available
    res = execute_ytdlp(extra_cookies=True)
    if res.returncode != 0:
        print("⚠️ First attempt failed. Retrying with cookieless clean fallback...")
        res_retry = execute_ytdlp(extra_cookies=False)
        if res_retry.returncode != 0:
            print("❌ yt-dlp download failed.")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--quality", default="best")
    parser.add_argument("--cookies", default="")
    args = parser.parse_args()

    media_url = args.url
    quality = args.quality
    cookie_content = args.cookies
    server_home = os.path.expanduser("~/bgutil-ytdlp-pot-provider/server")

    if cookie_content:
        with open("cookies.txt", "w", encoding="utf-8") as f:
            f.write(cookie_content)

    # Path 1: If YouTube, try direct Cobalt API resolver first
    downloaded = False
    if is_youtube(media_url):
        print("🔍 Detected YouTube URL. Checking fast API resolvers...")
        downloaded = try_cobalt(media_url, quality)

    # Path 2: If not YouTube or if API resolver did not succeed, run yt-dlp
    if not downloaded:
        print("🚀 Running yt-dlp engine...")
        run_ytdlp(media_url, quality, cookie_flag="cookies.txt" if cookie_content else "", server_home=server_home)

    if os.path.exists("cookies.txt"):
        try:
            os.remove("cookies.txt")
        except Exception:
            pass


if __name__ == "__main__":
    main()
