import json, os, sys, urllib.parse, urllib.request, boto3
from botocore.config import Config

SOFT_LIMIT_BYTES = int(9.5 * 1024 * 1024 * 1024)
TARGET_LIMIT_BYTES = int(8.0 * 1024 * 1024 * 1024)

def delete_kv_key(account_id, namespace_id, api_token, key_name):
    if not (account_id and namespace_id and api_token):
        return
    encoded = urllib.parse.quote(key_name, safe='')
    url = f'https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{encoded}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {api_token}'}, method='DELETE')
    try:
        with urllib.request.urlopen(req) as resp:
            print(f'Deleted KV entry: {key_name}')
    except Exception as e:
        print(f'Warning: Failed to delete KV key {key_name}: {e}', file=sys.stderr)

def main():
    account_id = os.environ.get('R2_ACCOUNT_ID') or os.environ.get('CF_ACCOUNT_ID')
    access_key = os.environ.get('R2_ACCESS_KEY') or os.environ.get('R2_ACCESS_KEY_ID')
    secret_key = os.environ.get('R2_SECRET_KEY') or os.environ.get('R2_SECRET_ACCESS_KEY')
    namespace_id = os.environ.get('CF_KV_NAMESPACE_ID') or os.environ.get('KV_NAMESPACE_ID')
    api_token = os.environ.get('CF_API_TOKEN')
    if not (account_id and access_key and secret_key):
        print('Missing R2 credentials')
        return
    s3 = boto3.client('s3', endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com', aws_access_key_id=access_key, aws_secret_access_key=secret_key, config=Config(signature_version='s3v4'), region_name='auto')
    resp = s3.list_objects_v2(Bucket='dl-cache')
    objects = resp.get('Contents', [])
    total_size = sum(obj.get('Size', 0) for obj in objects)
    total_gb = total_size / (1024 * 1024 * 1024)
    print(f'Total cached objects: {len(objects)}, Total size: {total_gb:.2f} GB')
    if total_size <= SOFT_LIMIT_BYTES:
        print('Storage is within 9.5 GB limit. No eviction needed.')
        return

    print(f'Storage ({total_gb:.2f} GB) exceeds 9.5 GB soft limit. Evicting oldest files to reach 8.0 GB target...')
    
    # Sort oldest first by LastModified timestamp
    objects.sort(key=lambda x: x.get('LastModified'))

    evicted_count = 0
    evicted_bytes = 0

    for obj in objects:
        key = obj.get('Key')
        size = obj.get('Size', 0)
        
        # Delete from R2
        try:
            s3.delete_object(Bucket='dl-cache', Key=key)
            print(f'Deleted R2 object: {key} ({size / (1024 * 1024):.1f} MB)')
        except Exception as e:
            print(f'Warning: Failed to delete R2 object {key}: {e}', file=sys.stderr)

        # Delete from KV index (media:<key_base>)
        key_base = key.rsplit('.', 1)[0]
        delete_kv_key(account_id, namespace_id, api_token, f'media:{key_base}')

        total_size -= size
        evicted_count += 1
        evicted_bytes += size

        if total_size <= TARGET_LIMIT_BYTES:
            break

    print(f'Eviction complete: Removed {evicted_count} objects ({evicted_bytes / (1024 * 1024):.1f} MB). Current storage: {total_size / (1024 * 1024 * 1024):.2f} GB.')


if __name__ == '__main__':
    main()
