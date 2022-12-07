from flask import Flask, request, jsonify
from src.helpers.compressorModel import Compressor
import json

app = Flask(__name__)
compressor = Compressor()

@app.route('/compress', methods=['POST'])
def compress():
    if request.method == 'POST':
        data = request.data
        data = json.loads(data.decode('utf-8'))
        dir = data['dir']
        print(dir)
        compressor.compress(dir)

    return jsonify({'Status' : 'Compression completed.'})

@app.route('/reconstruct', methods=['POST'])
def reconstruct():
    if request.method == 'POST':
        data = request.data
        data = json.loads(data.decode('utf-8'))
        for file in data.values():
            compressor.reconstruct(file, 'reconstructed')
    return jsonify({'Status' : 'Reconstruction completed.'})
