#!/usr/bin/env python3
"""Resolve official Orca JSON presets using names from the vendor manifest.

Run manually at setup time, never from a Telegram upload. The revision is pinned;
profile values and names are not inferred from similarly named files.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import urllib.parse
import urllib.request

REVISION = '40ecc66f78ed72dc368e0c1ebe40d77f00cc8a19'
BASE = 'https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/'


class Resolver:
    def __init__(self, vendor='Creality', revision=REVISION):
        self.vendor, self.revision = vendor, revision
        self.base = BASE + revision + '/resources/profiles/'
        manifest, _ = self.fetch(vendor + '.json')
        self.index = {}
        for kind, key in [('machine', 'machine_list'), ('process', 'process_list'), ('filament', 'filament_list')]:
            for item in manifest.get(key, []):
                self.index[(kind, item['name'])] = item['sub_path']
        self.cache, self.sources = {}, {}

    def fetch(self, relative):
        parts = PurePosixPath(relative)
        if parts.is_absolute() or '..' in parts.parts:
            raise ValueError('unsafe_manifest_path')
        url = self.base + urllib.parse.quote(relative, safe='/')
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('profile_too_large')
        return json.loads(raw), {'url': url, 'sha256': hashlib.sha256(raw).hexdigest()}

    def resolve(self, kind, name, stack=()):
        key = (kind, name)
        if key in self.cache:
            return dict(self.cache[key])
        if key in stack:
            raise ValueError('profile_inheritance_cycle')
        if key not in self.index:
            raise ValueError('parent_not_in_vendor_manifest: ' + repr(key))
        profile, source = self.fetch(self.vendor + '/' + self.index[key])
        parent = profile.get('inherits', '')
        if not isinstance(parent, str):
            raise ValueError('unsupported_inheritance')
        merged = self.resolve(kind, parent, stack + (key,)) if parent else {}
        merged.update(profile)
        merged.pop('inherits', None)
        # System source metadata plus explicit type means the CLI can use this
        # effective JSON directly without looking for a missing compiled parent.
        merged.update({'type': kind, 'from': 'system', 'name': name})
        self.sources[kind + ':' + name] = dict(source, parent=parent)
        self.cache[key] = dict(merged)
        return merged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--vendor', default='Creality')
    parser.add_argument('--machine', default='Creality K1C 0.4 nozzle')
    parser.add_argument('--process', default='0.20mm Standard @Creality K1C')
    parser.add_argument('--filament', default='Generic PETG @K1C-all')
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    resolver = Resolver(args.vendor)
    for kind in ('machine', 'process', 'filament'):
        profile = resolver.resolve(kind, getattr(args, kind))
        destination = output / ('flat-' + kind + '.json')
        destination.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + '\n')
        print(str(destination) + ': ' + str(len(profile)) + ' fields')
    (output / 'flat-profile-sources.json').write_text(json.dumps({'revision': REVISION, 'sources': resolver.sources}, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
