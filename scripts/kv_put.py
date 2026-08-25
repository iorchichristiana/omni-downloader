#!/usr/bin/env python3
"""
kv_put.py — Write a key-value pair to a Cloudflare Workers KV namespace.

Usage:
    python3 kv_put.py \
        --account-id <id> \
        --namespace-id <id> \
        --api-token <token> \
        --key "media:abc123" \
        --value '{"ext":"mkv","mime":"video/x-matroska","size":12345}' \
        [--ttl 3600]

Correct endpoint:
    PUT /accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key_name}
"""

import argparse
import sys
import urllib.parse
import urllib.request
import json


def main():
    parser = argparse.ArgumentParser(description="Write to Cloudflare KV")
    parser.add_argument("--account-id", required=True, help="Cloudflare account ID")
    parser.add_argument("--namespace-id", required=True, help="KV namespace ID")
    parser.add_argument("--api-token", required=True, help="Cloudflare API token")
    parser.add_argument("--key", required=True, help="KV key name")
    parser.add_argument("--value", required=True, help="Value to store (string or JSON)")
    parser.add_argument("--ttl", type=int, default=None, help="Expiration TTL in seconds (optional)")
    args = parser.parse_args()

    # URL-encode the key (it may contain colons, slashes, etc.)
    encoded_key = urllib.parse.quote(args.key, safe="")

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{args.account_id}"
        f"/storage/kv/namespaces/{args.namespace_id}/values/{encoded_key}"
    )

    # If value looks like JSON, keep it as-is; otherwise treat as plain string
    try:
        json.loads(args.value)
        body = args.value.encode("utf-8")
    except (json.JSONDecodeError, TypeError):
        body = args.value.encode("utf-8")

    headers = {
        "Authorization": f"Bearer {args.api_token}",
        "Content-Type": "application/json",
    }

    # KV API supports expirationTtl as a query param
    if args.ttl:
        url += f"?expirationTtl={args.ttl}"

    req = urllib.request.Request(url, data=body, headers=headers, method="PUT")

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("success"):
                print(f"✅ KV put success: {args.key}")
            else:
                print(f"❌ KV put failed: {result.get('errors')}", file=sys.stderr)
                sys.exit(1)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ KV put HTTP {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ KV put error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
