"""Pure display metadata: no model calls, task controls, network or disk writes."""
import re

ACTIONS = {
 'writer':'起草申请材料', 'online_application_writer':'撰写网申填写内容',
 'analyst':'分析岗位要求', 'independent_reviewer':'审核申请材料',
 'chinese_independent_reviewer':'审核中文申请材料', 'planner':'整理本次任务的处理步骤',
 'field_mapper':'匹配网页表单与已确认资料', 'field_repair':'修改申请问题的回答',
 'targeted_prose_repair':'修改申请材料中需要调整的段落',
 'browser':'分析网页内容', 'summarizer':'整理会议记录', 'verifier':'检查会议待办',
}
TERMINAL={'completed','interrupted','cancelled','failed','unknown'}

def safe_detail(value):
 text=' '.join(str(value or '').split())
 text=re.sub(r'(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)\S+',r'\1[已隐藏]',text)
 text=re.sub(r'(?:/Users/|/home/)[^\s:]+','[本地路径]',text)
 text=re.sub(r'https?://[^\s]+','[服务地址]',text)
 return text[:240]

def exception_details(error):
 """Capture facts only; never describe a generic cancellation as a user click."""
 return {'error_type':type(error).__name__, 'error_message':safe_detail(error),
         **({'http_status':error.status} if isinstance(getattr(error,'status',None),int) else {})}

def interruption_reason(row):
 if row.get('status')=='completed':return None
 if row.get('reason_code')=='client_disconnected':return '调用方连接已断开，未能交回完整结果'
 if row.get('reason_code')=='model_reported_failure':return '模型服务报告处理失败，但未提供详细错误'
 if row.get('reason_code')=='missing_completion':return '模型响应提前结束，未收到正常完成标记'
 kind=str(row.get('error_type') or '')
 message=safe_detail(row.get('error_message') or row.get('error'))
 low=(kind+' '+message).lower()
 if 'out of memory' in low or 'insufficient memory' in low:return '本地模型可用内存不足，处理未完成'
 if 'cancelled' in low or row.get('reason_code')=='caller_cancelled':return '调用程序取消了这次处理'
 if row.get('http_status'):return '模型服务返回错误（HTTP '+str(row['http_status'])+'）'
 if 'timeout' in low or 'timed out' in low:return '等待模型响应超时'
 if any(x in low for x in ('truncated','token limit','length limit')):return '模型输出达到长度限制，结果不完整'
 if 'jsondecode' in low:return '模型返回的内容不是有效的 JSON，调用程序无法读取'
 if 'validationerror' in low:return '模型返回的内容不符合本次任务要求的格式'
 if any(x in low for x in ('connection','disconnect','brokenpipe','reset by peer','payloaderror')):return '与本地模型的连接中断，未能完整接收结果'
 if 'stream ended before completion' in low:return '模型响应提前结束，未收到正常完成标记'
 if row.get('monitor_error')=='unsupported_content_encoding':return '监测无法读取这种压缩响应，不能确认模型是否正常完成'
 if row.get('monitor_error')=='response_decode_failed':return '监测读取压缩响应失败，不能确认模型是否正常完成'
 if message:return message
 return '当时未记录具体原因，无法判断是取消、连接问题还是其他异常'

def payload_context(label):
 result={}
 for source,target in (('company','company'),('role','job_role')):
  match=re.search(r'"'+source+r'"\s*:\s*"((?:[^"\\]|\\.)*)"',str(label or ''))
  if match:
   import json
   try:result[target]=json.loads('"'+match[1]+'"')[:100]
   except ValueError:pass
 return result

def action_text(row):
 role=row.get('role')
 if role in ACTIONS:return ACTIONS[role]
 text=str(row.get('label') or '')
 if re.search(r'^正在处理：\s*\{',text):
  # Match the actual payload, not an instruction or an imagined next step.
  if '"fields"' in text:return '处理网页表单填写项'
  if '"question"' in text or '"current_answer"' in text:return '处理申请问题的回答'
  if '"draft"' in text or '"sentences"' in text:return '处理申请材料'
  if '"job_requirements"' in text or '"jd"' in text:return '分析岗位要求'
  if '"company"' in text and '"role"' in text:return '整理岗位资料'
  if '"reusable_lessons"' in text:return '整理任务经验资料'
  return '处理模型收到的内容'
 if text.startswith('正在处理：'):return '处理：'+text[len('正在处理：'):]
 return text or '完成一次本地模型处理（调用方未提供具体动作）'

def describe(row):
 text=action_text(row)
 context=payload_context(row.get('label'))
 context.update({k:row[k] for k in ('company','job_role') if row.get(k)})
 target=' · '.join(context[k] for k in ('company','job_role') if context.get(k))
 if not target:
  title=str(row.get('task_title') or '')
  if ' · ' in title and not re.search(r'测试|验收|验证|检查点',title):target=title[:120]
 if target:text+=' · '+target
 status=row.get('status')
 if not row.get('finished'):return '正在'+text
 if status=='completed':return '已'+text
 if status in ('interrupted','cancelled','failed'):return '处理中断：'+text
 return '处理结束，结果未确认：'+text

def coalesce_model_records(rows):
 """Prefer one instrumented call over its transport mirror, never merge by label.

 Explicit IDs win. Legacy mirrors require same local model + caller, matching
 start/end boundaries and a unique one-to-one candidate. Ambiguous concurrency
 is retained instead of being silently merged. Inference is display-only.
 """
 rows=[dict(row) for row in rows]
 internals=[r for r in rows if r.get('kind')!='model_gateway']
 gateways=[r for r in rows if r.get('kind')=='model_gateway']
 candidates={}
 for gateway in gateways:
  explicit=gateway.get('model_call_id')
  owners=[r for r in internals if explicit and r['id']==explicit]
  if not owners:
   for owner in internals:
    if owner.get('role') not in ACTIONS or not owner.get('thread_id') or owner.get('thread_id')!=gateway.get('thread_id') or owner.get('model')!=gateway.get('model'):continue
    delta=gateway.get('started',0)-owner.get('started',0)
    if not 0<=delta<=.25:continue
    if bool(gateway.get('finished'))!=bool(owner.get('finished')):continue
    if owner.get('finished'):
     # A cancelled caller can close before the transport notices a disconnected stream.
     limit=15 if owner.get('status') in ('interrupted','cancelled') and gateway.get('status') in ('interrupted','cancelled') else 1
     if abs(gateway.get('heartbeat',0)-owner.get('heartbeat',0))>limit:continue
    owners.append(owner)
  if len(owners)==1:candidates[gateway['id']]=owners[0]['id']
 reverse={}
 for gateway_id,owner_id in candidates.items():reverse.setdefault(owner_id,[]).append(gateway_id)
 removed=set()
 for gateway in gateways:
  owner_id=candidates.get(gateway['id'])
  if not owner_id or len(reverse.get(owner_id,[]))!=1:continue
  owner=next(r for r in internals if r['id']==owner_id)
  owner['aliases']=list(dict.fromkeys([*owner.get('aliases',[]),gateway['id']]))[:8]
  owner['transport_id']=gateway['id']
  owner['correlation']='explicit' if gateway.get('model_call_id')==owner_id else 'unique_legacy_boundaries'
  if gateway.get('label'):owner['request_label']=gateway['label'];owner.update(payload_context(gateway['label']))
  for key in ('error_type','error_message','monitor_error','http_status'):
   if gateway.get(key) and not owner.get(key):owner[key]=gateway[key]
  removed.add(gateway['id'])
 for row in rows:
  if row.get('request_label') and not row.get('label'):row['label']=row['request_label']
  row['display_label']=describe(row)
  if row.get('finished') and row.get('status')!='completed':row['reason']=interruption_reason(row)
 return [row for row in rows if row['id'] not in removed]
