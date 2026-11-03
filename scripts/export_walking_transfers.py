"""Export only public facility links and the compiled OSM graph; no HTTP calls."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from project_paths import PROJECT_ROOT, RUNTIME_CACHE_DIR
from hybrid_walking import WalkGraph
from kakao_routes import KakaoClient, walking_edge
from portable_transfers import coordinates, public_node

FIELDS = ('mode','from','to','minutes','distance_m','source','geometry','fetched_at',
          'snap_distance_m','graph_version','major_road_fraction','needs_kakao_verification')


def export(nodes, runtime, graph_path, output):
    runtime, output = Path(runtime), Path(output)
    graph = WalkGraph(graph_path)
    nodes = {k:v for k,v in nodes.items() if public_node(k)}
    counts = dict(kakao=0, osm=0)
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='walking-export-',dir=output) as folder:
        path = Path(folder) / 'walking.sqlite'
        source = sqlite3.connect(Path(graph_path).resolve().as_uri()+'?mode=ro',uri=True)
        tables={r[0] for r in source.execute("select name from sqlite_master where type='table'")}
        if tables-{'nodes','edges','tiles','metadata'}:
            source.close();raise ValueError('Unexpected tables in public walking graph')
        db = sqlite3.connect(path)
        source.backup(db); source.close()
        db.execute('pragma journal_mode=DELETE')
        db.executescript('''
          create table transfers(provider text,a text,b text,xy text,payload text,expires_at real,
                                 primary key(provider,a,b,xy)) without rowid;
          create table bundle_meta(key text primary key,value text);
        ''')
        db.execute('insert into bundle_meta values(?,?)',('graph_identity',graph.content_signature))

        def save(provider, edge, expiry=None):
            a,b = edge.get('from'),edge.get('to')
            if a not in nodes or b not in nodes or a==b:
                return
            if a.startswith('bike:') and b.startswith('bike:'):
                return
            clean = {k:edge[k] for k in FIELDS if k in edge}
            db.execute('insert or replace into transfers values(?,?,?,?,?,?)',
                       (provider,a,b,coordinates(nodes[a],nodes[b]),
                        json.dumps(clean,ensure_ascii=False,allow_nan=False),expiry))

        api = sqlite3.connect((runtime/'scheduled_kakao_walk.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        links = sqlite3.connect((runtime/'walking_links.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        proxy = SimpleNamespace(db=api,hits=0)
        for a,b,xy in links.execute('select a,b,xy from links'):
            if a not in nodes or b not in nodes or json.loads(xy)!=json.loads(coordinates(nodes[a],nodes[b])):
                continue
            hit=KakaoClient.get_cached(proxy,'walk',nodes[a],nodes[b])
            if hit is not None:
                edge=walking_edge(*hit,a,b)
                if edge:
                    save('kakao',edge,datetime.fromisoformat(hit[1]).timestamp()+7*86400)
        api.close();links.close()
        osm=sqlite3.connect((runtime/'osm_walks.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        for (raw,) in osm.execute('select payload from osm_walks'):
            edge=json.loads(raw); a,b=edge.get('from'),edge.get('to')
            if a not in nodes or b not in nodes or edge.get('graph_version')!=graph.signature:
                continue
            if not {'needs_kakao_verification','major_road_fraction','snap_distance_m'}.issubset(edge):
                continue
            points=(edge.get('geometry') or {}).get('coordinates',[])
            if not points or points[0]!=[nodes[a]['longitude'],nodes[a]['latitude']] or points[-1]!=[nodes[b]['longitude'],nodes[b]['latitude']]:
                continue
            save('osm',edge)
        osm.close()
        db.commit();db.execute('pragma wal_checkpoint(TRUNCATE)')
        for provider in counts:
            counts[provider]=db.execute('select count(*) from transfers where provider=?',(provider,)).fetchone()[0]
        db.close()
        output.mkdir(parents=True,exist_ok=True)
        archive=output/'walking_transfers.sqlite.gz'
        temporary=archive.with_suffix('.tmp')
        with path.open('rb') as src,temporary.open('wb') as packed:
            with gzip.GzipFile(fileobj=packed,mode='wb',mtime=0,filename='') as dst:
                for block in iter(lambda:src.read(1024*1024),b''):dst.write(block)
        if temporary.stat().st_size>=100*1024**2:
            temporary.unlink();raise ValueError('Archive exceeds GitHub file limit')
        temporary.replace(archive)
        meta=dict(schema_version=1,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                  uncompressed_bytes=path.stat().st_size,graph_identity=graph.content_signature,
                  records=counts,created_at=datetime.now(timezone.utc).isoformat(),
                  privacy='public facilities only; no accounts, user endpoints, weather, stock or API usage')
        (output/'walking_transfers.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
        return meta


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--nodes',type=Path,required=True,help='Public facility coordinate JSON')
    p.add_argument('--runtime',type=Path,default=RUNTIME_CACHE_DIR)
    p.add_argument('--graph',type=Path,default=PROJECT_ROOT/'data/walking/seoul_walk.sqlite')
    p.add_argument('--output',type=Path,default=PROJECT_ROOT/'data/reference')
    args=p.parse_args()
    print(json.dumps(export(json.loads(args.nodes.read_text(encoding='utf-8')),args.runtime,args.graph,args.output),ensure_ascii=False))
