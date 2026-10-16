import asyncio,json
from aiohttp import web,ClientSession
from aiohttp.test_utils import TestServer
from career_desk import events as links
from career_desk.model_gateway import create_app,Completion

def test_completion_requires_finish_and_handles_fragmented_stream():
 p=Completion();p.feed(b'data: {"type":"response.output_text.delta","delta":"hi"}\n');assert not p.done
 p.feed(b'data: {"type":"response.com');p.feed(b'pleted"}\n');assert p.done
 p=Completion();p.feed(b'{"error":"failed"}\n');assert p.failed and not p.done

def test_proxy_forwards_once_preserves_body_and_ignores_cloud(tmp_path,monkeypatch):
 monkeypatch.setattr(links,'ROOT',tmp_path)
 async def run():
  received=[];up=web.Application()
  async def tags(r):return web.json_response({'models':[{'name':'gemma:test'}]})
  async def answer(r):
   raw=await r.read();received.append((raw,r.headers.get('Authorization')))
   data=json.loads(raw)
   if data.get('stream') is False:return web.json_response({'response':'hello','done':True})
   resp=web.StreamResponse(headers={'Content-Type':'application/x-ndjson'});await resp.prepare(r)
   await resp.write(b'{"response":"hi","done":false}\n');await asyncio.sleep(.01)
   await resp.write(b'{"done":true}\n');await resp.write_eof();return resp
  up.router.add_get('/api/tags',tags);up.router.add_post('/api/chat',answer)
  async with TestServer(up) as server:
   async with TestServer(create_app(str(server.make_url('')).rstrip('/'))) as gateway:
    async with ClientSession() as c:
     for model in ('gemma:test','cloud:cloud'):
      body=json.dumps({'model':model,'messages':[{'role':'user','content':'请总结一段内容'}],'stream':True}).encode()
      async with c.post(gateway.make_url('/api/chat'),data=body,headers={'Authorization':'Bearer secret','session_id':'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}) as r:
       assert r.status==200;assert (await r.read()).endswith(b'{"done":true}\n')
      assert received[-1]==(body,'Bearer secret')
  records=links.read()
  assert len(records)==1 and records[0]['status']=='completed'
  assert records[0]['tool']=='Work → Ollama'
  assert '请总结一段内容' in records[0]['label']
  assert 'secret' not in json.dumps(records)
  assert len(received)==2
 asyncio.run(run())

def test_zstd_requests_are_forwarded_without_double_decoding(tmp_path,monkeypatch):
 from backports import zstd
 monkeypatch.setattr(links,'ROOT',tmp_path)
 async def run():
  up=web.Application(handler_args={'auto_decompress':False});seen=[]
  async def tags(r):return web.json_response({'models':[{'name':'gemma:test'}]})
  async def answer(r):
   raw=await r.read();seen.append((raw,r.headers.get('Content-Encoding'),r.headers.get('Content-Length')))
   assert json.loads(raw)['model']=='gemma:test'
   return web.json_response({'done':True,'response':'5'})
  up.router.add_get('/api/tags',tags);up.router.add_post('/api/generate',answer)
  async with TestServer(up) as server:
   async with TestServer(create_app(str(server.make_url('')).rstrip('/'))) as gateway:
    async with ClientSession() as c:
     raw=zstd.compress(json.dumps({'model':'gemma:test','prompt':'2+3','stream':False}).encode())
     async with c.post(gateway.make_url('/api/generate'),data=raw,headers={'Content-Encoding':'zstd'}) as r:
      assert r.status==200;assert (await r.json())['response']=='5'
     plain=zstd.decompress(raw)
     assert seen==[(plain,None,str(len(plain)))]
  records=links.read()
  assert len(records)==1 and records[0]['status']=='completed'
 asyncio.run(run())
