"""Read-only diagnostic for task selection; no broker credentials or requests.

Run from the project root with .venv/bin/python and this file's path.
The installed SDK method is extracted as an AST function, avoiding SDK import
side effects. Its HTTP method is replaced with a local response function.
"""
import ast
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from freezegun import freeze_time
from loguru import logger

from core.constants import AnalysisAction, SignalType
from tests.factories import make_analysis_result, make_signal
from tests.trading.test_live_squareoff import FakeBroker, RELIANCE, broker_row
from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from trading.exit_intent_store import ExitIntentStore
from trading.order_manager import OrderManager, TradingMode
from trading.paper_trader import PaperTrader
from trading.risk_manager import RiskConfig, RiskManager


def installed_sdk_placement():
    spec = importlib.util.find_spec("SmartApi")
    path = Path(spec.origin).parent / "smartConnect.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SmartConnect")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "placeOrder")
    namespace = {"logger": SimpleNamespace(error=lambda *args: None)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["placeOrder"]


SDK_PLACE = installed_sdk_placement()


class Audit:
    def log(self, *args, **kwargs):
        pass


class Broker:
    def __init__(self, rows=None, *, fail=False, sdk=False):
        self.rows, self.fail, self.sdk = rows, fail, sdk
        self.sent = []
        self.position_calls = self.orderbook_calls = 0

    def position(self):
        self.position_calls += 1
        if self.fail:
            raise RuntimeError("synthetic position outage")
        return {"status": True, "data": self.rows}

    def orderBook(self):
        self.orderbook_calls += 1
        return {"status": True, "data": None}

    def placeOrder(self, params):
        self.sent.append(dict(params))
        response = {"status": True, "data": {"orderid": f"OFFLINE-{len(self.sent)}"}}
        if self.sdk:
            return SDK_PLACE(SimpleNamespace(_postRequest=lambda *args: response), dict(params))
        return response


def manager(root, broker, config=None, mode=TradingMode.LIVE):
    audit = Audit()
    risk = RiskManager(config or RiskConfig(), audit=audit)
    paper = PaperTrader(capital=risk._config.total_capital, slippage_pct=0, audit=audit)
    return OrderManager(
        risk, CircuitBreaker(CircuitBreakerConfig(), audit=audit), paper, audit,
        mode, SimpleNamespace(get_api=lambda: broker), ExitIntentStore(root / "intents"),
    )


def buy(om, symbol="RELIANCE", token="2885", price=2830):
    result = om.execute_signal(
        make_signal(symbol=symbol, token=token, price=price, signal_type=SignalType.BUY),
        make_analysis_result(action=AnalysisAction.BUY),
    )
    return {
        "success": getattr(result, "success", None),
        "status": getattr(getattr(result, "status", None), "value", None),
        "message": getattr(result, "message", None),
    }


def run():
    logger.remove()
    results = []
    with TemporaryDirectory(prefix="trend-task4-") as directory, freeze_time("2026-03-04 04:30:00"):
        root = Path(directory)
        rows = [{**RELIANCE, "producttype": "INTRADAY", "netqty": "10", "buyavgprice": "2830"}]
        broker = Broker(rows)
        om = manager(root / "existing", broker, RiskConfig(max_open_positions=1))
        results.append({"case": "broker already holds target; max_open_positions=1", "result": buy(om),
                        "submissions": len(broker.sent), "position_queries": broker.position_calls})

        broker = Broker(fail=True)
        om = manager(root / "unavailable", broker)
        results.append({"case": "broker positions unavailable", "result": buy(om),
                        "submissions": len(broker.sent), "position_queries": broker.position_calls})

        for restart in (False, True):
            folder = root / ("restart" if restart else "repeat")
            broker = Broker()
            om = manager(folder, broker, RiskConfig(max_open_positions=1))
            first = buy(om)
            if restart:
                om = manager(folder, broker, RiskConfig(max_open_positions=1))
            second = buy(om)
            results.append({"case": "accepted BUY invisible in broker books", "restart": restart,
                            "results": [first, second], "submissions": len(broker.sent),
                            "position_queries": broker.position_calls, "orderbook_queries": broker.orderbook_calls})

        broker = Broker()
        cfg = RiskConfig(total_capital=100_000, max_capital_deployed_pct=60, max_capital_per_trade_pct=50)
        om = manager(root / "capital", broker, cfg)
        first = buy(om, "AAA", "1", 1000)
        second = buy(om, "BBB", "2", 1000)
        results.append({"case": "combined pending capital exceeds deployment cap", "results": [first, second],
                        "cap": 60_000, "submitted_notional": sum(int(p["quantity"]) * float(p["price"]) for p in broker.sent)})

        broker = Broker(sdk=True)
        om = manager(root / "sdk", broker)
        results.append({"case": "installed SDK return contract", "result": buy(om),
                        "broker_accepted_submissions": len(broker.sent)})

        for outcome in ("sdk", "timeout"):
            class ExitBroker(FakeBroker):
                def placeOrder(self, params):
                    self.placed.append(dict(params))
                    if outcome == "timeout":
                        raise TimeoutError("synthetic reply lost after broker acceptance")
                    response = {"status": True, "data": {"orderid": f"ACCEPTED-{len(self.placed)}"}}
                    return SDK_PLACE(SimpleNamespace(_postRequest=lambda *args: response), dict(params))

            broker = ExitBroker(rows=[broker_row(RELIANCE, 10)])
            om = manager(root / f"exit-{outcome}", broker)
            first, second = om.square_off_intraday(), om.square_off_intraday()
            results.append({"case": f"exit {outcome}; accepted order absent from book on repeat",
                            "broker_accepted_submissions": len(broker.placed),
                            "reported_placed": [len(first.live_placed), len(second.live_placed)],
                            "reported_rejected": [len(first.live_rejected), len(second.live_rejected)]})
    return results


if __name__ == "__main__":
    results = run()
    output = json.dumps(results, indent=2) + "\n"
    Path(__file__).with_name("offline-probe-results.json").write_text(output)
    print(output)
