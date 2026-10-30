"""Read portable public assets; leave local credentials and mutable data alone."""

from pathlib import Path
import gzip, hashlib, json, os, tempfile
from project_paths import PROJECT_ROOT, CACHE_ROOT, ETA_MODEL_DIR, RUNTIME_CACHE_DIR


def prepare_route_cache():
    manifest = json.loads((ETA_MODEL_DIR / 'manifest.json').read_text(encoding='utf-8'))
    cache = Path(os.getenv('BICYCLE_CACHE_DIR', str(RUNTIME_CACHE_DIR / 'eta')))
    return seed_route_cache(cache / 'routes_v2.sqlite', manifest['graph_signature'])


def ocr_model_options():
    bundled = PROJECT_ROOT/'models/ocr'
    if all((bundled/name).is_file() for name in ('craft_mlt_25k.pth','korean_g2.pth')):
        return dict(model_storage_directory=str(bundled),download_enabled=False)
    return dict(model_storage_directory=str(CACHE_ROOT/'ocr_models'))


def seed_route_cache(cache_path, graph_signature, reference=None):
    """Initialize only a missing cache. Never replace or merge an existing DB."""
    path=Path(cache_path)
    if path.exists():
        return False
    reference=Path(reference or PROJECT_ROOT/'data/reference')
    archive, manifest=reference/'osm_station_routes.sqlite.gz', reference/'manifest.json'
    if not archive.is_file() or not manifest.is_file():
        return False
    meta=json.loads(manifest.read_text(encoding='utf-8'))
    if meta.get('graph_signature')!=graph_signature:
        return False
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=meta['sha256']:
        raise ValueError('공유 OSM 경로 자료의 체크섬이 다릅니다. Git에서 다시 받아 주세요.')
    path.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix='routes-seed-',suffix='.tmp',dir=path.parent)
    os.close(handle)
    temporary=Path(name)
    try:
        size=0
        with gzip.open(archive,'rb') as src, temporary.open('wb') as dst:
            for block in iter(lambda:src.read(1024*1024),b''):
                size+=len(block)
                if size>512*1024*1024:
                    raise ValueError('공유 경로 자료 크기가 예상 범위를 초과했습니다.')
                dst.write(block)
        if meta.get('uncompressed_bytes') is not None and size!=meta['uncompressed_bytes']:
            raise ValueError('공유 경로 자료 크기가 일치하지 않습니다.')
        try:
            os.link(temporary,path)
        except FileExistsError:
            return False
        except OSError:
            # Unsupported filesystem: ordinary route calculation remains usable.
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)
