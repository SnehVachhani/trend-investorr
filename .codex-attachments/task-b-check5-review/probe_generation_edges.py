from pathlib import Path
import sys,json,threading,time
p=Path(__file__).resolve().parent;m=json.loads((p/'workspace.json').read_text());sys.path.insert(0,m['workspace'])
from loguru import logger
logger.remove()
from data.streams.websocket_manager import WebSocketManager
from tests.data.ws_fakes import FakeSession,FakeAudit,FakeSmartWebSocketV2,wait_until
out=[]
class LateSocket:
 def __init__(self,delayed=False):
  self.delayed=delayed;self.before_app=threading.Event();self.proceed=threading.Event();self.closed=threading.Event();self.opened=threading.Event();self.wsapp=None;self.live=False;self.close_requests=0
 def connect(self):
  self.before_app.set()
  if self.delayed:assert self.proceed.wait(5)
  self.wsapp=object();self.live=True;self.on_open(self.wsapp);self.opened.set()
  self.closed.wait(5);self.live=False;self.on_close(self.wsapp)
 def close_connection(self):
  self.close_requests+=1
  if self.wsapp is not None:self.closed.set()
 def subscribe(self,**kw):pass
 def unsubscribe(self,**kw):pass
clients=[]
def create(**kw):
 s=LateSocket(delayed=not clients);clients.append(s);return s
mgr=WebSocketManager(FakeSession(),FakeAudit());mgr.STOP_JOIN_TIMEOUT=0.01;mgr._create_sdk_client=create;mgr.subscribe(['2885']);old_thread=None
try:
 mgr.start();assert wait_until(lambda:len(clients)==1 and clients[0].before_app.is_set());old_thread=mgr._ws_thread
 mgr.stop();mgr.start();assert wait_until(lambda:len(clients)==2 and mgr.is_connected and mgr.sent_count==1)
 clients[0].proceed.set();assert clients[0].opened.wait(2)
 out.append({'case':'old connect creates WebSocketApp only after stop returned and replacement started','live_connections':sum(s.live for s in clients),'old_thread_alive':old_thread.is_alive(),'old_close_requests':clients[0].close_requests,'ignored_old_callbacks':mgr.stats['stale_callbacks_ignored']})
finally:
 for s in clients:s.proceed.set();s.close_connection()
 mgr.stop()
 if old_thread:old_thread.join(2)

FakeSmartWebSocketV2.reset(['stay_open','stay_open'])
mgr=WebSocketManager(FakeSession(),FakeAudit());mgr._create_sdk_client=lambda **kw:FakeSmartWebSocketV2(**kw);mgr._backoff_wait=lambda *args:None;mgr.subscribe(['2885']);seen=[];mgr.on_tick(seen.append)
parse_entered=threading.Event();parse_resume=threading.Event();original=mgr._parse_tick;callback_thread=None
def slow_parse(message):
 parse_entered.set();assert parse_resume.wait(5);return original(message)
mgr._parse_tick=slow_parse
try:
 mgr.start();assert wait_until(lambda:len(FakeSmartWebSocketV2.instances)==1 and mgr.is_connected and mgr.sent_count==1)
 first=FakeSmartWebSocketV2.instances[0]
 callback_thread=threading.Thread(target=lambda:first.on_data(first.wsapp,{'token':'2885','last_traded_price':9000}),daemon=True);callback_thread.start();assert parse_entered.wait(2)
 first.drop('server_close');assert wait_until(lambda:len(FakeSmartWebSocketV2.instances)==2 and mgr.is_connected and mgr.sent_count==1)
 parse_resume.set();callback_thread.join(2)
 out.append({'case':'old data callback passes initial generation check, pauses parsing, resumes after replacement','delivered_prices':[t.ltp for t in seen],'current_generation':mgr.stats['socket_generation'],'stale_ticks_dropped':mgr.stats['stale_ticks_dropped']})
finally:
 parse_resume.set();mgr.stop()
 if callback_thread:callback_thread.join(2)
print(json.dumps(out,indent=2));(p/'generation-edge-results.json').write_text(json.dumps(out,indent=2)+'\n')
