"""
Trend-Investorr: Watchlist & Algorithm Binding CLI

Manage watchlists, stock assignments, and algorithm bindings
from the command line without needing a web dashboard.

Usage:
    python -m cli.watchlist_cli list
    python -m cli.watchlist_cli create hot_stocks --desc "High momentum stocks"
    python -m cli.watchlist_cli delete hot_stocks
    python -m cli.watchlist_cli add-stock hot_stocks RELIANCE
    python -m cli.watchlist_cli remove-stock hot_stocks RELIANCE
    python -m cli.watchlist_cli show hot_stocks
    python -m cli.watchlist_cli bind hot_stocks scanner_confluence
    python -m cli.watchlist_cli unbind hot_stocks connors_rsi_pullback
    python -m cli.watchlist_cli routing
    python -m cli.watchlist_cli algorithms
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger


def _get_services():
    """
    Connect to the database and return service instances.

    Returns:
        (stock_store, watchlist_manager) tuple.
    """
    from config.settings import Settings
    from storage.database import Database
    from storage.models import (
        StockStore,
        WatchlistStore,
        WatchlistAlgorithmBindingStore,
    )
    from core.watchlist_manager import WatchlistManager

    settings = Settings()
    db = Database(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_connections=1,
        max_connections=2,
    )
    db.connect()

    stock_store = StockStore(db)
    watchlist_manager = WatchlistManager(
        watchlist_store=WatchlistStore(db),
        binding_store=WatchlistAlgorithmBindingStore(db),
    )

    return stock_store, watchlist_manager


# ── Known algorithms (registered in main.py) ──
# Add new algorithm names here when you create them.
KNOWN_ALGORITHMS = [
    "scanner_confluence",
    "connors_rsi_pullback",
    "rayner_dip_buyer",
]


def cmd_list(args):
    """List all active watchlists with stock counts and bound algorithms."""
    _, wl_mgr = _get_services()
    watchlists = wl_mgr.get_all_watchlists()

    if not watchlists:
        print("\n  No active watchlists found. Create one with:")
        print("    python -m cli.watchlist_cli create <name> --desc \"description\"")
        return

    print(f"\n{'Name':<25} {'Type':<10} {'Stocks':<8} {'Max':<6} {'Bound Algorithms'}")
    print("─" * 85)
    for wl in watchlists:
        algos = wl.get("bound_algorithms", [])
        algo_str = ", ".join(algos) if algos else "(none)"
        print(
            f"  {wl['name']:<23} {wl['list_type']:<10} "
            f"{wl['stock_count']:<8} {wl['max_stocks']:<6} {algo_str}"
        )
    print()


def cmd_create(args):
    """Create a new watchlist."""
    _, wl_mgr = _get_services()

    wl_id = wl_mgr.create_watchlist(
        name=args.name,
        description=args.desc or "",
        list_type=args.type or "custom",
        max_stocks=args.max_stocks or 500,
    )

    if wl_id:
        print(f"✅ Watchlist '{args.name}' created (id={wl_id})")
    else:
        print(f"❌ Failed to create watchlist '{args.name}'")


def cmd_delete(args):
    """Soft-delete a watchlist."""
    _, wl_mgr = _get_services()

    if wl_mgr.delete_watchlist(args.name):
        print(f"✅ Watchlist '{args.name}' deactivated")
    else:
        print(f"❌ Watchlist '{args.name}' not found")


def cmd_add_stock(args):
    """Add a stock to a watchlist by symbol."""
    stock_store, wl_mgr = _get_services()

    stock = stock_store.get_by_symbol(args.symbol)
    if not stock:
        print(f"❌ Stock '{args.symbol}' not found in stocks table")
        return

    if wl_mgr.add_stock(args.watchlist, stock["id"], source="cli"):
        print(f"✅ Added {args.symbol} (token={stock['token']}) to '{args.watchlist}'")
    else:
        print(f"❌ Failed — watchlist '{args.watchlist}' not found or stock limit reached")


def cmd_remove_stock(args):
    """Remove a stock from a watchlist."""
    stock_store, wl_mgr = _get_services()

    stock = stock_store.get_by_symbol(args.symbol)
    if not stock:
        print(f"❌ Stock '{args.symbol}' not found")
        return

    wl_mgr.remove_stock(args.watchlist, stock["id"])
    print(f"✅ Removed {args.symbol} from '{args.watchlist}'")


def cmd_show(args):
    """Show details of a watchlist: stocks and bound algorithms."""
    _, wl_mgr = _get_services()

    detail = wl_mgr.get_watchlist_detail(args.name)
    if not detail:
        print(f"❌ Watchlist '{args.name}' not found")
        return

    # Header
    print(f"\n📋 Watchlist: {detail['name']}")
    print(f"   Description: {detail.get('description', '—')}")
    print(f"   Type: {detail['list_type']} | Max: {detail['max_stocks']} | Active: {detail['is_active']}")

    # Bound algorithms
    bindings = detail.get("bindings", [])
    if bindings:
        print(f"\n   🔗 Bound Algorithms:")
        for algo_name in bindings:
            print(f"      • {algo_name}")
    else:
        print(f"\n   🔗 Bound Algorithms: (none)")

    # Stocks
    items = detail.get("items", [])
    if items:
        print(f"\n   📊 Stocks ({len(items)}):")
        print(f"   {'Symbol':<15} {'Token':<10} {'Source':<12} {'Priority'}")
        print(f"   " + "─" * 50)
        for item in items:
            print(
                f"   {item['symbol']:<15} {item['token']:<10} "
                f"{item.get('source', '—'):<12} {item.get('priority', 0)}"
            )
    else:
        print(f"\n   📊 Stocks: (empty)")

    print()


def cmd_bind(args):
    """Bind an algorithm to a watchlist."""
    _, wl_mgr = _get_services()

    if args.algorithm not in KNOWN_ALGORITHMS:
        print(f"⚠️  Warning: '{args.algorithm}' is not a known algorithm.")
        print(f"   Known algorithms: {', '.join(KNOWN_ALGORITHMS)}")
        print(f"   Proceeding anyway (algorithm may be registered at runtime)...")

    if wl_mgr.bind_algorithm(args.watchlist, args.algorithm):
        print(f"✅ Bound '{args.algorithm}' to watchlist '{args.watchlist}'")
        print(f"   ℹ️  Changes take effect at next scanner_warmup() (9:00 AM)")
    else:
        print(f"❌ Watchlist '{args.watchlist}' not found or inactive")


def cmd_unbind(args):
    """Unbind an algorithm from a watchlist."""
    _, wl_mgr = _get_services()

    if wl_mgr.unbind_algorithm(args.watchlist, args.algorithm):
        print(f"✅ Unbound '{args.algorithm}' from watchlist '{args.watchlist}'")
    else:
        print(f"❌ Binding not found: '{args.algorithm}' ↔ '{args.watchlist}'")


def cmd_routing(args):
    """Show the full routing table (token → algorithms)."""
    _, wl_mgr = _get_services()

    summary = wl_mgr.get_routing_summary()
    if not summary:
        print("\n  No algorithm bindings configured.")
        print("  System is running in passthrough mode (all algorithms see all stocks).")
        print("\n  Create bindings with:")
        print("    python -m cli.watchlist_cli bind <watchlist> <algorithm>")
        return

    print(f"\n{'Watchlist':<25} {'Algorithm':<30} {'Stocks'}")
    print("─" * 65)
    for row in summary:
        print(
            f"  {row['watchlist_name']:<23} {row['algorithm_name']:<30} "
            f"{row['stock_count']}"
        )

    # Also show the computed routing table stats
    router = wl_mgr.load_router()
    print(f"\n  Router mode: {router.mode}")
    print(f"  Router stats: {router.stats}")
    print()


def cmd_algorithms(args):
    """List known algorithms and their watchlist bindings."""
    _, wl_mgr = _get_services()

    print(f"\n{'Algorithm':<30} {'Bound Watchlists'}")
    print("─" * 65)

    for algo_name in KNOWN_ALGORITHMS:
        bindings = wl_mgr.get_algorithm_bindings(algo_name)
        if bindings:
            wl_names = ", ".join(b["name"] for b in bindings)
        else:
            wl_names = "(no bindings — will receive NOTHING)"
        print(f"  {algo_name:<28} {wl_names}")

    print("\n  ℹ️  Option (A) active: algorithms without bindings receive no observations.")
    print()


def main():
    """CLI entry point."""
    # Suppress loguru for CLI output
    logger.remove()

    parser = argparse.ArgumentParser(
        description="Trend-Investorr: Watchlist & Algorithm Binding Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m cli.watchlist_cli list
  python -m cli.watchlist_cli create hot_stocks --desc "High momentum stocks"
  python -m cli.watchlist_cli add-stock hot_stocks RELIANCE
  python -m cli.watchlist_cli bind hot_stocks scanner_confluence
  python -m cli.watchlist_cli show hot_stocks
  python -m cli.watchlist_cli routing
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # list
    subparsers.add_parser("list", help="List all active watchlists")

    # create
    p_create = subparsers.add_parser("create", help="Create a new watchlist")
    p_create.add_argument("name", help="Watchlist name (lowercase, no spaces)")
    p_create.add_argument("--desc", help="Description", default="")
    p_create.add_argument("--type", help="List type (custom/system)", default="custom")
    p_create.add_argument("--max-stocks", type=int, help="Max stocks", default=500)

    # delete
    p_delete = subparsers.add_parser("delete", help="Deactivate a watchlist")
    p_delete.add_argument("name", help="Watchlist name")

    # add-stock
    p_add = subparsers.add_parser("add-stock", help="Add a stock to a watchlist")
    p_add.add_argument("watchlist", help="Watchlist name")
    p_add.add_argument("symbol", help="Stock symbol (e.g. RELIANCE)")

    # remove-stock
    p_rm = subparsers.add_parser("remove-stock", help="Remove a stock from a watchlist")
    p_rm.add_argument("watchlist", help="Watchlist name")
    p_rm.add_argument("symbol", help="Stock symbol")

    # show
    p_show = subparsers.add_parser("show", help="Show watchlist details")
    p_show.add_argument("name", help="Watchlist name")

    # bind
    p_bind = subparsers.add_parser("bind", help="Bind an algorithm to a watchlist")
    p_bind.add_argument("watchlist", help="Watchlist name")
    p_bind.add_argument("algorithm", help="Algorithm name (e.g. scanner_confluence)")

    # unbind
    p_unbind = subparsers.add_parser("unbind", help="Unbind an algorithm from a watchlist")
    p_unbind.add_argument("watchlist", help="Watchlist name")
    p_unbind.add_argument("algorithm", help="Algorithm name")

    # routing
    subparsers.add_parser("routing", help="Show full routing table")

    # algorithms
    subparsers.add_parser("algorithms", help="List algorithms and their bindings")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        "list": cmd_list,
        "create": cmd_create,
        "delete": cmd_delete,
        "add-stock": cmd_add_stock,
        "remove-stock": cmd_remove_stock,
        "show": cmd_show,
        "bind": cmd_bind,
        "unbind": cmd_unbind,
        "routing": cmd_routing,
        "algorithms": cmd_algorithms,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
