#!/usr/bin/env python3
\"\"\"
r2_put.py — Upload a file to Cloudflare R2 bucket using S3 API (boto3)
\"\"\"
import argparse
import sys
import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(\"--account-id\", required=True)
    parser.add_argument(\"--access-key\", required=True)
    parser.add_argument(\"--secret-key\", required=True)
    parser.add_argument(\"--bucket\", default=\"dl-cache\")
    parser.add_argument(\"--key\", required=True)
    parser.add_argument(\"--file\", required=True)
    parser.add_argument(\"--content-type\", default=\"application/octet-stream\")
    args = parser.parse_args()

    try:
        s3 = boto3.client(
            \"s3\",
            endpoint_url=f\"https://{args.account_id}.r2.cloudflarestorage.com\",
            aws_access_key_id=args.access_key,
            aws_secret_access_key=args.secret_key,
            config=Config(signature_version=\"s3v4\"),
            region_name=\"auto\",
        )

        with open(args.file, \"rb\") as f:
            s3.put_object(
                Bucket=args.bucket,
                Key=args.key,
                Body=f,
                ContentType=args.content_type,
            )
        print(f\"? R2 upload success: {args.key}\")
    except Exception as e:
        print(f\"? R2 upload failed: {e}\", file=sys.stderr)
        sys.exit(1)


if __name__ == \"__main__\":
    main()
