import asyncio, json
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from career_desk import events
from career_desk.model_gateway import create_app


def test_legacy_success_and_interruption_are_migrated_without_resetting(tmp_path, monkeypatch):
    monkeypatch.setattr(events, 'ROOT', tmp_path)
    (tmp_path/'recent.json').write_text(json.dumps([{'id':'old-success','model':'example:test','label':'正在处理：示例','status':'completed','heartbeat':10}]))
    (tmp_path/'last_interruption.json').write_text(json.dumps([{'id':'old-interruption','model':'example:test','status':'interrupted','heartbeat':20}]))
    rows=events.read()
    assert len(rows)==2 and rows[0]['id']=='old-interruption'
    assert '未记录' in rows[0]['reason'] and rows[1]['display_label'].startswith('已')
    # An older separate interruption must not overwrite a later outcome for the same ID.
    (tmp_path/'recent.json').write_text(json.dumps([{'id':'old-interruption','status':'completed','heartbeat':30}]))
    assert events.read()[0]['status']=='completed'


def test_same_invocation_is_saved_once_and_separate_rewrites_remain_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(events,'ROOT',tmp_path)
    base={'finished':True,'status':'completed','model':'example:test','role':'field_mapper','heartbeat':20,'label':'正在处理：{"fields":[]}'}
    events.save({**base,'id':'transport-a','model_call_id':'invocation-a'})
    events.save({**base,'id':'transport-b','model_call_id':'invocation-a','heartbeat':21})
    events.save({**base,'id':'transport-c','model_call_id':'invocation-b','heartbeat':22})
    rows=events.read()
    assert len(rows)==2 and {x['id'] for x in rows}=={'invocation-a','invocation-b'}
    assert set(rows[1]['aliases'])=={'transport-a','transport-b'}
    assert all(x['display_label']=='已匹配网页表单与已确认资料' for x in rows)
    assert len(json.loads((tmp_path/'recent.json').read_text()))==2


def test_validation_requests_have_the_same_terminal_visibility_and_causes_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(events,'ROOT',tmp_path)
    events.save({'id':'validation','finished':True,'status':'interrupted','purpose':'monitor_verification','model':'example:test','role':'writer','heartbeat':10,'error_type':'TimeoutError'})
    row=events.read()[0]
    assert row['status']=='interrupted' and '超时' in row['reason']
    assert row['display_label']=='处理中断：起草申请材料'


@pytest.mark.parametrize('case,http_status,status,reason',[
    ('http',503,'interrupted','HTTP 503'),
    ('partial',200,'interrupted','完成标记'),
    ('memory',200,'interrupted','内存不足'),
    ('encoding',200,'unknown','压缩响应'),
])
def test_proxy_records_real_failure_details_and_never_replays(tmp_path,monkeypatch,case,http_status,status,reason):
    monkeypatch.setattr(events,'ROOT',tmp_path)
    async def run():
        received=[];backend=web.Application()
        async def tags(request):return web.json_response({'models':[{'name':'example:test'}]})
        async def answer(request):
            received.append((await request.json(),request.headers.get('X-Local-Model-Call-Id')))
            if case=='http':return web.json_response({'error':'synthetic unavailable'},status=503)
            if case=='partial':return web.json_response({'response':'partial','done':False})
            if case=='memory':return web.json_response({'error':'out of memory'})
            return web.Response(body=b'{"done":true}\n',headers={'Content-Encoding':'example'})
        backend.router.add_get('/api/tags',tags);backend.router.add_post('/api/generate',answer)
        async with TestServer(backend) as upstream:
            async with TestServer(create_app(str(upstream.make_url('')).rstrip('/'))) as proxy:
                async with ClientSession(auto_decompress=False) as client:
                    async with client.post(proxy.make_url('/api/generate'),json={'model':'example:test','prompt':'synthetic request'},headers={'X-Local-Model-Call-Id':'a'*32,'X-Local-Model-Role':'writer'}) as response:
                        assert response.status==http_status
                        await response.read()
        assert len(received)==1 and received[0][1]=='a'*32
    asyncio.run(run())
    row=events.read()[0]
    assert row['id']=='a'*32 and row['status']==status and reason in row['reason']
    assert row['role']=='writer'
