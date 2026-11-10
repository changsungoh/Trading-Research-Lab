"""Verify committed synthetic artifacts without plotting or external packages."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(folder):
    folder=Path(folder).resolve()
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    for name,expected in manifest['files'].items():
        path=(folder/name).resolve()
        if not path.is_relative_to(folder):raise ValueError('Manifest path escapes artifact directory')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('Artifact hash mismatch: '+name)
    return len(manifest['files'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path(__file__).parent/'synthetic')
    args=parser.parse_args()
    print('Verified synthetic artifacts:',verify(args.directory))
