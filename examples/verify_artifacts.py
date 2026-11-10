"""Verify committed synthetic artifacts without plotting or external packages."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(folder):
    folder=Path(folder).resolve()
    status=folder/'run_status.json'
    if status.exists() and json.loads(status.read_text(encoding='utf-8')).get('status')!='complete':
        raise ValueError('Research run is incomplete or failed')
    if not (folder/'manifest.json').is_file():
        raise ValueError('No completed artifact manifest')
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if 'status' in manifest and manifest['status']!='complete':
        raise ValueError('Research manifest is not complete')
    if not isinstance(manifest.get('files'),dict) or not manifest['files']:
        raise ValueError('Manifest must contain artifact hashes')
    for name,expected in manifest['files'].items():
        path=(folder/name).resolve()
        if not path.is_relative_to(folder):raise ValueError('Manifest path escapes artifact directory')
        if not path.is_file():raise ValueError('Missing artifact: '+name)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('Artifact hash mismatch: '+name)
    return len(manifest['files'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path(__file__).parent/'synthetic')
    args=parser.parse_args()
    print('Verified synthetic artifacts:',verify(args.directory))
