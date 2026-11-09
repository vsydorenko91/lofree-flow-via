#!/usr/bin/env python3
"""Stage the web flasher: build (or take out/) the image, copy it, the stock image and the
VIA definition next to web/index.html, and write web/firmware/manifest.json."""
import argparse, hashlib, json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oe915, flash

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, default=ROOT / 'out')
    p.add_argument('--version', default='dev')
    a = p.parse_args()
    image = a.out / 'flow84-via.bin'
    data = flash.validate(image, a.out / 'manifest.json')      # stock + declared changes only
    dest = ROOT / 'web/firmware'
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'flow84-via.bin').write_bytes(data)
    shutil.copyfile(oe915.STOCK_PATH, dest / 'stock.bin')
    shutil.copyfile(ROOT / 'definitions/flow84.json', ROOT / 'web/flow84.json')
    sha = lambda b: hashlib.sha256(b).hexdigest()
    manifest = {'version': a.version, 'images': {
        'via': {'file': 'flow84-via.bin', 'size': len(data), 'sha256': sha(data),
                'label': 'VIA firmware (this project)'},
        'stock': {'file': 'stock.bin', 'size': len(oe915.load_stock()), 'sha256': oe915.STOCK_SHA,
                  'label': 'Stock Lofree firmware v2.52'}}}
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
