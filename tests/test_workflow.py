"""Reproduce the documented workflow without a real model or personal data."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from career_desk import draft, events, records
from career_desk.model_gateway import create_app
from career_desk.server import Dashboard


def command(data, *args, success=True):
    result = subprocess.run([sys.executable, '-m', 'career_desk.records', '--data', str(data), *args],
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is success, result.stderr
    return result


def test_documented_record_and_confirmation_workflow(tmp_path):
    data = tmp_path / 'personal'
    command(data, 'init')
    dashboard = Dashboard(data, '', '')
    assert dashboard.workspace()['domestic']['summary']['total'] == 0
    base = ('--id', 'cn-job-001', '--market', 'CN', '--company', '示例企业', '--role', '数据分析师')
    command(data, 'application', *base, '--status', 'attention', '--note', '材料待审核')
    command(data, 'question', '--id', 'q-start', '--market', 'CN', '--company', '示例企业',
            '--role', '数据分析师', '--text', '请确认可到岗日期')
    command(data, 'question', '--id', 'q-start', '--market', 'CN', '--company', '示例企业',
            '--role', '数据分析师', '--text', '请确认可到岗日期')
    waiting = dashboard.workspace()
    assert len(waiting['pending']) == 1 and waiting['domestic']['summary']['attention'] == 1
    command(data, 'answer', '--id', 'q-start', '--text', '示例：下月可以到岗')
    confirmed = dashboard.workspace()
    assert confirmed['pending'] == [] and confirmed['facts_count'] == 1 and confirmed['confirmed_count'] == 1
    command(data, 'answer', '--id', 'q-start', '--text', '不会重复记录', success=False)
    assert dashboard.workspace()['facts_count'] == 1
    # A draft is not proof of submission; a bare submitted flag is rejected.
    command(data, 'application', *base, '--status', 'submitted', success=False)
    assert dashboard.workspace()['domestic']['summary']['attention'] == 1
    command(data, 'application', *base, '--status', 'submitted', '--note', '隔离测试：模拟本人确认成功，不代表真实申请')
    finished = dashboard.workspace()
    assert finished['domestic']['summary'] == {'total': 1, 'submitted': 1, 'attention': 0, 'excluded': 0}
    command(data, 'application', '--id', 'us-job-001', '--market', 'US', '--company', 'Example Company',
            '--role', 'Analyst', '--status', 'excluded', '--note', '示例资格不符')
    assert dashboard.workspace()['us']['summary']['excluded'] == 1
    before = (data / 'profile.json').read_bytes()
    command(data, 'init', success=False)
    assert (data / 'profile.json').read_bytes() == before
    assert not (data / '.records-lock').exists()


def test_confirmation_failure_keeps_question_visible_and_retry_deduplicates(tmp_path, monkeypatch):
    data = tmp_path / 'personal'
    command(data, 'init')
    command(data, 'question', '--id', 'q1', '--market', 'US', '--company', 'Example', '--role', 'Analyst', '--text', 'Example question')
    args = argparse.Namespace(data=str(data), command='answer', id='q1', text='Example confirmed answer')
    original = records.save_json
    def failed_pending(path, value):
        if path.name == 'pending.json': raise OSError('synthetic write failure')
        original(path, value)
    monkeypatch.setattr(records, 'save_json', failed_pending)
    with pytest.raises(OSError): records.execute(args)
    assert not json.loads((data / 'pending.json').read_text())[0]['resolved']
    monkeypatch.setattr(records, 'save_json', original)
    records.execute(args)
    profile = json.loads((data / 'profile.json').read_text())
    assert len(profile['facts']) == 1 and len(profile['confirmed']['US']) == 1


def test_real_draft_entry_through_proxy_busy_completed_and_output(tmp_path, monkeypatch):
    data = tmp_path / 'personal'
    command(data, 'init')
    job = data / 'inputs/job-001.txt'
    job.write_text('示例岗位：整理销售数据，不代表真实招聘。')
    output = data / 'drafts/job-001.txt'
    monkeypatch.setattr(events, 'ROOT', data)
    async def run():
        received = []
        release = asyncio.Event()
        backend = web.Application()
        async def tags(request): return web.json_response({'models': [{'name': 'example-local:test'}]})
        async def answer(request):
            received.append(await request.json())
            await release.wait()
            return web.json_response({'message': {'content': '示例草稿：资料尚未确认，不编造经历。'}, 'done': True})
        backend.router.add_get('/api/tags', tags)
        backend.router.add_post('/api/chat', answer)
        async with TestServer(backend) as upstream:
            async with TestServer(create_app(str(upstream.make_url('')).rstrip('/'))) as proxy:
                args = argparse.Namespace(profile=str(data / 'profile.json'), jd=str(job), output=str(output),
                    model='example-local:test', chat='求职流程', endpoint=str(proxy.make_url('')).rstrip('/'))
                task = asyncio.create_task(draft.run(args))
                try:
                    async with ClientSession() as client:
                        for _ in range(100):
                            async with client.get(proxy.make_url('/observer-health')) as response: health = await response.json()
                            if health['local_inflight']: break
                            await asyncio.sleep(.01)
                        assert health['local_inflight'] == 1
                        release.set()
                        await task
                        async with client.get(proxy.make_url('/observer-health')) as response: health = await response.json()
                        assert health['local_inflight'] == 0
                finally:
                    release.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert len(received) == 1
        assert received[0]['model'] == 'example-local:test'
        assert '只输出草稿' in received[0]['messages'][0]['content']
    asyncio.run(run())
    assert '不编造经历' in output.read_text()
    completed = events.read()
    assert len(completed) == 1 and completed[0]['status'] == 'completed'
    assert completed[0]['chat'] == '求职流程'
