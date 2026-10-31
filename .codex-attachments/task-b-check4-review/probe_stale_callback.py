from pathlib import Path
import sys,json,time
p=Path(__file__).resolve().parent;m=json.loads((p/'workspace.json').read_text());sys.path.insert(0,m['workspace'])
from loguru import logger
logger.remove()
from data.streams.websocket_manager import WebSocketManager
from tests.data.test_websocket_reconnect import FakeSmartWebSocketV2,FakeSession,FakeAudit,wait_until
FakeSmartWebSocketV2.reset(['stay_open','stay_open'])
wm=WebSocketManager(FakeSession(),FakeAudit());wm._create_sdk_client=lambda **kw:FakeSmartWebSocketV2(**kw);wm._backoff_wait=lambda delay:None;wm.subscribe(['2885'])
try:
 wm.start();assert wait_until(lambda:wm.is_connected and len(FakeSmartWebSocketV2.instances)==1)
 first=FakeSmartWebSocketV2.instances[0];first.drop('server_close')
 assert wait_until(lambda:wm.is_connected and len(FakeSmartWebSocketV2.instances)==2)
 before={'connected':wm.is_connected,'sent':wm.sent_count}
 first.on_close(first.wsapp)
 after={'connected':wm.is_connected,'sent':wm.sent_count,'current_socket_open':FakeSmartWebSocketV2.instances[1]._open}
 result={'case':'old connection close callback arrives after replacement is connected','before':before,'after':after}
 print(json.dumps(result,indent=2));(p/'stale-callback-probe.json').write_text(json.dumps(result,indent=2)+'\n')
finally:wm.stop()
