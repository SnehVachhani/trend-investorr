from pathlib import Path
import sys,json
from datetime import datetime as RealDatetime
p=Path(__file__).resolve().parent;meta=json.loads((p/'workspace.json').read_text());sys.path.insert(0,meta['workspace'])
from loguru import logger
logger.remove()
import data.streams.websocket_manager as module
from tests.data.ws_fakes import FakeSmartWebSocketV2,FakeSession,FakeAudit,wait_until
now=[1700000000.0]
class ClockDatetime:
 @staticmethod
 def now(tz=None):return RealDatetime.fromtimestamp(now[0],tz)
module.datetime=ClockDatetime
FakeSmartWebSocketV2.reset(['stay_open']*10)
m=module.WebSocketManager(FakeSession(),FakeAudit());m._clock=lambda:now[0];m._create_sdk_client=lambda **kw:FakeSmartWebSocketV2(**kw);m._backoff_wait=lambda *args:None;m.subscribe(['2885'])
counts=[]
try:
 m.start();assert wait_until(lambda:m.is_synced)
 for i in range(m.MAX_RECONNECT_ATTEMPTS+2):
  client=FakeSmartWebSocketV2.instances[-1];gen=m.stats['socket_generation'];now[0]+=m.HEARTBEAT_TIMEOUT+1
  assert m._health_check(gen,client) is False
  assert wait_until(lambda:m.stats['reconnect_exhausted'] or (len(FakeSmartWebSocketV2.instances)>=i+2 and m.is_synced))
  counts.append(m.stats['reconnect_count'])
  if m.stats['reconnect_exhausted']:break
 result={'case':'repeated sockets open without ever delivering pong or tick, each silently times out','silent_failures':m.stats['silent_failures'],'configured_retry_limit':m.MAX_RECONNECT_ATTEMPTS,'retry_count_after_each_recovery':counts,'is_running':m.is_running,'exhausted':m.stats['reconnect_exhausted'],'sdk_instances':len(FakeSmartWebSocketV2.instances)}
 print(json.dumps(result,indent=2));(p/'silent-budget-results.json').write_text(json.dumps(result,indent=2)+'\n')
finally:m.stop()
