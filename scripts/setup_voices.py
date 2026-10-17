#!/usr/bin/env python3
"""Install pinned official sherpa-onnx CPU voice models into this project only."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
KOKORO = 'kokoro-int8-multi-lang-v1_1'
ZIPVOICE = 'sherpa-onnx-zipvoice-distill-int8-zh-en-emilia'
ASSETS = [
    {'engine': 'kokoro', 'file': KOKORO + '.tar.bz2', 'directory': KOKORO,
     'bytes': 147031220, 'sha256': 'a1e94694776049035c4f2c6529f003aaece993c76aae9a78995831c3c4dcafc6',
     'url': f'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{KOKORO}.tar.bz2'},
    {'engine': 'zipvoice', 'file': ZIPVOICE + '.tar.bz2', 'directory': ZIPVOICE,
     'bytes': 109162785, 'sha256': '77219c8b40f4ee8d73a7f902305ff6c1128ef9b54461c41b4ca6ed890b6c2803',
     'url': f'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{ZIPVOICE}.tar.bz2'},
    {'engine': 'zipvoice', 'file': 'vocos_24khz.onnx', 'bytes': 54157409,
     'sha256': 'bcb3b970e384161c4d634f0bb9e999ff1c471b34c9bc0b1049a5014065ed3cc0',
     'url': 'https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/vocos_24khz.onnx'},
]


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(asset: dict, models: Path, direct: bool) -> dict:
    destination = models / asset['file']
    if not destination.is_file():
        partial = destination.with_suffix(destination.suffix + '.part')
        command = ['curl', '--fail', '--location', '--retry', '3', '--connect-timeout', '20',
                   '--continue-at', '-', '--output', str(partial), '--silent', '--show-error']
        if direct:
            command += ['--noproxy', '*']
        subprocess.run([*command, asset['url']], check=True)
        if partial.stat().st_size != asset['bytes'] or digest(partial) != asset['sha256']:
            raise RuntimeError(f'Checksum mismatch for {asset["file"]}; partial file retained for inspection')
        partial.rename(destination)
    if destination.stat().st_size != asset['bytes'] or digest(destination) != asset['sha256']:
        raise RuntimeError(f'Checksum mismatch for existing {asset["file"]}; refusing to use it')
    if asset.get('directory') and not (models / asset['directory']).is_dir():
        with tempfile.TemporaryDirectory(dir=models) as temporary:
            with tarfile.open(destination) as archive:
                # Reject traversal, devices and unsafe links in downloaded archives.
                archive.extractall(temporary, filter='data')
            (Path(temporary) / asset['directory']).rename(models / asset['directory'])
    return {**asset, 'verified': True}


def setup(models: Path, engine: str = 'all', direct: bool = False) -> list[dict]:
    models.mkdir(parents=True, exist_ok=True)
    assets = [asset for asset in ASSETS if engine == 'all' or asset['engine'] == engine]
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(lambda asset: install(asset, models, direct), assets))
    manifest_path = models / 'voice-manifest.json'
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        chosen = {asset['file'] for asset in records}
        records = [asset for asset in existing if asset['file'] not in chosen] + records
    manifest_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models-dir', type=Path, default=ROOT / 'models')
    parser.add_argument('--engine', choices=['all', 'kokoro', 'zipvoice'], default='all')
    parser.add_argument('--direct', action='store_true', help='Bypass configured proxies')
    args = parser.parse_args()
    records = setup(args.models_dir, args.engine, args.direct)
    print(json.dumps({'models_dir': str(args.models_dir), 'assets': records}, ensure_ascii=False, indent=2))
