"""Loopback streaming observer. Forwards once; no inference retries or option changes."""
import asyncio,json,os,re,time,uuid,gzip,zlib
from backports import zstd
from aiohttp import web,ClientSession,ClientTimeout
from urllib.parse import urlparse
from . import events as links
from .record_writer import RecordWriter
HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','host'}
INFERENCE={'/api/chat','/api/generate','/api/embed','/api/embeddings','/v1/chat/completions','/v1/completions','/v1/embeddings','/api/codex/v1/responses','/v1/responses'}

def request_label(body):
 messages=body.get('messages',body.get('input',body.get('prompt',[])))
 if isinstance(messages,str):text=messages
 else:
  text=''
  for m in reversed(messages if isinstance(messages,list) else []):
   if not isinstance(m,dict) or m.get('role')!='user':continue
   content=m.get('content','')
   if isinstance(content,list):content=' '.join(x.get('text','') for x in content if isinstance(x,dict) and x.get('type') in ('text','input_text'))
   if isinstance(content,str):text=content;break
 return '正在处理：'+(' '.join(text.split())[:140] or 'Work 交来的内容')

class Completion:
 def __init__(self):self.buffer=b'';self.done=False;self.failed=False
 def feed(self,chunk):
  self.buffer+=chunk
  while b'\n' in self.buffer:
   line,self.buffer=self.buffer.split(b'\n',1);self.line(line)
  if len(self.buffer)>262144:self.buffer=b''
 def line(self,line):
  if line.startswith(b'data:'):line=line[5:].strip()
  if line.strip()==b'[DONE]':self.done=True;return
  try:d=json.loads(line)
  except (ValueError,UnicodeError):return
  if not isinstance(d,dict):return
  if d.get('error') or d.get('type') in ('error','response.failed','response.incomplete'):self.failed=True
  if d.get('done') is True or d.get('type')=='response.completed':self.done=True
  if d.get('choices') and any(x.get('finish_reason') for x in d['choices']):self.done=True
  if d.get('status')=='completed':self.done=True
  if isinstance(d.get('embedding'),list) or isinstance(d.get('embeddings'),list):self.done=True
  if d.get('object')=='list' and isinstance(d.get('data'),list) and all(x.get('object')=='embedding' for x in d['data']):self.done=True

class ResponseCompletion:
 """Decode only an observation copy; never change client response bytes."""
 def __init__(self,encoding=''):
  self.parser=Completion();self.error=None;self.decoders=[]
  for name in reversed([x.strip().lower() for x in encoding.split(',') if x.strip() and x.strip().lower()!='identity']):
   if name not in ('gzip','deflate','zstd','zstandard'):
    self.error='unsupported_content_encoding';break
   self.decoders.append([name,self.new(name)])
 @staticmethod
 def new(name):
  return zstd.ZstdDecompressor() if name in ('zstd','zstandard') else zlib.decompressobj(31 if name=='gzip' else 15)
 def pieces(self,index,data):
  if index==len(self.decoders):
   yield data;return
  name,obj=self.decoders[index];pending=data
  while True:
   if obj.eof:
    if not pending:break
    obj=self.new(name);self.decoders[index][1]=obj
   out=obj.decompress(pending,65536)
   if out:yield from self.pieces(index+1,out)
   if obj.eof:
    pending=obj.unused_data
    if not pending:break
   elif name in ('zstd','zstandard'):
    pending=b''
    if obj.needs_input:break
   else:
    pending=obj.unconsumed_tail
    if not pending:break
 def feed(self,chunk):
  if self.error:return
  try:
   for plain in self.pieces(0,chunk):self.parser.feed(plain)
  except (ValueError,RuntimeError,zstd.ZstdError,zlib.error):self.error='response_decode_failed'
 def finish(self):
  if self.error:return False
  if any(not obj.eof for _,obj in self.decoders):return False
  self.parser.line(self.parser.buffer)
  return self.parser.done and not self.parser.failed

def create_app(upstream='http://127.0.0.1:11434',primary=False,observe_requests=True):
 app=web.Application(client_max_size=64*1024**2,handler_args={'auto_decompress':False});state={'models':(0,set()),'inflight':0,'local_inflight':0}
 async def lifecycle(app):
  writer=RecordWriter(links.save);state['recorder']=writer;state['snapshots']={}
  try:
   async with ClientSession(auto_decompress=False,timeout=ClientTimeout(total=None,sock_connect=10)) as session:
    state['session']=session;yield
  finally:await asyncio.to_thread(writer.close,.15)
 app.cleanup_ctx.append(lifecycle)
 async def health(r):return web.json_response({'service':'local-model-observer','inflight':state['inflight'],'local_inflight':state['local_inflight'],'recorder':state['recorder'].status(),'monitoring_ready':state['recorder'].status()['healthy'],'primary':primary,'time':time.time(),'upstream':upstream,'active':[dict(x) for x in state['snapshots'].values()]})
 async def local_names():
  if time.time()-state['models'][0]>15:
   try:
    async with state['session'].get(upstream+'/api/tags',timeout=ClientTimeout(total=1)) as r:
     data=await r.json();names={m['name'] for m in data.get('models',[]) if not m.get('remote_host') and ':cloud' not in m['name'] and not m['name'].endswith('-cloud')}
     state['models']=(time.time(),names)
   except Exception:return state['models'][1]
  return state['models'][1]
 async def relay(request):
  origin=request.headers.get('Origin')
  if origin and urlparse(origin).hostname not in ('127.0.0.1','localhost','::1'):return web.Response(status=403)
  raw=await request.read();body={};span=None;beat=None;finished=False
  encoding=request.headers.get('Content-Encoding','').lower();decoded=raw
  try:
   if encoding in ('zstd','zstandard'):decoded=zstd.decompress(raw)
   elif encoding=='gzip':decoded=gzip.decompress(raw)
   elif encoding=='deflate':decoded=zlib.decompress(raw)
  except (ValueError,RuntimeError,zstd.ZstdError,zlib.error,OSError):return web.json_response({'error':'Invalid compressed request'},status=400)
  transformed=encoding in ('zstd','zstandard','gzip','deflate')
  if observe_requests and request.method=='POST' and request.path in INFERENCE:
   try:body=json.loads(decoded)
   except (ValueError,UnicodeError):pass
   if isinstance(body,dict) and body.get('model') in await local_names():
    tid=next((request.headers.get(h) for h in ('x-codex-thread-id','session_id','x-session-id') if re.fullmatch(r'[0-9a-f-]{36}',request.headers.get(h,''))),None)
    span={'id':uuid.uuid4().hex,'kind':'model_gateway','thread_id':tid,'tool':'Work → Ollama' if tid or 'codex' in request.headers.get('User-Agent','').lower() else 'Ollama 客户端 → 本地模型' if 'ollama' in request.headers.get('User-Agent','').lower() else '本机程序 → Ollama','label':request_label(body),'role':'model','model':body['model'],'pid':os.getpid(),'started':time.time(),'heartbeat':time.time(),'status':'waiting','finished':False,'association':'explicit_request' if tid else 'caller_unavailable'}
    span['chat']=request.headers.get('X-Chat-Name','')[:100]
    try:span['chat']=json.loads(chr(34)+span['chat']+chr(34))
    except ValueError:pass
    if request.headers.get('X-Observer-Purpose')=='monitor_verification':span['purpose']='monitor_verification'
    state['snapshots'][span['id']]=span;state['recorder'].submit(span);state['local_inflight']+=1
    async def pulse():
     while True:await asyncio.sleep(2);span['heartbeat']=time.time();state['recorder'].submit(span)
    beat=asyncio.create_task(pulse())
  state['inflight']+=1
  parser=None;response=None;monitor_error=None
  try:
   headers={k:v for k,v in request.headers.items() if k.lower() not in HOP and (not k.lower().startswith('x-observer-') or not observe_requests)}
   if transformed:headers={k:v for k,v in headers.items() if k.lower() not in ('content-encoding','content-length')}
   async with state['session'].request(request.method,upstream+request.rel_url.raw_path_qs,headers=headers,data=decoded,allow_redirects=False) as upstream_response:
    parser=ResponseCompletion(upstream_response.headers.get('Content-Encoding',''))
    response=web.StreamResponse(status=upstream_response.status,headers={k:v for k,v in upstream_response.headers.items() if k.lower() not in HOP})
    await response.prepare(request)
    async for chunk in upstream_response.content.iter_any():
     await response.write(chunk)
     if span:
      parser.feed(chunk);span.update(status='generating',progress_at=time.time())
    finished=200<=upstream_response.status<300 and parser.finish()
    monitor_error=parser.error
    await response.write_eof();return response
  except (ConnectionError,asyncio.CancelledError):raise
  except Exception:
   if response and response.prepared:request.transport.close() if request.transport else None;return response
   return web.json_response({'error':'Ollama connection unavailable; request was not retried'},status=502)
  finally:
   state['inflight']-=1
   if beat:beat.cancel()
   if span:
    state['local_inflight']-=1
    state['snapshots'].pop(span['id'],None)
    if monitor_error:span['monitor_error']=monitor_error
    span.update(finished=True,status='completed' if finished else 'unknown' if monitor_error else 'interrupted',heartbeat=time.time());state['recorder'].submit(span)
   if beat:await asyncio.gather(beat,return_exceptions=True)
 app.router.add_get('/observer-health',health)
 app.router.add_route('*','/{path:.*}',relay)
 return app
