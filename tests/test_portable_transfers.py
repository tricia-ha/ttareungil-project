import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hybrid_walking import HybridWalking, WalkGraph
from multimodal import Network
from portable_transfers import SharedTransfers, coordinates, prepare
from scheduled_runtime import KakaoWalking
from walking_link_cache import WalkingLinkCache

A, B = 'subway:test:A', 'bus:123'
a = dict(latitude=37.5, longitude=127.0)
b = dict(latitude=37.501, longitude=127.0)


@pytest.fixture
def bundle(tmp_path):
    reference=tmp_path/'clone/data/reference';reference.mkdir(parents=True)
    path=tmp_path/'source.sqlite';db=sqlite3.connect(path)
    db.executescript('''
      create table nodes(id integer primary key,lon real,lat real);
      create table edges(u integer,v integer,meters real,seconds real,uncertain integer,primary key(u,v));
      create table tiles(id text,bounds text);
      create table transfers(provider text,a text,b text,xy text,payload text,expires_at real,
                             primary key(provider,a,b,xy));
      create table bundle_meta(key text,value text);
    ''')
    db.executemany('insert into nodes values(?,?,?)',[(1,127,37.5),(2,127,37.501)])
    db.execute('insert into edges values(1,2,111,92.5,0)')
    db.execute('insert into tiles values(?,?)',('t',json.dumps([37.49,126.99,37.52,127.02])))
    db.commit()
    graph=WalkGraph(path);edge=graph.route(a,b,A,B)
    db.execute('insert into bundle_meta values(?,?)',('graph_identity',graph.content_signature))
    for provider in ['osm','kakao']:
        value=dict(edge)
        if provider=='kakao':value.update(minutes=2,source='kakao_walk')
        db.execute('insert into transfers values(?,?,?,?,?,?)',
                   (provider,A,B,coordinates(a,b),json.dumps(value),time.time()+86400 if provider=='kakao' else None))
    db.commit();db.close()
    archive=reference/'walking_transfers.sqlite.gz';archive.write_bytes(gzip.compress(path.read_bytes(),mtime=0))
    meta=dict(schema_version=1,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),uncompressed_bytes=path.stat().st_size)
    (reference/'walking_transfers.json').write_text(json.dumps(meta),encoding='utf-8')
    return reference,path


def test_fresh_clone_restores_public_transfer_without_api_or_private_folder(bundle,tmp_path):
    ref,_=bundle
    links=WalkingLinkCache(tmp_path/'fresh/cache/links.sqlite',reference=ref)
    client=SimpleNamespace(get_cached=lambda *args:None)
    net=Network({A:a,B:b},walk_policy={'max_distance_m':900})
    try:
        assert links.restore(net,client)==1
        assert net.walks[0]['minutes']==2
        walking=KakaoWalking(client,links)
        assert walking.cached(a,b,A,B)[0]
        assert not walking.cached(a,b,'origin',B)[0]
        assert not walking.cached(dict(a,latitude=37.50001),b,A,B)[0]
        assert not links.shared.db.execute("select name from sqlite_master where name='kakao_usage'").fetchall()
    finally:links.close()


def test_expired_shared_kakao_is_not_rejuvenated(bundle,tmp_path):
    ref,_=bundle
    shared=SharedTransfers(ref,tmp_path/'cache')
    try:
        with patch('portable_transfers.time.time',return_value=time.time()+8*86400):
            assert shared.get('kakao',a,b,A,B) is None
    finally:shared.close()


def test_shared_osm_survives_path_mtime_change_but_not_graph_change(bundle,tmp_path):
    ref,original=bundle
    links=WalkingLinkCache(tmp_path/'cache/links.sqlite',reference=ref)
    client=SimpleNamespace(get_cached=lambda *args:None)
    graph_path=links.shared.path
    assert graph_path!=original
    os.utime(original,(time.time()-30,time.time()-30))
    assert WalkGraph(original).signature!=WalkGraph(graph_path).signature
    walking=HybridWalking(KakaoWalking(client,links),graph_path,tmp_path/'osm.sqlite')
    walking.extra_rules=[]
    try:
        # Test the OSM route independently of the authoritative override.
        with patch.object(links,'shared_edge',return_value=None),patch.object(walking.graph,'route',side_effect=AssertionError('must use shared cache')):
            hit,value=walking.cached(a,b,A,B)
            assert hit and value['shared_transfer_cache']
            assert value['graph_version']==walking.graph.signature
            assert links.shared.get('osm',a,b,A,B,graph_identity='different graph') is None
    finally:walking.close();links.close()


def test_new_local_response_including_no_route_wins(bundle,tmp_path):
    ref,_=bundle;links=WalkingLinkCache(tmp_path/'cache/links.sqlite',reference=ref)
    client=SimpleNamespace(get_cached=lambda *args:({'status':'NO_ROUTE'},'2026-11-03T00:00:00+00:00'))
    try:assert KakaoWalking(client,links).cached(a,b,A,B)==(True,None)
    finally:links.close()


def test_corrupted_archive_rejected_and_concurrent_install_is_idempotent(bundle,tmp_path):
    ref,_=bundle
    with ThreadPoolExecutor(max_workers=2) as pool:
        targets=list(pool.map(lambda _:prepare(ref,tmp_path/'cache'),range(2)))
    target=targets[0]
    assert targets==[target,target]
    assert prepare(ref,tmp_path/'cache')==target
    assert not list(target.parent.glob('walking-*.tmp'))
    (ref/'walking_transfers.sqlite.gz').write_bytes(b'bad data')
    with pytest.raises(ValueError):prepare(ref,tmp_path/'other-cache')


def test_atomic_install_without_hardlink_support(bundle,tmp_path):
    ref,_=bundle
    with patch('portable_transfers.os.link',side_effect=OSError('hardlinks unavailable')):
        target=prepare(ref,tmp_path/'cache')
    assert target.is_file()
    with sqlite3.connect(target) as db:
        assert db.execute('select count(*) from transfers').fetchone()[0]==2
