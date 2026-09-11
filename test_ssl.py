import requests
import certifi

print('certifi bundle:', certifi.where())
r = requests.get('https://video.bunnycdn.com', timeout=10, verify=certifi.where())
print('Status:', r.status_code)