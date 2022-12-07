import requests
import json
payload = {'dir': '.'}
resp = requests.post("http://localhost:5000/compress", data=json.dumps(payload))

print(resp.text)