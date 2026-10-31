from pathlib import Path
import sys,json
p=Path(__file__).resolve().parent;m=json.loads((p/'workspace.json').read_text());sys.path.insert(0,m['workspace'])
from loguru import logger
logger.remove()
from data.streams.websocket_manager import WebSocketManager
from core.constants import SMARTAPI_MAX_TOKENS_PER_CONNECTION
from tests.data.test_websocket_subscriptions import FakeSocket,open_connection
limit=SMARTAPI_MAX_TOKENS_PER_CONNECTION
wm=WebSocketManager(None,None);wm.subscribe([str(i) for i in range(limit+1)])
out=[{'case':'more requested tokens than one connection capacity','per_connection_limit':limit,'requested':wm.requested_count,'accepted':True}]
wm=WebSocketManager(None,None);wm.subscribe([str(i) for i in range(limit)]);sock=FakeSocket();open_connection(wm,sock)
def fail_unsubscribe(**kwargs):raise RuntimeError('synthetic send failure')
sock.unsubscribe=fail_unsubscribe
wm.unsubscribe(['0']);wm.subscribe(['new'])
out.append({'case':'failed unsubscribe followed by replacement request','requested':wm.requested_count,'sent':wm.sent_count,'per_connection_limit':limit})
print(json.dumps(out,indent=2));(p/'capacity-probes.json').write_text(json.dumps(out,indent=2)+'\n')
