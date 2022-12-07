from compress import prepare_model, prepare_dataloader, compress_and_save, load_and_decompress
from pathlib import Path
import os

class _Compressor:
    
    PATH_TO_MODEL = 'model/hific_low.pt'
    LOG_DIR = 'logs'
    _instance = None

    def compress(self, dir):
        output_dir = dir+'/compressed'
        loader = prepare_dataloader(self.args, dir, output_dir)
        compress_and_save(_Compressor.model, _Compressor.args, loader, output_dir)

    def reconstruct(self, file, output_dir):
        load_and_decompress(_Compressor.model, file, output_dir +'/'+os.path.basename(file)+'.png')

def Compressor():
    if _Compressor._instance is None:
        Path(_Compressor.LOG_DIR).mkdir(exist_ok=True, parents=True)
        _Compressor._instance = _Compressor()
        _Compressor.model, _Compressor.args = prepare_model(_Compressor.PATH_TO_MODEL, _Compressor.LOG_DIR)
    
    return _Compressor._instance

if __name__ == '__main__':
    print(os.path.exists(_Compressor.PATH_TO_MODEL))