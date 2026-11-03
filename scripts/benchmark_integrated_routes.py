"""Full integrated A/B replay with fixed real inputs and bounded preparation calls."""
import argparse
import copy
import gzip
import gc
import hashlib
import json
import pickle
import shutil
import sqlite3
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
from web_capacity import configure_process, SystemResources, GIB
configure_process()
from web_runtime import prepare
prepare()
from project_paths import (RUNTIME_OUTPUT_DIR, RUNTIME_CACHE_DIR, PROJECT_ROOT,
                           TRANSIT_DATA_DIR, STATION_CATALOG, load_project_env)


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def backup(connection_or_path, target):
    owned = not isinstance(connection_or_path, sqlite3.Connection)
    source = sqlite3.connect(connection_or_path) if owned else connection_or_path
    try:
        with sqlite3.connect(target) as dest:
            source.backup(dest)
    finally:
        if owned:
            source.close()


def load_module(name, path):
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(Path(path).read_text(encoding='utf-8-sig'), str(path), 'exec'), module.__dict__)
    return module


def main(args):
    import requests
    import candidate_edges, transit_seed
    from multimodal import Router, Config, V521BikeProvider
    from multimodal_cache import CachedBikeProvider
    from hybrid_walking import HybridWalking
    from scheduled_runtime import ScheduledConfig, KakaoWalking, build_scheduled_network
    from scheduled_runtime import runtime_code_identity, _artifact_identity, forecast_issue_key
    from search_budget import workload, select_budget
    from kakao_runtime import load_crosswalk, eligible_station_catalog, validate_snapshot
    from station_coordinates import routing_station_catalog
    from inventory_service import InventoryPredictor
    from transit_schedule import build_timetable
    from walking_link_cache import WalkingLinkCache
    from kakao_routes import KakaoClient, CallLimit
    from kma_weather_forecast_provider import KMAWeatherForecastProvider, latitude_longitude_to_kma_grid
    from eta_service import BicycleETAService
    from route_identity import route_signature

    load_project_env()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    previous = Path(args.snapshot)
    manifest = json.loads((previous / 'live_result.json').read_text(encoding='utf-8'))
    departure, as_of = manifest['departure'], manifest['inventory_as_of']
    snapshot_payload = json.loads((previous / 'frozen_inventory.json').read_text(encoding='utf-8'))
    snapshot = validate_snapshot(snapshot_payload, as_of)
    cases = json.loads(Path(args.cases).read_text(encoding='utf-8'))
    case_indexes = {case['id']: index for index, case in enumerate(cases)}
    if args.only_case:
        cases = [c for c in cases if c['id'] == args.only_case]
        if not cases:
            raise ValueError('Unknown benchmark case')
    old = {n: load_module('benchmark_original_' + n, Path(args.baseline_sources) / (n + '.py'))
           for n in ('multimodal', 'candidate_edges', 'transit_seed', 'hybrid_walking')}
    original_lazy, original_seed = candidate_edges.LazyConnections, transit_seed.timetable_seed
    issue_text = forecast_issue_key(now=datetime.fromisoformat(departure))
    issue = datetime.fromisoformat(issue_text)
    inventory = InventoryPredictor()
    stations, coverage = eligible_station_catalog(routing_station_catalog(STATION_CATALOG), load_crosswalk(), inventory, snapshot)
    print('PREPARE timetable / inventory / P50 model', flush=True)
    needs_preparation = any(not (out / c['id'] / 'prepared.pkl.gz').exists() for c in cases)
    table = build_timetable(TRANSIT_DATA_DIR, departure, 180) if needs_preparation else None
    live_weather = KMAWeatherForecastProvider(cache_dir=RUNTIME_CACHE_DIR / 'kma_forecast')
    weather = KMAWeatherForecastProvider(cache_dir=out / 'forecast_snapshot')
    # Replay uses immutable issue-specific payloads, even after an overnight pause.
    weather.cache_ttl_seconds = 100 * 365 * 24 * 60 * 60
    grids = {latitude_longitude_to_kma_grid(float(s['latitude']), float(s['longitude'])) for s in stations}
    grids.add(latitude_longitude_to_kma_grid(37.5665, 126.9780))
    weather_report = dict(grids=len(grids), new_calls=0, failed=[])
    # Close weather inputs over every eligible rental point before timed replay.
    for i, grid in enumerate(sorted(grids), 1):
        path = weather._cache_path(*grid, issue)
        original = live_weather._cache_path(*grid, issue)
        if not path.exists() and original.exists():
            shutil.copy2(original, path)
        fresh = path.exists() and time.time() - path.stat().st_mtime <= weather.cache_ttl_seconds
        try:
            weather._download_grid(*grid, issue)
        except Exception as exc:
            weather_report['failed'].append(dict(grid=grid, error=type(exc).__name__))
            break
        weather_report['new_calls'] += int(not fresh)
        print('WEATHER', i, '/', len(grids), 'new', weather_report['new_calls'], flush=True)
    if not (out / 'weather.json').exists():
        atomic(out / 'weather.json', weather_report)
    atomic(out / 'forecast_snapshot_manifest.json', {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(weather.cache_dir.glob('*.json'))})
    model = BicycleETAService(weather_provider=weather)
    original_route_db = model.routes.database
    common_geometry = out / 'common_geometry.sqlite'
    if not common_geometry.exists():
        backup(original_route_db, common_geometry)
    model.routes.database = sqlite3.connect(common_geometry, check_same_thread=False)
    context = dict(version=2, code=runtime_code_identity(), artifacts=_artifact_identity(), use_kma=True,
                   user_factor=1.0, bike_type='regular', user_type='domestic', forecast_issue=issue_text)
    summary_path = out / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else dict(
        departure=departure, inventory_as_of=as_of, forecast_issue=issue_text, coverage=coverage,
        repeats_per_method=2, preparation_policy='shared budget; alternating reference order; union cached before timed replay',
        timed_external_http=False, cases={})

    def walking_for(method, client, db, links=None):
        cls = HybridWalking if method == 'optimized' else old['hybrid_walking'].HybridWalking
        return cls(KakaoWalking(client, links), ROOT / 'data/walking/seoul_walk.sqlite', db)

    def run_router(method, net, cfg, walking, bike, callback=None):
        with patch.object(candidate_edges, 'LazyConnections', original_lazy if method == 'optimized' else old['candidate_edges'].LazyConnections), \
             patch.object(transit_seed, 'timetable_seed', original_seed if method == 'optimized' else old['transit_seed'].timetable_seed):
            cls = Router if method == 'optimized' else old['multimodal'].Router
            return cls(net, bike, cfg, walking_provider=walking, on_routes=callback).search('origin', 'destination', departure)

    def result_info(result):
        return dict(status=result['status'], complete=result['search_complete'],
            reason=result['termination_reason'], expanded=result['expanded_states'],
            best_minutes=result['routes'][0]['total_minutes'] if result['routes'] else None,
            routes=[dict(minutes=r['total_minutes'], modes=sorted({l['mode'] for l in r['legs']}),
                         signature=repr(route_signature(r['legs']))) for r in result['routes']],
            mixed_found=result['mixed_route_found'], new_predictions=result['bike_predictions'],
            seed_scans=result['timetable_seed_scans'],
            diagnostics={k:v for k,v in result.items()
                         if isinstance(v,(str,int,float,bool)) and k != 'status'})

    try:
        for case in cases:
            index = case_indexes[case['id']]
            folder = out / case['id']
            folder.mkdir(exist_ok=True)
            row = summary['cases'].setdefault(case['id'], dict(label=case['label'], trials=[]))
            atomic(summary_path, summary)
            print('CASE', case['label'], flush=True)
            if not (folder / 'prepared.pkl.gz').exists():
                defaults = ScheduledConfig()
                density_radius = max(defaults.max_connector_distance_m,
                                     defaults.max_access_walk_minutes * 80)
                plan = select_budget(workload(table.nodes, stations, case['origin'], case['destination'],
                                             len(table.transit), density_radius, 'integrated'), 'integrated')
                config = ScheduledConfig(max_seconds=plan['max_seconds'], max_kakao_calls=plan['max_kakao_calls'])
                interrupted_calls = sum(x.get('new_api_calls', 0)
                                        for x in row.get('preparation_attempts', []))
                remaining_calls = max(0, config.max_kakao_calls - interrupted_calls)
                client = KakaoClient(RUNTIME_CACHE_DIR / 'scheduled_kakao_walk.sqlite', max_calls=max(1, remaining_calls), daily_limit=1000)
                client.max_calls = remaining_calls
                links = WalkingLinkCache(RUNTIME_CACHE_DIR / 'walking_links.sqlite')
                walking = walking_for('optimized', client, RUNTIME_CACHE_DIR / 'osm_walks.sqlite', links)
                try:
                    network = build_scheduled_network(table, stations, case['origin'], case['destination'], walking, config, departure=departure)
                    network.metadata['restored_walk_connections'] = links.restore(network, client)
                    cfg = Config(horizon_minutes=180, step_seconds=60, top_k=3, max_seconds=plan['max_seconds'],
                        max_expansions=100000, require_known_inventory=True, allow_future_unknown_inventory=True,
                        max_empty_probability=.1, bike_expansion_batch_size=2, reuse_transit_suffixes=True)
                    provider = V521BikeProvider(model, inventory, snapshot, as_of, use_kma=True, eta_quantile='p50')
                    bike = CachedBikeProvider(provider, RUNTIME_CACHE_DIR / 'scheduled_bike.sqlite', context)
                    references = []
                    cap = client.max_calls
                    order = ['baseline', 'optimized'] if index % 2 == 0 else ['optimized', 'baseline']
                    for pos, method in enumerate(order):
                        client.deadline = None
                        remaining = max(0, cap - client.calls)
                        client.max_calls = client.calls + remaining // 2 if pos == 0 else cap
                        trial_walk = walking if method == 'optimized' else walking_for(method, client, RUNTIME_CACHE_DIR / 'osm_walks.sqlite', links)
                        before = client.calls
                        ref_cfg = copy.copy(cfg)
                        ref_cfg.max_seconds = min(30, cfg.max_seconds)
                        start = time.monotonic()
                        try:
                            with patch.object(KMAWeatherForecastProvider, '_latest_issue', lambda self, now=None: issue):
                                reference = run_router(method, copy.deepcopy(network), ref_cfg, trial_walk, bike)
                            references.append(dict(method=method, seconds=time.monotonic()-start, new_calls=client.calls-before, **result_info(reference)))
                            print('REFERENCE', method, 'calls', client.calls, '/', cap, 'routes', len(reference['routes']), flush=True)
                        finally:
                            if trial_walk is not walking:
                                trial_walk.close()
                    client.max_calls = cap
                    network.metadata['restored_walk_connections'] += links.restore(network, client)
                    cache = folder / 'cache_base'
                    cache.mkdir(exist_ok=True)
                    backup(client.db, cache / 'walk.sqlite')
                    backup(walking.db, cache / 'osm.sqlite')
                    backup(bike.db, cache / 'bike.sqlite')
                    backup(model.routes.database, cache / 'geometry.sqlite')
                    with gzip.open(folder / 'prepared.pkl.gz', 'wb') as f:
                        pickle.dump(dict(network=network, config=cfg.__dict__, budget=plan), f, protocol=pickle.HIGHEST_PROTOCOL)
                    row.update(budget=plan, preparation_api=client.stats(), references=references,
                               network_modes=sorted({e['mode'] for e in network.transit})+['walk','bicycle'])
                    atomic(summary_path, summary)
                finally:
                    row.setdefault('preparation_attempts', []).append(client.stats())
                    atomic(summary_path, summary)
                    if 'bike' in locals():
                        bike.close()
                    walking.close();links.close();client.close()

            with gzip.open(folder / 'prepared.pkl.gz', 'rb') as f:
                prepared = pickle.load(f)
            order = ['baseline','optimized','optimized','baseline'] if index % 2 == 0 else ['optimized','baseline','baseline','optimized']
            for trial_id, method in enumerate(order):
                if any(t['trial_id']==trial_id for t in row['trials']):
                    continue
                trial = folder / 'trial'
                trial.mkdir(exist_ok=True)
                for p in (folder / 'cache_base').glob('*.sqlite'):
                    shutil.copy2(p, trial / p.name)
                client = KakaoClient(trial / 'walk.sqlite', max_calls=prepared['budget']['max_kakao_calls'], daily_limit=1000)
                walking = walking_for(method, client, trial / 'osm.sqlite')
                walking.graph.sources.clear()
                model.routes.database.close()
                model.routes.database = sqlite3.connect(trial / 'geometry.sqlite', check_same_thread=False)
                model.weather_provider = weather
                provider = V521BikeProvider(model, inventory, snapshot, as_of, use_kma=True, eta_quantile='p50')
                bike = CachedBikeProvider(provider, trial / 'bike.sqlite', context)
                # An actual missing cache entry remains unavailable during replay.
                def no_new_walk(a,b,start,end):
                    hit,edge=walking.cached(a,b,start,end)
                    if hit:return edge
                    raise CallLimit('Frozen replay: no new external call')
                walking.__class__ = type('CacheOnly' + method, (walking.__class__,), {'__call__':lambda self,*args: no_new_walk(*args)})
                # Searches read timetable edges and node attributes; only the
                # query's containers/policies/metadata need separate copies.
                net = copy.copy(prepared['network'])
                net.nodes = dict(net.nodes)
                net.walks, net.bikes = list(net.walks), list(net.bikes)
                net.metadata = copy.deepcopy(net.metadata)
                net.walk_policy = copy.deepcopy(net.walk_policy)
                net.bike_policy = copy.deepcopy(net.bike_policy)
                cfg = Config(**prepared['config'])
                probe = SystemResources()
                guard = dict(last=0, stop=False, min_available=None)
                def cancel():
                    now=time.monotonic()
                    if now-guard['last']>.5:
                        guard['last']=now
                        available=probe()['available']
                        if available is not None:
                            guard['min_available']=min(guard['min_available'] or available,available)
                            guard['stop']=available < 3*GIB
                    return guard['stop']
                cfg.cancel_check=cancel
                blocked={}
                def block_http(session, method_name, url, **kwargs):
                    from urllib.parse import urlsplit
                    domain=urlsplit(url).netloc
                    blocked[domain]=blocked.get(domain,0)+1
                    raise RuntimeError('Frozen replay: no external HTTP')
                # Keep previous versions' abandoned search records out of the
                # next version's timing. Cleanup cost is recorded separately.
                pre_cleanup = time.monotonic()
                gc.collect()
                pre_cleanup = time.monotonic()-pre_cleanup
                gc_state = dict(seconds=0.0, collections=0, started=None)
                def record_gc(phase, info):
                    if phase == 'start':
                        gc_state['started'] = time.monotonic()
                    elif gc_state['started'] is not None:
                        gc_state['seconds'] += time.monotonic()-gc_state['started']
                        gc_state['collections'] += 1
                        gc_state['started'] = None
                gc.callbacks.append(record_gc)
                start=time.monotonic();cpu_start=time.process_time();first=[None]
                def partial(value):
                    if first[0] is None and value.get('routes'):
                        first[0]=time.monotonic()-start
                try:
                    with patch.object(KMAWeatherForecastProvider, '_latest_issue',lambda self,now=None:issue), \
                         patch.object(requests.sessions.Session,'request',block_http):
                        result=run_router(method,net,cfg,walking,bike,partial)
                    elapsed=time.monotonic()-start
                    cpu_seconds=time.process_time()-cpu_start
                    gc.callbacks.remove(record_gc)
                    cleanup_started=time.monotonic()
                    collected=gc.collect()
                    cleanup_seconds=time.monotonic()-cleanup_started
                    data=dict(trial_id=trial_id,method=method,seconds=elapsed,
                              process_cpu_seconds=cpu_seconds,
                              cyclic_gc_seconds=gc_state['seconds'],
                              pre_trial_cleanup_seconds=pre_cleanup,
                              post_trial_cleanup_seconds=cleanup_seconds,
                              post_trial_collected_objects=collected,
                              timing_valid=elapsed-cpu_seconds < max(60,3*cfg.max_seconds),
                              first_seconds=first[0],http_attempts_blocked=blocked,new_api_calls=client.calls,
                              memory_guard=guard,**result_info(result))
                    if not data['timing_valid']:
                        data['exclusion_reason'] = 'Long wall-clock gap relative to CPU execution'
                        row.setdefault('excluded_trials', []).append(data)
                        atomic(summary_path, summary)
                        raise RuntimeError('Timing interrupted; rerun the benchmark to resume')
                    row['trials'].append(data)
                    atomic(folder / f'routes_{trial_id}.json', {
                        'routes':result['routes'], 'mixed_routes':result.get('mixed_routes',[])})
                    atomic(summary_path,summary)
                    print('TRIAL',case['id'],method,round(data['seconds'],2),'best',data['best_minutes'],'mixed',data['mixed_found'],'http',blocked,flush=True)
                finally:
                    if record_gc in gc.callbacks:
                        gc.callbacks.remove(record_gc)
                    bike.close();walking.close();client.close()
                    model.routes.database.close()
                    model.routes.database=sqlite3.connect(common_geometry,check_same_thread=False)
                    for p in trial.glob('*.sqlite*'):
                        p.unlink()
            print('CASE_DONE',case['id'],flush=True)
    finally:
        model.close()
        original_route_db.close()
        atomic(summary_path, summary)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,default=RUNTIME_OUTPUT_DIR/'diagnostics/search_upgrade_20261102')
    parser.add_argument('--baseline-sources',type=Path,default=RUNTIME_OUTPUT_DIR/'diagnostics/search_upgrade_20261102/source_before')
    parser.add_argument('--output',type=Path,default=RUNTIME_OUTPUT_DIR/'diagnostics/integrated_multi_od_20261102')
    parser.add_argument('--cases',type=Path,default=PROJECT_ROOT/'data/reference/integrated_benchmark_cases.json')
    parser.add_argument('--only-case')
    main(parser.parse_args())
