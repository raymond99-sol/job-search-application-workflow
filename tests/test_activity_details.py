import asyncio,json

from career_desk.activity_details import coalesce_model_records,interruption_reason,exception_details



def pair():
 owner={'id':'owner','role':'field_mapper','model':'local:test','thread_id':'chat','local':True,'started':10,'heartbeat':20.03,'finished':True,'status':'completed'}
 transport={'id':'transport','kind':'model_gateway','model':'local:test','thread_id':'chat','local':True,'started':10.04,'heartbeat':20,'finished':True,'status':'completed','label':'正在处理：{"fields": []}'}
 return owner,transport

def test_one_real_request_is_one_detailed_record():
 rows=coalesce_model_records(list(pair()))
 assert len(rows)==1 and rows[0]['id']=='owner' and rows[0]['aliases']==['transport']
 assert rows[0]['display_label']=='已匹配网页表单与已确认资料'
 assert len(rows)==1

def test_two_real_requests_with_identical_labels_and_near_times_remain_distinct():
 one,two=pair();two={**one,'id':'second','heartbeat':20.04,'started':10.01}
 assert len(coalesce_model_records([one,two]))==2

def test_ambiguous_concurrent_callers_are_not_merged():
 owner,gateway=pair();other={**owner,'id':'other','started':10.01}
 assert len(coalesce_model_records([owner,other,gateway]))==3
 other={**gateway,'id':'second-transport','started':10.05}
 assert len(coalesce_model_records([owner,gateway,other]))==3

def test_explicit_request_identity_overrides_timing_and_active_label_is_specific():
 owner,gateway=pair();gateway.update(model_call_id='owner',started=12)
 owner.update(finished=False,status='generating')
 result=coalesce_model_records([owner,gateway])
 assert len(result)==1 and result[0]['correlation']=='explicit'
 assert result[0]['display_label']=='正在匹配网页表单与已确认资料'

def test_company_role_and_work_type_are_preserved():
 owner,gateway=pair();owner['role']='targeted_prose_repair'
 gateway['label']='正在处理：{"company":"Example","role":"Analyst","job_requirements":{}}'
 row=coalesce_model_records([owner,gateway])[0]
 assert row['display_label']=='已修改申请材料中需要调整的段落 · Example · Analyst'

def test_failure_reason_is_factual_and_unknown_is_explicit():
 assert '未记录' in interruption_reason({'status':'interrupted'})
 assert '调用程序取消' in interruption_reason({'status':'interrupted','error_type':'CancelledError'})
 assert '超时' in interruption_reason({'status':'interrupted','error_type':'TimeoutError'})
 assert '格式' in interruption_reason({'status':'interrupted','error_type':'ValidationError'})
 assert '长度限制' in interruption_reason({'status':'interrupted','error_type':'ValueError','error_message':'Model response truncated; do not use partial output'})
 assert '连接中断' in interruption_reason({'status':'interrupted','error_type':'ClientConnectionError'})
 assert '监测' in interruption_reason({'status':'unknown','monitor_error':'response_decode_failed'})
 assert interruption_reason({'status':'completed','error_type':'TimeoutError'}) is None
 assert 'sensitive-example' not in exception_details(ValueError('token=sensitive-example'))['error_message']



def test_cancelled_transport_close_can_lag_without_duplicating_the_call():
 owner,gateway=pair();owner.update(status='interrupted',error_type='CancelledError')
 gateway.update(status='interrupted',heartbeat=30)
 result=coalesce_model_records([owner,gateway])
 assert len(result)==1 and '调用程序取消' in result[0]['reason']
