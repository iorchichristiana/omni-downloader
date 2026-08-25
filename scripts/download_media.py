#!/usr/bin/env python3
"""
download_media.py — Robust media downloader powered by yt-dlp with mobile client fallbacks,
EJS challenge solvers, and multi-format mapping for 1,700+ platforms.
"""

import argparse
import os
import re
import subprocess
import sys


def is_youtube(url: str) -> bool:
    return bool(re.search(r'(youtube\.com|youtu\.be)', url, re.I))


def run_ytdlp(url: str, quality: str, cookie_flag: str):
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
    
    # Base arguments
    base_args = [
        "yt-dlp",
        "-f", fmt,
        "--remote-components", "ejs:github",
        "--print-to-file", "%(title)s", "title.txt",
        "--no-playlist"
    ]
    
    if is_youtube(url):
        # Use mobile clients specifically to bypass datacenter IP bot checks
        base_args.extend(["--extractor-args", "youtube:player_client=android_creator,android,ios"])

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

    # Attempt 1: With cookies if provided
    res = execute_ytdlp(extra_cookies=True)
    if res.returncode != 0:
        print("⚠️ First attempt returned non-zero code. Retrying with clean mobile extractor...")
        # Attempt 2: Clean cookieless fallback
        res_retry = execute_ytdlp(extra_cookies=False)
        if res_retry.returncode != 0:
            print("❌ Media download failed.")
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

    if cookie_content:
        with open("cookies.txt", "w", encoding="utf-8") as f:
            f.write(cookie_content)

    try:
        run_ytdlp(media_url, quality, cookie_flag="cookies.txt" if cookie_content else "")
    finally:
        if os.path.exists("cookies.txt"):
            try:
                os.remove("cookies.txt")
            except Exception:
                pass


if __name__ == "__main__":
    main()
