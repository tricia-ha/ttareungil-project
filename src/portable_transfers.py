"""Read-only public-facility walking data; no credentials or user endpoints."""

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from project_paths import PROJECT_ROOT, RUNTIME_CACHE_DIR


def public_node(value):
    return isinstance(value, str) and value.startswith(('bus:', 'subway:', 'bike:'))


def coordinates(a, b):
    return json.dumps([[float(a['latitude']), float(a['longitude'])],
                       [float(b['latitude']), float(b['longitude'])]])


def prepare(reference=None, cache=None):
    reference = Path(reference or PROJECT_ROOT / 'data/reference')
    manifest = reference / 'walking_transfers.json'
    archive = reference / 'walking_transfers.sqlite.gz'
    if not manifest.exists() or not archive.exists():
        return None
    meta = json.loads(manifest.read_text(encoding='utf-8'))
    if meta.get('schema_version') != 1:
        raise ValueError('Unsupported public walking-cache schema')
    signature = meta['sha256']
    if len(signature) != 64 or any(c not in '0123456789abcdef' for c in signature):
        raise ValueError('Invalid public walking-cache checksum')
    target = Path(cache or RUNTIME_CACHE_DIR) / 'shared_walking' / (signature + '.sqlite')
    if target.exists():
        return target
    if hashlib.sha256(archive.read_bytes()).hexdigest() != signature:
        raise ValueError('공유 환승 자료가 손상되었습니다. Git에서 다시 받아 주세요.')
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=target.parent, prefix='walking-', suffix='.tmp')
    os.close(handle)
    temporary = Path(name)
    try:
        size = 0
        with gzip.open(archive, 'rb') as source, temporary.open('wb') as destination:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                size += len(block)
                if size > 512 * 1024 * 1024:
                    raise ValueError('Public walking data exceeds size limit')
                destination.write(block)
        if size != meta['uncompressed_bytes']:
            raise ValueError('Public walking data size mismatch')
        # Atomic installation across workers. Each version is immutable.
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        except OSError:
            if not target.exists():
                try:
                    os.replace(temporary, target)
                except OSError:
                    if not target.exists():
                        raise
        return target if target.exists() else None
    finally:
        temporary.unlink(missing_ok=True)


class SharedTransfers:
    def __init__(self, reference=None, cache=None):
        self.path = prepare(reference, cache)
        self.db = None
        self.graph_identity = None
        if self.path:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
            self.graph_identity = self.db.execute(
                "select value from bundle_meta where key='graph_identity'").fetchone()[0]

    def get(self, provider, a, b, start, end, graph_identity=None):
        if self.db is None or not public_node(start) or not public_node(end):
            return None
        if provider == 'osm' and graph_identity != self.graph_identity:
            return None
        row = self.db.execute(
            'select payload,expires_at from transfers where provider=? and a=? and b=? and xy=?',
            (provider, start, end, coordinates(a, b))).fetchone()
        if row is None or (row[1] is not None and time.time() > row[1]):
            return None
        value = json.loads(row[0])
        if not math.isfinite(float(value['minutes'])) or float(value['minutes']) <= 0:
            return None
        value.update({'from': start, 'to': end, 'shared_transfer_cache': True})
        return value

    def kakao_pairs(self):
        if self.db is None:
            return ()
        return self.db.execute(
            "select a,b,xy from transfers where provider='kakao' and expires_at>=?",
            (time.time(),))

    def close(self):
        if self.db is not None:
            self.db.close()
