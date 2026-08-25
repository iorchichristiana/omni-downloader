import argparse, sys, boto3
from botocore.config import Config

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--account-id', required=True)
    p.add_argument('--access-key', required=True)
    p.add_argument('--secret-key', required=True)
    p.add_argument('--bucket', default='dl-cache')
    p.add_argument('--key', required=True)
    p.add_argument('--file', required=True)
    p.add_argument('--content-type', default='application/octet-stream')
    args = p.parse_args()
    try:
        s3 = boto3.client('s3', endpoint_url=f'https://{args.account_id}.r2.cloudflarestorage.com', aws_access_key_id=args.access_key, aws_secret_access_key=args.secret_key, config=Config(signature_version='s3v4'), region_name='auto')
        with open(args.file, 'rb') as f:
            s3.put_object(Bucket=args.bucket, Key=args.key, Body=f, ContentType=args.content_type)
        print(f'R2 upload success: {args.key}')
    except Exception as e:
        print(f'R2 upload failed: {e}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
