import requests
import json
from os import listdir
from os.path import isfile, join

hific_files = [f for f in listdir("compressed") if isfile(join("./compressed", f)) and f.split('.')[-1] in ['hfc']]

payload = dict()
for i, file in enumerate(hific_files):
    payload[str(i)] = 'compressed/' + file

print(payload)
resp = requests.post("http://localhost:5000/reconstruct", data=json.dumps(payload))

print(resp.text)