"""Read-only dashboard. Never edits an application or sends an inference request."""
import csv, json, time, asyncio
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout
from . import events
from .activity_details import describe

WEB = Path(__file__).parent / 'web'

class Dashboard:
    def __init__(self, data, upstream, proxy):
        self.data = Path(data)
        self.upstream, self.proxy = upstream, proxy
        self.cache = None
        self.refreshed = 0
        self.lock = asyncio.Lock()
        self.session = None

    def workspace(self):
        with (self.data / 'applications.csv').open(encoding='utf-8-sig', newline='') as file:
            source = list(csv.DictReader(file))
        markets = {}
        for market in ('CN', 'US'):
            records = []
            for item in source:
                if item['market'] != market: continue
                status = item['status']
                if status not in ('submitted', 'attention', 'excluded'):
                    raise ValueError('status must be submitted, attention or excluded')
                records.append({**item, 'job_ids': item.get('job_id', '').split('|') if item.get('job_id') else [],
                                'evidence': [item['note']] if item.get('note') else [], 'sources': [], 'task_id': None})
            markets[market] = {'records': records, 'summary': {
                'total': len(records), **{key: sum(r['status'] == key for r in records)
                                          for key in ('submitted', 'attention', 'excluded')}}, 'sync': {'healthy': True}}
        profile = json.loads((self.data / 'profile.json').read_text(encoding='utf-8'))
        pending = json.loads((self.data / 'pending.json').read_text(encoding='utf-8'))
        pending = sorted((p for p in pending if not p.get('resolved', False)),
                         key=lambda p: p.get('updated') or p.get('created') or '', reverse=True)
        return {'domestic': markets['CN'], 'us': markets['US'], 'pending': pending,
                'facts_count': len(profile.get('facts', [])),
                'confirmed_count': len(profile.get('confirmed', {}).get('CN', [])),
                'us_confirmed_count': len(profile.get('confirmed', {}).get('US', [])), 'time': time.time()}

    async def signals(self):
        async with self.lock:
            if self.cache and time.monotonic() - self.refreshed < .9: return self.cache
            async def get(url):
                try:
                    async with self.session.get(url, timeout=ClientTimeout(total=.8)) as response:
                        response.raise_for_status()
                        return await response.json()
                except Exception:
                    return None
            tags, loaded, gateway = await asyncio.gather(get(self.upstream + '/api/tags'),
                get(self.upstream + '/api/ps'), get(self.proxy + '/observer-health'))
            names = lambda d: [m.get('name', m.get('model', '')) for m in (d or {}).get('models', [])
                               if not m.get('remote_host') and ':cloud' not in m.get('name', '')
                               and not m.get('name', '').endswith('-cloud')]
            active = [{**row, 'display_label': describe(row)} for row in (gateway or {}).get('active', [])]
            selected = [m['model'] for m in active] or names(loaded)
            health = {'online': tags is not None, 'models': list(dict.fromkeys(selected)),
                      'loaded': names(loaded), 'selected': None, 'busy': bool(active)}
            gate = {'primary': gateway is not None, 'local_inflight': (gateway or {}).get('local_inflight', 0),
                    'monitoring_available': bool(gateway and gateway.get('monitoring_ready'))}
            rows = await asyncio.to_thread(events.read)
            feed = [{'id': r['id'], 'label': r.get('display_label') or describe({**r, 'finished': True}),
                     'status': r['status'], 'source': r.get('chat') or r.get('tool'),
                     'time': r.get('heartbeat'), 'model': r.get('model'), 'reason': r.get('reason'), 'aliases': r.get('aliases', [])} for r in rows]
            self.cache = (health, {'time': time.time(), 'model_monitor': {'active': active,
                'backend_online': tags is not None, 'gateway': gate}, 'operation_feed': feed,
                'calls': [], 'activities': [], 'general': {'work_tasks': []}})
            self.refreshed = time.monotonic()
            return self.cache

    def app(self):
        @web.middleware
        async def local_only(request, handler):
            host = request.host.split(':')[0]
            if host not in ('localhost', '127.0.0.1'):
                return web.Response(status=403)
            response = await handler(request)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
            return response
        app = web.Application(middlewares=[local_only])
        async def lifecycle(app):
            async with ClientSession() as session:
                self.session = session
                yield
        app.cleanup_ctx.append(lifecycle)
        async def index(request): return web.FileResponse(WEB / 'index.html')
        async def workspace(request):
            try: return web.json_response(await asyncio.to_thread(self.workspace))
            except (OSError, ValueError, KeyError):
                return web.json_response({'error': '记录格式有误，请检查本地数据文件；稍后自动重试'}, status=503)
        async def health(request): return web.json_response((await self.signals())[0])
        async def state(request): return web.json_response((await self.signals())[1])
        app.router.add_get('/', index)
        app.router.add_get('/api/workspace', workspace)
        app.router.add_get('/api/health', health)
        app.router.add_get('/state', state)
        return app
