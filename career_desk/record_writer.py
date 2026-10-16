"""Bounded, best-effort disk recorder. Request handlers never wait for disk IO."""
from collections import OrderedDict
import threading,time

class RecordWriter:
 def __init__(self,write,capacity=128,deadline=1.0):
  self.write=write;self.capacity=capacity;self.deadline=deadline
  self.pending=OrderedDict();self.lock=threading.Lock();self.wake=threading.Event();self.stop=False
  self.started=0.;self.failed=False;self.dropped=0;self.saved=0
  self.thread=threading.Thread(target=self.run,name='model-record-writer',daemon=True);self.thread.start()
 def submit(self,data):
  # Never acquire a mutex held during IO. Contention drops telemetry, not results.
  if not self.lock.acquire(False):self.dropped+=1;return False
  try:
   key=data['id']
   if key not in self.pending and len(self.pending)>=self.capacity:self.pending.popitem(last=False);self.dropped+=1
   self.pending[key]=dict(data)
  finally:self.lock.release()
  self.wake.set();return True
 def run(self):
  while not self.stop:
   self.wake.wait(.5);self.wake.clear()
   while not self.stop:
    with self.lock:
     if not self.pending:break
     key,item=self.pending.popitem(last=False)
    self.started=time.monotonic()
    try:success=self.write(item) is not False
    except Exception:success=False
    self.started=0.;self.failed=not success
    if success:self.saved+=1
    else:
     # Retry telemetry only, and keep a newer completion snapshot over old beats.
     with self.lock:
      if key not in self.pending:
       if len(self.pending)>=self.capacity:self.pending.popitem(last=False);self.dropped+=1
       self.pending[key]=item
     self.wake.wait(.5);self.wake.clear();break
 def status(self):
  age=time.monotonic()-self.started if self.started else 0.
  return {'healthy':not self.failed and age<self.deadline,'stalled':age>=self.deadline,'pending':len(self.pending),'dropped':self.dropped,'saved':self.saved}
 def close(self,timeout=.15):
  # Best effort drain for a clean stop, with a strict time budget even on hung IO.
  until=time.monotonic()+timeout
  while time.monotonic()<until and (self.pending or self.started):self.wake.set();time.sleep(.005)
  self.stop=True;self.wake.set();self.thread.join(max(0,until-time.monotonic()))
