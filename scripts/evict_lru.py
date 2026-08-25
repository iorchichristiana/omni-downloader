#!/usr/bin/env python3
\"\"\"
evict_lru.py — Evict LRU (Least Recently Used/Modified) objects from R2
when bucket storage exceeds 9.5 GB, keeping total storage safely below the
Cloudflare R2 10 GB free tier.
\"\"\"

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

SOFT_LIMIT_BYTES = 9.5 * 1024 * 1024 * 1024  # 9.5 GB trigger
TARGET_LIMIT_BYTES = 8.0 * 1024 * 1024 * 1024  # 8.0 GB target after cleanup


def delete_kv_key(account_id, namespace_id, api_token, key_name):
    if not (account_id and namespace_id and api_token):
        return
    encoded = urllib.parse.quote(key_name, safe=\"\")
    url = f\"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{encoded}\"
    req = urllib.request.Request(
        url,
        headers={\"Authorization\": f\"Bearer {api_token}\"},
        method=\"DELETE\"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f\"  Deleted KV entry: {key_name}\")
    except Exception as e:
        print(f\"  Warning: Failed to delete KV key {key_name}: {e}\", file=sys.stderr)


def main():
    account_id = os.environ.get(\"R2_ACCOUNT_ID\") or os.environ.get(\"CF_ACCOUNT_ID\")
    namespace_id = os.environ.get(\"CF_KV_NAMESPACE_ID\") or os.environ.get(\"KV_NAMESPACE_ID\")
    api_token = os.environ.get(\"CF_API_TOKEN\")

    print(\"?? Listing R2 objects in bucket dl-cache...\")
    try:
        proc = subprocess.run(
            [\"rclone\", \"lsjson\", \"r2:dl-cache/\"],
            capture_output=True,
            text=True,
            check=True
        )
        objects = json.loads(proc.stdout)
    except Exception as e:
        print(f\"? Failed to list R2 objects: {e}\", file=sys.stderr)
        sys.exit(1)

    total_size = sum(obj.get(\"Size\", 0) for obj in objects)
    total_gb = total_size / (1024 * 1024 * 1024)
    print(f\"?? Total cached objects: {len(objects)}, Total size: {total_gb:.2f} GB\")

    if total_size <= SOFT_LIMIT_BYTES:
        print(\"? Storage is well within the 9.5 GB soft limit. No eviction needed.\")
        return

    print(f\"?? Storage exceeds 9.5 GB soft limit. Evicting oldest objects until under 8.0 GB...\")

    # Sort oldest first by ModTime
    objects.sort(key=lambda x: x.get(\"ModTime\", \"\"))

    current_size = total_size
    evicted_count = 0

    for obj in objects:
        if current_size <= TARGET_LIMIT_BYTES:
            break

        path = obj.get(\"Path\")
        size = obj.get(\"Size\", 0)
        print(f\"??? Evicting: {path} ({size / (1024*1024):.1f} MB, modified {obj.get('ModTime')})\")

        # Delete from R2
        del_proc = subprocess.run(
            [\"rclone\", \"deletefile\", f\"r2:dl-cache/{path}\"],
            capture_output=True,
            text=True
        )
        if del_proc.returncode != 0:
            print(f\"  Warning: Failed to delete R2 object {path}: {del_proc.stderr}\", file=sys.stderr)
            continue

        # Extract key_base (strip file extension)
        key_base = path.rsplit(\".\", 1)[0]
        delete_kv_key(account_id, namespace_id, api_token, f\"media:{key_base}\")

        current_size -= size
        evicted_count += 1

    remaining_gb = current_size / (1024 * 1024 * 1024)
    print(f\"? Cleanup complete. Evicted {evicted_count} objects. Remaining size: {remaining_gb:.2f} GB.\")


if __name__ == \"__main__\":
    main()
