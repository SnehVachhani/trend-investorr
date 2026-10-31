"""Offline research only: controlled failures, no broker or database calls."""
from pathlib import Path
import sys, json
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from loguru import logger
logger.remove()
from data.streams.websocket_manager import WebSocketManager
from core.exceptions import WebSocketError

class Socket:
    def __init__(self, fail=False):
        self.calls=[]
        self.fail=fail
    def subscribe(self, **kwargs):
        self.calls.append({'operation':'subscribe','tokens':list(kwargs['token_list'][0]['tokens'])})
        if self.fail: raise RuntimeError('synthetic subscription transport failure')
    def unsubscribe(self, **kwargs):
        self.calls.append({'operation':'unsubscribe','tokens':list(kwargs['token_list'][0]['tokens'])})
        if self.fail: raise RuntimeError('synthetic unsubscription transport failure')

def manager(socket):
    m=WebSocketManager(None,SimpleNamespace(log=lambda *a,**k: None))
    m._ws=socket
    return m

out=[]
s=Socket();m=manager(s);m.subscribe(['2885']);m.unsubscribe(['2885']);m._on_open(s)
out.append({'case':'cancel desired subscription before first connection','calls':s.calls,'active_tokens':sorted(m._subscribed_tokens)})
s=Socket(True);m=manager(s);m.subscribe(['2885']);m._on_open(s);s.fail=False;m._on_open(s)
out.append({'case':'subscription transport fails on first open, reconnect succeeds','calls':s.calls,'pending_tokens':sorted(m._pending_subscribes),'active_tokens':sorted(m._subscribed_tokens)})
s=Socket();m=manager(s);m.subscribe(['2885']);m._on_open(s)
out.append({'case':'first connect should subscribe each token once','calls':s.calls})
s=Socket(True);m=manager(s);m._is_connected=True;m._subscribed_tokens={'2885'};m.unsubscribe(['2885'])
out.append({'case':'unsubscribe transport failure','locally_active_tokens':sorted(m._subscribed_tokens),'pending_unsubscribe':sorted(m._pending_unsubscribes),'calls':s.calls})
s=Socket();m=manager(s);m._is_running=True
m._connect=lambda: (_ for _ in ()).throw(WebSocketError('synthetic failed connection'))
with patch('data.streams.websocket_manager.time.sleep',lambda _: None): m._run()
out.append({'case':'reconnect retry budget exhausted','is_running':m.is_running,'reconnect_count':m._reconnect_count})

from api import server
class NoThread:
    def __init__(self,*a,**k): pass
    def start(self): pass
with patch.object(server,'Thread',NoThread),patch.object(server,'_enhanced_momentum_backtest_jobs',{}):
    first=server.start_enhanced_momentum_backtest(server.EnhancedMomentumBacktestRequest())
    # A second process has the same initial empty module-local registry.
    with patch.object(server,'_enhanced_momentum_backtest_jobs',{}):
        second=server.start_enhanced_momentum_backtest(server.EnhancedMomentumBacktestRequest())
        try: server.get_enhanced_momentum_backtest_job(first['job_id'])
        except server.HTTPException as e: missing=e.status_code
    out.append({'case':'poll job through a fresh worker registry','first_worker_start':first['status'],'second_worker_start':second['status'],'first_job_lookup_status':missing,'different_job_ids':first['job_id']!=second['job_id']})
    server._enhanced_momentum_backtest_jobs.clear()
    try: server.get_enhanced_momentum_backtest_job(first['job_id'])
    except server.HTTPException as e: missing=e.status_code
    out.append({'case':'restart loses queued or running job record','lookup_status':missing})

# LIVE risk P&L update source: the broker is deliberately not queried here.
from trading.order_manager import OrderManager
from trading.order_manager import TradingMode
m=object.__new__(OrderManager)
m._mode=TradingMode.LIVE
calls=[]
m._paper_trader=SimpleNamespace(update_prices=lambda p:None,get_daily_summary=lambda:SimpleNamespace(realized_pnl=-5000.0,unrealized_pnl=-1000.0))
m._risk_manager=SimpleNamespace(update_daily_pnl=lambda *p:calls.append(p))
m.update_prices({'2885':2830})
out.append({'case':'LIVE price update with synthetic paper loss','risk_pnl_values':calls,'mode':m._mode.value})
print(json.dumps(out,indent=2))
Path(__file__).with_name('probe-results.json').write_text(json.dumps(out,indent=2)+'\n')
