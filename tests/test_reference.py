import asyncio, gzip, json, shutil, threading, time, zlib
from pathlib import Path
import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from backports import zstd
from career_desk import events
from career_desk.model_gateway import create_app, ResponseCompletion
from career_desk.record_writer import RecordWriter
from career_desk.server import Dashboard

@pytest.mark.parametrize('encoding,encode', [('gzip', gzip.compress), ('deflate', zlib.compress), ('zstd', zstd.compress)])
def test_response_completion_observes_fragmented_compressed_bytes(encoding, encode):
    raw = encode(b'{"response":"hello","done":false}\n{"done":true}\n')
    parser = ResponseCompletion(encoding)
    for offset in range(0, len(raw), 3): parser.feed(raw[offset:offset+3])
    assert parser.finish()
    truncated = ResponseCompletion(encoding)
    truncated.feed(raw[:len(raw)//2])
    assert not truncated.finish()


def test_recent_archive_is_bounded_and_persists_after_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(events, 'ROOT', tmp_path)
    for i in range(40):
        events.save({'id': str(i), 'finished': True, 'status': 'completed', 'heartbeat': i, 'label': 'x'})
    rows = events.read()
    assert len(rows) == 25 and rows[0]['id'] == '39' and rows[-1]['id'] == '15'
    assert json.loads((tmp_path / 'recent.json').read_text()) == rows
    assert not list(tmp_path.glob('*.tmp'))


def test_workspace_counts_and_pending_follow_file_changes(tmp_path):
    examples = Path(__file__).resolve().parents[1] / 'examples'
    for source in examples.iterdir(): shutil.copy(source, tmp_path / source.name)
    dashboard = Dashboard(tmp_path, '', '')
    first = dashboard.workspace()
    assert first['domestic']['summary'] == {'total': 3, 'submitted': 1, 'attention': 1, 'excluded': 1}
    assert first['us']['summary']['total'] == 2 and len(first['pending']) == 1
    with (tmp_path / 'applications.csv').open('a') as out:
        out.write('cn-004,CN,示例新公司,分析师,CN-004,submitted,2026-01-14,北京,,新增示例\n')
    pending = json.loads((tmp_path / 'pending.json').read_text())
    pending[0]['resolved'] = True
    (tmp_path / 'pending.json').write_text(json.dumps(pending))
    (tmp_path / 'profile.json').write_text(json.dumps({'facts': ['one', 'two'], 'confirmed': {}}))
    updated = dashboard.workspace()
    assert updated['domestic']['summary']['total'] == 4
    assert updated['domestic']['summary']['submitted'] == 2
    assert updated['pending'] == [] and updated['facts_count'] == 2


def test_blocked_disk_writer_does_not_block_submission():
    release = threading.Event()
    writer = RecordWriter(lambda _: release.wait(2), capacity=2, deadline=.01)
    try:
        writer.submit({'id': 'first'})
        time.sleep(.03)
        started = time.monotonic()
        for i in range(30): writer.submit({'id': str(i)})
        assert time.monotonic() - started < .1
        assert writer.status()['stalled'] and writer.status()['dropped'] > 0
    finally:
        release.set()
        writer.close(.2)


def test_busy_complete_cancel_failure_and_proxy_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(events, 'ROOT', tmp_path)
    observed = []
    original_save = events.save
    def save(item):
        observed.append(dict(item))
        return original_save(item)
    monkeypatch.setattr(events, 'save', save)
    async def run():
        release = asyncio.Event()
        received = []
        upstream = web.Application()
        async def tags(request): return web.json_response({'models': [{'name': 'qwen:test'}]})
        async def generate(request):
            body = await request.json()
            received.append(body['prompt'])
            if body['prompt'] == 'cancel':
                response = web.StreamResponse()
                await response.prepare(request)
                await response.write(b'{"response":"start","done":false}\n')
                for _ in range(100):
                    await asyncio.sleep(.02)
                    try: await response.write(b'{"response":"more","done":false}\n')
                    except (ConnectionError, RuntimeError): break
                return response
            if body['prompt'] == 'fail': return web.json_response({'error': 'example failure'}, status=500)
            if body['prompt'] == 'wait': await release.wait()
            return web.Response(body=zstd.compress(b'{"done":true,"response":"OK"}\n'),
                                headers={'Content-Encoding': 'zstd', 'Content-Type': 'application/x-ndjson'})
        upstream.router.add_get('/api/tags', tags)
        upstream.router.add_post('/api/generate', generate)
        async with TestServer(upstream) as backend:
            upstream_url = str(backend.make_url('')).rstrip('/')
            async with ClientSession() as client:
                async with TestServer(create_app(upstream_url)) as proxy:
                    endpoint = proxy.make_url('/api/generate')
                    async def request(prompt):
                        async with client.post(endpoint, json={'model': 'qwen:test', 'prompt': prompt}) as response:
                            return response.status, await response.read()
                    job = asyncio.create_task(request('wait'))
                    for _ in range(100):
                        async with client.get(proxy.make_url('/observer-health')) as response: health = await response.json()
                        if health['local_inflight']: break
                        await asyncio.sleep(.01)
                    assert health['local_inflight'] == 1 and health['active'][0]['model'] == 'qwen:test'
                    release.set()
                    assert (await job)[0] == 200
                    async with client.get(proxy.make_url('/observer-health')) as response: health = await response.json()
                    assert health['local_inflight'] == 0 and health['active'] == []
                    # Client cancellation is never reported as successful completion.
                    response = await client.post(endpoint, json={'model': 'qwen:test', 'prompt': 'cancel'})
                    await response.content.read(1)
                    response.close()
                    for _ in range(100):
                        await asyncio.sleep(.01)
                        async with client.get(proxy.make_url('/observer-health')) as response: health = await response.json()
                        if not health['local_inflight']: break
                    assert health['local_inflight'] == 0
                    assert (await request('fail'))[0] == 500
                # Proxy stopping does not stop or take over the upstream.
                async with client.post(backend.make_url('/api/generate'), json={'model': 'qwen:test', 'prompt': 'direct'}) as response:
                    assert response.status == 200 and b'OK' in await response.read()
                async with TestServer(create_app(upstream_url)) as recovered:
                    async with client.post(recovered.make_url('/api/generate'), json={'model': 'qwen:test', 'prompt': 'recovered'}) as response:
                        assert response.status == 200
            assert received == ['wait', 'cancel', 'fail', 'direct', 'recovered']
    asyncio.run(run())
    finals = {r['label']: r['status'] for r in observed if r.get('finished')}
    assert finals['正在处理：wait'] == 'completed'
    assert finals['正在处理：cancel'] == 'interrupted'
    assert finals['正在处理：fail'] == 'interrupted'
    assert finals['正在处理：recovered'] == 'completed'


def test_failed_recording_still_returns_model_answer(monkeypatch):
    def broken(_): raise PermissionError('synthetic failure')
    monkeypatch.setattr(events, 'save', broken)
    async def run():
        upstream = web.Application()
        received = []
        async def tags(request): return web.json_response({'models': [{'name': 'qwen:test'}]})
        async def answer(request):
            received.append(await request.read())
            return web.json_response({'done': True, 'response': 'OK'})
        upstream.router.add_get('/api/tags', tags)
        upstream.router.add_post('/api/generate', answer)
        async with TestServer(upstream) as backend:
            async with TestServer(create_app(str(backend.make_url('')).rstrip('/'))) as proxy:
                async with ClientSession() as client:
                    async with client.post(proxy.make_url('/api/generate'), json={'model': 'qwen:test', 'prompt': 'hello'}) as response:
                        assert response.status == 200 and (await response.json())['response'] == 'OK'
                assert len(received) == 1
    asyncio.run(run())
