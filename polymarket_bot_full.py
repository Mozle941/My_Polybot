"""
╔══════════════════════════════════════════════════════════════════╗
║     POLYMARKET COPY-TRADING BOT — WITH LIVE DASHBOARD           ║
║                                                                  ║
║  HOW TO RUN:                                                     ║
║    1. pip install flask py-clob-client python-dotenv web3        ║
║    2. Fill in your .env file                                     ║
║    3. python polymarket_bot_full.py                              ║
║    4. Open http://localhost:5000 in your browser                 ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, time, logging, threading, json, requests
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional
from flask import Flask, jsonify, Response, request
from collections import deque

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 1. CONFIG
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BotConfig:
    starting_capital: float = 10.0
    min_trade_size: float = 1.0
    min_trades_history: int = 50
    min_roi_pct: float = 0.15
    min_recent_roi_pct: float = 0.05
    min_calibration_score: float = 0.65
    min_efficiency_score: float = 0.15
    trader_allocations: Dict[str, float] = field(default_factory=lambda: {
        "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2": 0.40, "0xc2e7800b5af46e6093872b177b7a5e7f0563be51": 0.30,
        "0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9": 0.20, "0xde17f7144fbd0eddb2679132c10ff5e74b120988": 0.10,
    })
    reserve_trader: str = "0x1f0ebc543b2d411f66947041625c0aa1ce61cf86"
    max_order_size_pct: float = 0.02
    order_split_chunks: int = 5
    chunk_delay_seconds: int = 45
    max_acceptable_slippage: float = 0.015
    copy_delay_seconds: int = 30
    min_market_liquidity: float = 10_000.0
    max_positions_open: int = 10
    kelly_fraction: float = 0.25
    max_single_position_pct: float = 0.05
    max_category_exposure_pct: float = 0.25
    max_single_entity_exposure_pct: float = 0.15
    consecutive_loss_limit: int = 4
    reactivation_wins_needed: int = 3
    weekly_roi_floor: float = 0.10
    performance_drop_threshold: float = 0.40
    insider_size_multiplier: float = 5.0
    insider_liquidity_threshold: float = 5_000.0
    insider_hours_to_resolution: int = 12
    insider_market_age_days: int = 2
    max_insider_copy_pct: float = 0.30
    stop_loss_pct: float = 0.40
    profit_tiers: Dict[float, float] = field(default_factory=lambda: {
        0.20: 0.30, 0.35: 0.25, 0.50: 0.20, 0.75: 0.50,
    })
    stablecoin_lock_cooldown_hours: int = 72
    reentry_drop_level_1: float = 0.15
    reentry_drop_level_2: float = 0.25
    poll_interval_seconds: int = 60
    rebalance_interval_days: int = 7
    paper_mode: bool = True
    dashboard_port: int = 5000

# ══════════════════════════════════════════════════════════════════════════════
# 2. DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    trader_id: str; market_id: str; market_name: str; side: str
    size_usd: float; implied_prob: float; market_liquidity: float
    hours_to_resolution: float; market_age_days: float; trader_avg_size: float

@dataclass
class Position:
    market_id: str; market_name: str; trader_id: str
    entry_price: float; size_usd: float; side: str
    opened_at: str = field(default_factory=lambda: datetime.now().isoformat())

@dataclass
class ExecutionResult:
    success: bool; avg_price: float = 0.0; filled_size: float = 0.0; reason: str = ""

@dataclass
class StablecoinLock:
    amount_usd: float; locked_at: datetime; trigger_roi: float
    exit_price_reference: float; redeployed: bool = False

# ══════════════════════════════════════════════════════════════════════════════
# 3. PORTFOLIO
# ══════════════════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self, starting_capital):
        self.starting_capital = starting_capital
        self.active_capital = starting_capital
        self.stablecoin_reserve = 0.0
        self.open_positions: List[Position] = []
        self.closed_pnl = 0.0
        self.trade_log: List[dict] = []
        self.equity_history: List[dict] = [{"time": datetime.now().strftime("%H:%M"), "value": starting_capital}]

    @property
    def total_capital(self):
        return self.active_capital + self.stablecoin_reserve + sum(p.size_usd for p in self.open_positions)

    @property
    def current_roi(self):
        return (self.total_capital - self.starting_capital) / self.starting_capital

    def _record_equity(self):
        self.equity_history.append({"time": datetime.now().strftime("%H:%M"), "value": round(self.total_capital, 2)})
        if len(self.equity_history) > 288:  # ~24h at 5min intervals
            self.equity_history.pop(0)

    def add_position(self, trade: Trade, size: float, avg_price: float):
        self.open_positions.append(Position(
            market_id=trade.market_id, market_name=trade.market_name,
            trader_id=trade.trader_id, entry_price=avg_price, size_usd=size, side=trade.side,
        ))
        self.active_capital -= size
        self._record_equity()

    def close_position(self, position: Position, exit_price: float):
        pnl = (exit_price - position.entry_price) / position.entry_price * position.size_usd
        self.active_capital += position.size_usd + pnl
        self.closed_pnl += pnl
        self.open_positions.remove(position)
        self.trade_log.append({
            "time": datetime.now().strftime("%H:%M:%S"), "action": "CLOSE",
            "market": position.market_name[:50], "trader": position.trader_id,
            "pnl_usd": round(pnl, 2), "pnl_pct": round(pnl / position.size_usd * 100, 1),
        })
        self._record_equity()

    def lock_to_stablecoin(self, amount):
        t = min(amount, self.active_capital)
        self.active_capital -= t; self.stablecoin_reserve += t
        self._record_equity()

    def unlock_from_stablecoin(self, amount):
        t = min(amount, self.stablecoin_reserve)
        self.stablecoin_reserve -= t; self.active_capital += t

    def to_dict(self):
        return {
            "total_capital": round(self.total_capital, 2),
            "active_capital": round(self.active_capital, 2),
            "stablecoin_reserve": round(self.stablecoin_reserve, 2),
            "closed_pnl": round(self.closed_pnl, 2),
            "current_roi": round(self.current_roi * 100, 2),
            "starting_capital": self.starting_capital,
            "open_positions": [asdict(p) for p in self.open_positions],
            "trade_log": list(reversed(self.trade_log[-50:])),
            "equity_history": self.equity_history,
        }

# ══════════════════════════════════════════════════════════════════════════════
# 4. TRADER RANKER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TraderStats:
    trader_id: str; roi_lifetime: float; roi_30d: float; pnl_usd: float
    volume_usd: float; trade_count: int; calibration_score: float; win_rate_slope: float
    consecutive_losses: int = 0; suspended: bool = False; suspension_wins: int = 0

    @property
    def efficiency_score(self):
        return (self.roi_lifetime * 100) / (self.volume_usd / 1_000_000) if self.volume_usd else 0.0

    @property
    def composite_score(self):
        if self.suspended: return 0.0
        return (self.roi_lifetime * 0.35 + self.roi_30d * 0.25 + self.win_rate_slope * 0.15 +
                self.calibration_score * 0.15 + min(self.efficiency_score / 100, 1.0) * 0.10)

    @property
    def health(self):
        if self.suspended: return "SUSPENDED"
        if self.consecutive_losses >= 3: return "WARNING"
        return "HEALTHY"

    def to_dict(self, alloc=0.0):
        return {
            "id": self.trader_id, "alloc": round(alloc * 100, 0),
            "roi": round(self.roi_lifetime * 100, 1), "roi_30d": round(self.roi_30d * 100, 1),
            "pnl": self.pnl_usd, "volume": self.volume_usd,
            "efficiency": round(self.efficiency_score, 1),
            "calibration": round(self.calibration_score * 100, 1),
            "losses": self.consecutive_losses, "health": self.health,
            "score": round(self.composite_score, 3),
        }

class TraderRanker:
    def __init__(self, config: BotConfig):
        self.config = config
        self.traders: Dict[str, TraderStats] = {}
        self._load()

    def _load(self):
        # Seed any wallet addresses from config that aren't already in traders dict.
        # These get placeholder stats — real stats are fetched live once connected.
        for wallet_addr in self.config.trader_allocations:
            if wallet_addr not in self.traders:
                self.traders[wallet_addr] = TraderStats(
                    trader_id=wallet_addr,
                    roi_lifetime=0.20,   # placeholder — will update from live data
                    roi_30d=0.15,
                    pnl_usd=0,
                    volume_usd=1_000_000,
                    trade_count=999,     # bypass trade_count filter for config wallets
                    calibration_score=0.66,
                    win_rate_slope=0.0,
                )
        # Also seed reserve trader
        if self.config.reserve_trader and self.config.reserve_trader not in self.traders:
            self.traders[self.config.reserve_trader] = TraderStats(
                trader_id=self.config.reserve_trader,
                roi_lifetime=0.18, roi_30d=0.12, pnl_usd=0,
                volume_usd=500_000, trade_count=999,
                calibration_score=0.65, win_rate_slope=0.0,
            )
        log.info(f"Loaded {len(self.traders)} traders | Following: {list(self.config.trader_allocations.keys())}")

    def _eligible(self):
        """
        Config wallets bypass strict filters — they are followed by explicit choice.
        Non-config traders must pass all quality filters.
        """
        result = []
        for t in self.traders.values():
            if t.suspended:
                continue
            # Always include explicitly configured wallets
            if t.trader_id in self.config.trader_allocations:
                result.append(t)
                continue
            # Non-config traders must pass all quality filters
            if (t.trade_count >= self.config.min_trades_history
                    and t.roi_lifetime >= self.config.min_roi_pct
                    and t.calibration_score >= self.config.min_calibration_score
                    and t.efficiency_score >= self.config.min_efficiency_score):
                result.append(t)
        return sorted(result, key=lambda x: x.composite_score, reverse=True)

    def active_traders(self):
        """Return wallet addresses of all followed, non-suspended traders."""
        return [
            tid for tid in self.config.trader_allocations
            if tid in self.traders and not self.traders[tid].suspended
        ]

    def is_healthy(self, tid):
        t = self.traders.get(tid)
        if not t: return False
        # Config wallets are always considered healthy unless suspended
        if tid in self.config.trader_allocations:
            return not t.suspended
        return bool(not t.suspended and t.roi_30d >= self.config.min_recent_roi_pct)

    def rerank(self):
        for t in self.traders.values(): self._check_decay(t)
        self._rotate_allocations(self._eligible())

    def _check_decay(self, t):
        if t.consecutive_losses >= self.config.consecutive_loss_limit:
            if not t.suspended:
                log.warning(f"Suspending {t.trader_id}")
                t.suspended = True; t.suspension_wins = 0
            return
        if t.suspended and t.suspension_wins >= self.config.reactivation_wins_needed:
            t.suspended = False; t.consecutive_losses = 0
            log.info(f"Reinstated {t.trader_id}")
        if t.roi_30d < self.config.weekly_roi_floor:
            cur = self.config.trader_allocations.get(t.trader_id, 0)
            if cur > 0: self.config.trader_allocations[t.trader_id] = cur * 0.5

    def _rotate_allocations(self, eligible):
        for tid in list(self.config.trader_allocations):
            t = self.traders.get(tid)
            if t and t.suspended:
                for c in eligible:
                    if c.trader_id not in self.config.trader_allocations:
                        freed = self.config.trader_allocations.pop(tid)
                        self.config.trader_allocations[c.trader_id] = freed
                        log.info(f"Rotated {tid} → {c.trader_id}")
                        break

    def record_result(self, tid, won):
        t = self.traders.get(tid)
        if not t: return
        if won: t.consecutive_losses = 0; t.suspension_wins += (1 if t.suspended else 0)
        else: t.consecutive_losses += 1

    def to_dict(self):
        return [t.to_dict(self.config.trader_allocations.get(t.trader_id, 0))
                for t in sorted(self.traders.values(), key=lambda x: x.composite_score, reverse=True)]

# ══════════════════════════════════════════════════════════════════════════════
# 5. RISK MANAGER
# ══════════════════════════════════════════════════════════════════════════════

CATEGORY_MAP = {
    "politics": ["election","trump","biden","harris","senate","congress","president"],
    "crypto":   ["btc","eth","bitcoin","ethereum","crypto","defi","token"],
    "sports":   ["nfl","nba","mlb","soccer","champion","super bowl","world cup"],
    "macro":    ["fed","rates","inflation","recession","gdp","fomc"],
    "geo":      ["war","ukraine","russia","china","taiwan","nato","israel"],
}

class RiskManager:
    def __init__(self, config): self.config = config

    def check_slippage(self, trade):
        return trade.size_usd <= trade.market_liquidity * self.config.max_order_size_pct * 5

    def check_correlation(self, trade, portfolio):
        cat = self._cat(trade.market_name); ent = self._ent(trade.market_name)
        total = portfolio.total_capital or 1
        cat_exp = sum(p.size_usd for p in portfolio.open_positions if self._cat(p.market_name) == cat)
        ent_exp = sum(p.size_usd for p in portfolio.open_positions if ent and self._ent(p.market_name) == ent)
        if ent and ent_exp / total >= self.config.max_single_entity_exposure_pct: return False
        if cat_exp / total >= self.config.max_category_exposure_pct: return False
        return True

    def insider_score(self, trade):
        s = 0
        if trade.trader_avg_size > 0 and trade.size_usd > trade.trader_avg_size * self.config.insider_size_multiplier: s += 3
        if trade.market_liquidity < self.config.insider_liquidity_threshold: s += 2
        if trade.hours_to_resolution < self.config.insider_hours_to_resolution: s += 3
        if trade.market_age_days < self.config.insider_market_age_days: s += 2
        return s

    def category_exposure(self, portfolio):
        total = portfolio.total_capital or 1
        cats = {}
        for p in portfolio.open_positions:
            c = self._cat(p.market_name)
            cats[c] = cats.get(c, 0) + p.size_usd / total
        return {k: round(v * 100, 1) for k, v in cats.items()}

    def _cat(self, name):
        n = name.lower()
        for c, kws in CATEGORY_MAP.items():
            if any(k in n for k in kws): return c
        return "other"

    def _ent(self, name):
        for e in ["trump","biden","harris","btc","eth","fed"]:
            if e in name.lower(): return e
        return None

# ══════════════════════════════════════════════════════════════════════════════
# 6. POSITION SIZER
# ══════════════════════════════════════════════════════════════════════════════

class PositionSizer:
    def __init__(self, config): self.config = config

    def calculate(self, trade, portfolio):
        if len(portfolio.open_positions) >= self.config.max_positions_open: return 0.0
        p = trade.implied_prob; b = (1/p - 1) if p > 0 else 0
        if b <= 0: return 0.0
        kelly = max(0, (p*b - (1-p)) / b) * self.config.kelly_fraction
        alloc = self.config.trader_allocations.get(trade.trader_id, 0.10)
        return min(portfolio.active_capital * kelly * alloc,
                   portfolio.total_capital * self.config.max_single_position_pct)

# ══════════════════════════════════════════════════════════════════════════════
# 7. PROFIT MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class ProfitManager:
    def __init__(self, config):
        self.config = config
        self.locks: List[StablecoinLock] = []
        self.triggered_tiers: set = set()
        self.last_lock_time: Optional[datetime] = None

    def check_triggers(self, portfolio):
        for trigger, pct in sorted(self.config.profit_tiers.items()):
            if portfolio.current_roi >= trigger and trigger not in self.triggered_tiers:
                self._lock(portfolio, trigger, pct)
                self.triggered_tiers.add(trigger)
        self._reentry(portfolio)

    def _lock(self, portfolio, trigger, pct):
        if self.last_lock_time:
            hrs = (datetime.now() - self.last_lock_time).total_seconds() / 3600
            if hrs < self.config.stablecoin_lock_cooldown_hours: return
        gains = portfolio.total_capital - portfolio.starting_capital
        amount = gains * pct
        if amount < 1: return
        self.locks.append(StablecoinLock(amount, datetime.now(), trigger, portfolio.total_capital))
        portfolio.lock_to_stablecoin(amount)
        self.last_lock_time = datetime.now()
        portfolio.trade_log.append({
            "time": datetime.now().strftime("%H:%M:%S"), "action": "LOCK",
            "market": f"+{trigger:.0%} ROI milestone", "trader": "—",
            "pnl_usd": round(amount, 2), "pnl_pct": 0,
        })
        log.info(f"PROFIT LOCK @ +{trigger:.0%} | ${amount:,.2f} locked")

    def _reentry(self, portfolio):
        for l in self.locks:
            if l.redeployed or l.amount_usd <= 0: continue
            drop = (l.exit_price_reference - portfolio.total_capital) / l.exit_price_reference
            if drop >= self.config.reentry_drop_level_2:
                portfolio.unlock_from_stablecoin(l.amount_usd); l.redeployed = True
            elif drop >= self.config.reentry_drop_level_1:
                half = l.amount_usd * 0.5
                portfolio.unlock_from_stablecoin(half); l.amount_usd -= half

    @property
    def total_locked(self): return sum(l.amount_usd for l in self.locks if not l.redeployed)

    def to_dict(self):
        return {
            "total_locked": round(self.total_locked, 2),
            "triggered_tiers": sorted([round(t * 100) for t in self.triggered_tiers]),
            "all_tiers": sorted([round(t * 100) for t in self.config.profit_tiers.keys()]),
        }

# ══════════════════════════════════════════════════════════════════════════════
# 8. POLYMARKET CLIENT
# ══════════════════════════════════════════════════════════════════════════════

HOST = "https://clob.polymarket.com"; GAMMA = "https://gamma-api.polymarket.com"

class PolymarketClient:
    def __init__(self, config):
        self.config = config
        self._seen: set = set()
        self._paper = config.paper_mode      # controls order EXECUTION only
        self._connected = False              # controls whether CLOB client is available
        self._trader_positions_cache: Dict[str, list] = {}  # wallet → positions

        # Always try to connect for READ access (trade/position watching)
        # even in paper mode — only ORDER EXECUTION is blocked in paper mode
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType, TradeParams
            from py_clob_client.order_builder.constants import BUY, SELL
            from dotenv import load_dotenv; load_dotenv()
            pk = os.getenv("POLYMARKET_PRIVATE_KEY")
            pf = os.getenv("POLYMARKET_PROXY_FUNDER")
            lm = os.getenv("LOGIN_METHOD", "email").lower()
            if not pk: raise EnvironmentError("POLYMARKET_PRIVATE_KEY not set")
            st = {"email":1,"metamask":2,"eoa":0}.get(lm, 1)
            self._OA = OrderArgs; self._OT = OrderType; self._TP = TradeParams
            self._BUY = BUY; self._SELL = SELL
            self.client = (ClobClient(HOST, key=pk, chain_id=137) if st==0
                           else ClobClient(HOST, key=pk, chain_id=137, signature_type=st, funder=pf))
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self._connected = True
            mode = "PAPER (read-only)" if self._paper else "LIVE"
            log.info(f"Connected to Polymarket | Mode: {mode}")
        except Exception as e:
            log.warning(f"Polymarket connection failed: {e}")
            log.warning("Running in FULL PAPER MODE — no live data")
        if self._paper: log.info("Order execution DISABLED (paper mode)")

    def get_price(self, tid):
        if not self._connected:
            import random; return round(random.uniform(0.3, 0.7), 4)
        try: return float(self.client.get_midpoint(tid).get("mid", 0.5))
        except: return 0.5

    def get_depth(self, tid):
        if not self._connected: return 50_000.0
        try:
            b = self.client.get_order_book(tid)
            return sum(float(x["size"])*float(x["price"]) for x in b.get("bids",[])) + \
                   sum(float(x["size"]) for x in b.get("asks",[]))
        except: return 0.0

    def get_market_info(self, tid):
        try:
            r = requests.get(f"{GAMMA}/markets", params={"clob_token_ids": tid}, timeout=10)
            d = r.json(); return d[0] if d else {}
        except: return {}

    def get_recent_trades(self, trader_ids):
        """Poll recent trades from followed wallets — works in both paper and live mode."""
        if not self._connected:
            return []   # No credentials at all — truly offline
        out = []
        for tid in trader_ids:
            try:
                resp = self.client.get_trades(self._TP(maker_address=tid))
                for t in (resp if isinstance(resp, list) else resp.get("data",[])):
                    uid = t.get("id") or t.get("transaction_hash","")
                    if uid in self._seen: continue
                    self._seen.add(uid)
                    mkid = t.get("asset_id",""); mi = self.get_market_info(mkid)
                    out.append(Trade(
                        trader_id=tid, market_id=mkid,
                        market_name=mi.get("question","Unknown"),
                        side="YES" if t.get("side","BUY").upper()=="BUY" else "NO",
                        size_usd=float(t.get("size",0)), implied_prob=float(t.get("price",0.5)),
                        market_liquidity=self.get_depth(mkid),
                        hours_to_resolution=self._hrs(mi), market_age_days=self._age(mi),
                        trader_avg_size=self._avg(tid),
                    ))
            except Exception as e: log.error(f"trades failed {tid}: {e}")
        return out

    def get_trader_positions(self, trader_ids: List[str]) -> Dict[str, list]:
        """
        Fetch current open positions for each followed wallet.
        Uses the official Data API: https://data-api.polymarket.com/positions
        Query param: user=0x... (no auth required — fully public)
        Field names from official docs: title, outcome, size, avgPrice,
        currentValue, cashPnl, curPrice, asset, conditionId, slug
        """
        DATA_API = "https://data-api.polymarket.com"
        result: Dict[str, list] = {}

        for wallet in trader_ids:
            try:
                r = requests.get(
                    f"{DATA_API}/positions",
                    params={
                        "user": wallet,
                        "sizeThreshold": 0.01,
                        "limit": 100,
                        "sortBy": "CURRENT",
                        "sortDirection": "DESC",
                    },
                    timeout=10,
                )
                r.raise_for_status()
                raw = r.json() if isinstance(r.json(), list) else r.json().get("data", [])

                parsed = []
                for p in raw:
                    size = float(p.get("size", 0) or 0)
                    if size <= 0:
                        continue
                    parsed.append({
                        "market_id":   p.get("asset", p.get("conditionId", "")),
                        "market_name": p.get("title", "Unknown Market"),
                        "side":        "YES" if str(p.get("outcome","YES")).upper() == "YES" else "NO",
                        "size":        round(size, 4),
                        "avg_price":   round(float(p.get("avgPrice", 0) or 0), 4),
                        "cur_price":   round(float(p.get("curPrice", 0) or 0), 4),
                        "current_val": round(float(p.get("currentValue", 0) or 0), 2),
                        "pnl":         round(float(p.get("cashPnl", 0) or 0), 2),
                        "pnl_pct":     round(float(p.get("percentPnl", 0) or 0), 2),
                        "end_date":    p.get("endDate", ""),
                        "slug":        p.get("eventSlug", p.get("slug", "")),
                    })

                result[wallet] = parsed
                log.info(f"Positions fetched: {wallet[:12]}... → {len(parsed)} open")

            except Exception as e:
                log.warning(f"get_trader_positions failed for {wallet[:12]}: {e}")
                result[wallet] = []

        self._trader_positions_cache = result
        return result

    def get_price(self, tid):
        if self._paper:
            import random; return round(random.uniform(0.3, 0.7), 4)
        try: return float(self.client.get_midpoint(tid).get("mid", 0.5))
        except: return 0.5

    def get_depth(self, tid):
        if self._paper: return 50_000.0
        try:
            b = self.client.get_order_book(tid)
            return sum(float(x["size"])*float(x["price"]) for x in b.get("bids",[])) + \
                   sum(float(x["size"]) for x in b.get("asks",[]))
        except: return 0.0

    def get_market_info(self, tid):
        try:
            r = requests.get(f"{GAMMA}/markets", params={"clob_token_ids": tid}, timeout=10)
            d = r.json(); return d[0] if d else {}
        except: return {}

    def get_recent_trades(self, trader_ids):
        if self._paper: return []
        out = []
        for tid in trader_ids:
            try:
                resp = self.client.get_trades(self._TP(maker_address=tid))
                for t in (resp if isinstance(resp, list) else resp.get("data",[])):
                    uid = t.get("id") or t.get("transaction_hash","")
                    if uid in self._seen: continue
                    self._seen.add(uid)
                    mkid = t.get("asset_id",""); mi = self.get_market_info(mkid)
                    out.append(Trade(
                        trader_id=tid, market_id=mkid,
                        market_name=mi.get("question","Unknown"),
                        side="YES" if t.get("side","BUY").upper()=="BUY" else "NO",
                        size_usd=float(t.get("size",0)), implied_prob=float(t.get("price",0.5)),
                        market_liquidity=self.get_depth(mkid),
                        hours_to_resolution=self._hrs(mi), market_age_days=self._age(mi),
                        trader_avg_size=self._avg(tid),
                    ))
            except Exception as e: log.error(f"trades failed {tid}: {e}")
        return out

    def _hrs(self, mi):
        ed = mi.get("end_date_iso") or mi.get("end_date")
        if not ed: return 72.0
        try:
            dt = datetime.fromisoformat(ed.replace("Z","+00:00"))
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds()/3600)
        except: return 72.0

    def _age(self, mi):
        cr = mi.get("created_at") or mi.get("start_date_iso")
        if not cr: return 30.0
        try:
            dt = datetime.fromisoformat(cr.replace("Z","+00:00"))
            return (datetime.now(timezone.utc) - dt).total_seconds()/86400
        except: return 30.0

    def _avg(self, tid):
        try:
            resp = self.client.get_trades(self._TP(maker_address=tid))
            sizes = [float(t.get("size",0)) for t in (resp if isinstance(resp,list) else resp.get("data",[]))[-50:]]
            return sum(sizes)/len(sizes) if sizes else 100.0
        except: return 100.0

    def execute_split_order(self, market_id, side, total_size, chunks, delay, max_slip):
        if self._paper:
            p = self.get_price(market_id)
            log.info(f"[PAPER] {side} ${total_size:.2f} @ {p:.4f}")
            return ExecutionResult(True, p, total_size, "paper")
        if not self._connected:
            return ExecutionResult(False, reason="Not connected to Polymarket")
        chunk = total_size / chunks; ep = self.get_price(market_id); prices = []
        cs = self._BUY if side in ("YES","BUY") else self._SELL
        for i in range(chunks):
            cp = self.get_price(market_id)
            if abs(cp-ep)/max(ep,0.0001) > max_slip: break
            try:
                o = self.client.create_order(self._OA(token_id=market_id,price=round(cp,4),size=round(chunk,2),side=cs))
                r = self.client.post_order(o, self._OT.GTC)
                if r.get("errorMsg"): break
                prices.append(cp)
            except Exception as e: log.error(f"chunk {i+1}: {e}"); break
            if i < chunks-1: time.sleep(delay)
        if not prices: return ExecutionResult(False, reason="No fills")
        return ExecutionResult(True, sum(prices)/len(prices), chunk*len(prices))

    def close_position(self, pos):
        p = self.get_price(pos.market_id)
        if self._paper: return ExecutionResult(True, p, pos.size_usd)
        if not self._connected: return ExecutionResult(False, reason="Not connected")
        try:
            o = self.client.create_order(self._OA(token_id=pos.market_id,price=round(p,4),size=round(pos.size_usd,2),side=self._SELL))
            r = self.client.post_order(o, self._OT.GTC)
            return ExecutionResult(not r.get("errorMsg"), p, pos.size_usd, r.get("errorMsg",""))
        except Exception as e: return ExecutionResult(False, reason=str(e))

    def get_account_balance(self) -> dict:
        """
        Fetch real account balance:
        - USDC cash: read directly from Polygon blockchain (always accurate)
        - Portfolio value: sum currentValue from user's open positions
        - Total PnL: sum cashPnl from open positions
        Uses multiple RPC fallbacks so one failure doesn't block the others.
        """
        DATA_API  = "https://data-api.polymarket.com"
        pf        = os.getenv("POLYMARKET_PROXY_FUNDER", "")
        if not pf:
            return {"usdc_balance": 0, "portfolio_value": 0, "total_value": 0, "total_pnl": 0, "open_count": 0}

        # ── 1. USDC cash balance — read on-chain from Polygon ──
        USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
        USDC_ABI = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
                     "type": "function"}]
        POLYGON_RPCS = [
            "https://polygon-bor-rpc.publicnode.com",   # confirmed working
            "https://rpc-mainnet.maticvigil.com",
            "https://polygon.drpc.org",
            "https://1rpc.io/matic",
            "https://polygon.llamarpc.com",
        ]
        usdc = 0.0
        for rpc_url in POLYGON_RPCS:
            try:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 6}))
                if not w3.is_connected():
                    continue
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_CONTRACT), abi=USDC_ABI)
                raw = contract.functions.balanceOf(
                    Web3.to_checksum_address(pf)).call()
                usdc = raw / 1e6   # USDC.e has 6 decimals
                log.info(f"USDC balance fetched on-chain: ${usdc:.2f}")
                break
            except Exception as e:
                log.debug(f"RPC {rpc_url} failed: {e}")
                continue

        # ── 2. Portfolio value + All-Time PnL ────────────────────────
        portfolio_value = 0.0
        total_pnl       = 0.0
        open_count      = 0
        try:
            # Open positions — currentValue and cashPnl
            r = requests.get(
                f"{DATA_API}/positions",
                params={"user": pf, "sizeThreshold": 0, "limit": 100},
                timeout=8,
            )
            r.raise_for_status()
            open_pos        = r.json() if isinstance(r.json(), list) else []
            portfolio_value = sum(float(p.get("currentValue", 0) or 0) for p in open_pos)
            open_pnl        = sum(float(p.get("cashPnl", 0) or 0) for p in open_pos)
            open_count      = len(open_pos)

            # Closed positions — realizedPnl (correct endpoint + field)
            r2 = requests.get(
                f"{DATA_API}/closed-positions",
                params={"user": pf, "limit": 500},
                timeout=8,
            )
            r2.raise_for_status()
            closed_pos  = r2.json() if isinstance(r2.json(), list) else []
            closed_pnl  = sum(float(p.get("realizedPnl", 0) or 0) for p in closed_pos)

            # All-time PnL = open unrealised + closed realised
            total_pnl = open_pnl + closed_pnl
            log.info(f"Portfolio: ${portfolio_value:.2f} | Open PnL: ${open_pnl:.2f} | "
                     f"Closed PnL: ${closed_pnl:.2f} | All-Time PnL: ${total_pnl:.2f}")
        except Exception as e:
            log.debug(f"Portfolio fetch failed: {e}")

        return {
            "usdc_balance":    round(usdc, 2),
            "portfolio_value": round(portfolio_value, 2),
            "total_value":     round(usdc + portfolio_value, 2),
            "total_pnl":       round(total_pnl, 2),
            "open_count":      open_count,
        }

    def get_pnl_history(self, window: str = "1w") -> list:
        """
        Fetch PnL history for the account.
        window: '1d' = 24h, '1w' = 7 days, '1m' = 30 days
        """
        DATA_API = "https://data-api.polymarket.com"
        pf = os.getenv("POLYMARKET_PROXY_FUNDER", "")
        if not pf:
            return []
        window_map = {"1d": "1d", "7d": "1w", "30d": "1m"}
        api_window = window_map.get(window, "1w")
        try:
            r = requests.get(
                f"{DATA_API}/portfolio-history",
                params={"user": pf, "window": api_window},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            history = data if isinstance(data, list) else data.get("history", [])
            return [
                {
                    "time": h.get("t", h.get("timestamp", "")),
                    "value": round(float(h.get("v", h.get("value", 0)) or 0), 2),
                }
                for h in history
            ]
        except Exception as e:
            log.warning(f"get_pnl_history({window}) failed: {e}")
            return []

    def get_closed_positions(self) -> list:
        """Fetch recently closed/resolved positions for the live account tab."""
        DATA_API = "https://data-api.polymarket.com"
        pf = os.getenv("POLYMARKET_PROXY_FUNDER", "")
        if not pf:
            return []
        try:
            r = requests.get(
                f"{DATA_API}/positions",
                params={"user": pf, "sizeThreshold": 0, "redeemable": True, "limit": 20},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json() if isinstance(r.json(), list) else []
            return [
                {
                    "market_name": p.get("title", "Unknown"),
                    "outcome":     p.get("outcome", ""),
                    "size":        round(float(p.get("size", 0) or 0), 4),
                    "avg_price":   round(float(p.get("avgPrice", 0) or 0), 4),
                    "pnl":         round(float(p.get("cashPnl", 0) or 0), 2),
                    "pnl_pct":     round(float(p.get("percentPnl", 0) or 0), 2),
                    "end_date":    p.get("endDate", ""),
                    "redeemable":  p.get("redeemable", False),
                }
                for p in data
            ]
        except Exception as e:
            log.warning(f"get_closed_positions failed: {e}")
            return []
        if not self._connected:
            log.warning("cancel_all_orders: not connected — skipping")
            return
        try:
            resp = self.client.cancel_all()
            log.warning(f"All orders cancelled: {resp}")
        except Exception as e:
            log.error(f"cancel_all_orders failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# 9. BOT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class PolymarketCopyBot:
    def __init__(self, config):
        self.config = config
        self.client = PolymarketClient(config)
        self.ranker = TraderRanker(config)
        self.risk = RiskManager(config)
        self.sizer = PositionSizer(config)
        self.profit_mgr = ProfitManager(config)
        self.portfolio = Portfolio(config.starting_capital)
        self.last_rebalance = datetime.now()
        self.running = True
        self.activity_log: deque = deque(maxlen=100)

    def _log_activity(self, action, market, trader, checks=None, size=0):
        self.activity_log.appendleft({
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": action, "market": market[:55],
            "trader": trader, "checks": checks, "size": round(size, 2),
        })

    def run(self):
        mode = "PAPER" if self.config.paper_mode else "LIVE"
        log.info(f"Bot started [{mode}] | Capital: ${self.portfolio.total_capital:,.2f}")
        while self.running:
            try:
                self._cycle()
                time.sleep(self.config.poll_interval_seconds)
            except KeyboardInterrupt:
                log.info("Stopped."); break
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True); time.sleep(60)

    def _cycle(self):
        if (datetime.now() - self.last_rebalance) >= timedelta(days=self.config.rebalance_interval_days):
            self.ranker.rerank(); self.last_rebalance = datetime.now()

        # Fetch what the followed traders currently hold (read-only, works in paper mode)
        self.client.get_trader_positions(self.ranker.active_traders())

        for trade in self.client.get_recent_trades(self.ranker.active_traders()):
            self._evaluate(trade)

        for pos in list(self.portfolio.open_positions):
            cp = self.client.get_price(pos.market_id)
            pnl = (cp - pos.entry_price) / pos.entry_price
            if pnl <= -self.config.stop_loss_pct:
                log.warning(f"STOP-LOSS: {pos.market_id}")
                self.client.close_position(pos)
                self.portfolio.close_position(pos, cp)
                self._log_activity("STOP-LOSS", pos.market_name, pos.trader_id, size=pos.size_usd)

        self.profit_mgr.check_triggers(self.portfolio)
        self.portfolio._record_equity()

    def _evaluate(self, trade):
        checks = {
            "trader_healthy": self.ranker.is_healthy(trade.trader_id),
            "slippage_ok":    self.risk.check_slippage(trade),
            "correlation_ok": self.risk.check_correlation(trade, self.portfolio),
            "insider_ok":     self.risk.insider_score(trade) < 5,
            "liquidity_ok":   trade.market_liquidity >= self.config.min_market_liquidity,
            "odds_ok":        0.05 <= trade.implied_prob <= 0.95,
        }
        passed = sum(checks.values())
        if passed == 6:
            size = self.sizer.calculate(trade, self.portfolio)
            self._execute(trade, size, passed)
        elif passed >= 5:
            size = self.sizer.calculate(trade, self.portfolio) * 0.5
            self._execute(trade, size, passed)
        else:
            self._log_activity("SKIP", trade.market_name, trade.trader_id, passed)

    def _execute(self, trade, size, checks):
        if size < self.config.min_trade_size:
            self._log_activity("SKIP", trade.market_name, trade.trader_id, checks); return
        if not self.config.paper_mode: time.sleep(self.config.copy_delay_seconds)
        res = self.client.execute_split_order(
            trade.market_id, trade.side, size,
            self.config.order_split_chunks, self.config.chunk_delay_seconds, self.config.max_acceptable_slippage)
        if res.success:
            self.portfolio.add_position(trade, size, res.avg_price)
            action = "EXECUTE" if checks == 6 else "REDUCED"
            self._log_activity(action, trade.market_name, trade.trader_id, checks, size)
        else:
            self._log_activity("FAILED", trade.market_name, trade.trader_id, checks)

    def state(self):
        return {
            "running":          self.running,
            "mode":             "PAPER" if self.config.paper_mode else "LIVE",
            "connected":        self.client._connected,
            "portfolio":        self.portfolio.to_dict(),
            "traders":          self.ranker.to_dict(),
            "profit":           self.profit_mgr.to_dict(),
            "activity":         list(self.activity_log),
            "exposure":         self.risk.category_exposure(self.portfolio),
            "last_update":      datetime.now().strftime("%H:%M:%S"),
            "trader_positions": self.client._trader_positions_cache,
            "account":          self.client.get_account_balance(),
        }

# ══════════════════════════════════════════════════════════════════════════════
# 10. FLASK DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>POLYBOT Dashboard</title>
<style>
:root {
  --g:#00ff88;--b:#63b3ed;--y:#f6c90e;--r:#ff4d6d;--p:#a78bfa;
  --bg:#060709;--bg2:#0a0d11;--bg3:#0f1318;
  --border:rgba(0,255,136,0.1);--text:#c8d6e5;--dim:#3d5166;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace;min-height:100vh;overflow-x:hidden;}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,255,136,0.012) 3px,rgba(0,255,136,0.012) 4px);pointer-events:none;z-index:9999;}
.gridbg{position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,136,0.025) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,0.025) 1px,transparent 1px);background-size:48px 48px;pointer-events:none;}

/* ── Header ── */
header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:12px 20px;border-bottom:1px solid var(--border);background:rgba(6,7,9,0.97);position:sticky;top:0;z-index:100;backdrop-filter:blur(12px);}
.hdr-l{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
.hdr-r{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
.logo{font-weight:900;font-size:14px;letter-spacing:5px;color:var(--g);text-shadow:0 0 20px rgba(0,255,136,0.4);}
.spill{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:2px;}
.dot{width:8px;height:8px;border-radius:50%;background:var(--g);box-shadow:0 0 8px var(--g);animation:pulse 2s ease-in-out infinite;flex-shrink:0;}
.dot.off{background:var(--r);box-shadow:0 0 8px var(--r);}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.5;transform:scale(1.6)}}
.hv{text-align:right;}
.hv .big{font-size:17px;font-weight:700;}
.hv .small{font-size:9px;color:var(--dim);letter-spacing:2px;}
.btn{padding:7px 14px;font-family:monospace;font-size:10px;letter-spacing:2px;cursor:pointer;border-radius:2px;border:1px solid;transition:all 0.2s;white-space:nowrap;}
.btn-stop{background:rgba(255,77,109,0.1);color:var(--r);border-color:rgba(255,77,109,0.3);}
.btn-start{background:rgba(0,255,136,0.1);color:var(--g);border-color:rgba(0,255,136,0.3);}
.btn-stop:hover{background:rgba(255,77,109,0.2);}
.btn-start:hover{background:rgba(0,255,136,0.2);}

/* ── Mode banner ── */
.mode-banner{text-align:center;padding:7px;font-size:10px;letter-spacing:3px;}
.mode-banner.paper{background:rgba(246,201,14,0.08);color:var(--y);border-bottom:1px solid rgba(246,201,14,0.15);}
.mode-banner.live{background:rgba(0,255,136,0.08);color:var(--g);border-bottom:1px solid rgba(0,255,136,0.15);}

/* ── Main tabs (top-level navigation) ── */
.main-tabs{display:flex;gap:0;border-bottom:1px solid var(--border);background:var(--bg2);overflow-x:auto;}
.main-tab{padding:12px 24px;font-size:10px;letter-spacing:2px;cursor:pointer;border-bottom:2px solid transparent;color:var(--dim);white-space:nowrap;transition:all 0.2s;text-transform:uppercase;}
.main-tab:hover{color:var(--text);}
.main-tab.active{color:var(--g);border-bottom-color:var(--g);}

/* ── Layout ── */
.main{padding:16px 20px;max-width:1440px;margin:0 auto;}
.tab-panel{display:none;}
.tab-panel.active{display:block;}

/* ── Cards ── */
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:14px;}
@media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr);}}
@media(max-width:600px){.cards{grid-template-columns:repeat(2,1fr);}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:2px;padding:14px 16px;position:relative;overflow:hidden;animation:fadeUp 0.4s ease both;}
.card::after{content:'';position:absolute;top:0;right:0;width:3px;height:100%;background:linear-gradient(180deg,var(--accent,#00ff88) 0%,transparent 100%);}
.card-label{font-size:9px;color:var(--dim);letter-spacing:3px;text-transform:uppercase;margin-bottom:8px;}
.card-val{font-size:20px;font-weight:700;color:var(--accent,#00ff88);line-height:1;word-break:break-all;}
.card-sub{font-size:10px;color:var(--dim);margin-top:6px;}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

/* ── Section ── */
.section{background:var(--bg2);border:1px solid var(--border);border-radius:2px;overflow:hidden;margin-bottom:14px;}
.sec-head{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;padding:10px 18px;border-bottom:1px solid rgba(255,255,255,0.04);font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--dim);}
.sec-head .hl{color:var(--g);}
.collapse-btn{background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;padding:0 4px;line-height:1;}
.collapse-btn:hover{color:var(--text);}
.collapsible-body{transition:max-height 0.3s ease;overflow:hidden;}
.collapsible-body.collapsed{max-height:0!important;}

/* ── Chart ── */
#chart-wrap{padding:14px 4px 8px;height:150px;position:relative;}
canvas#equityChart{width:100%!important;height:100%!important;}

/* ── PnL timeframe tabs ── */
.pnl-tabs{display:flex;gap:6px;}
.pnl-tab{padding:4px 10px;font-size:9px;letter-spacing:2px;cursor:pointer;border-radius:2px;border:1px solid rgba(255,255,255,0.08);color:var(--dim);transition:all 0.2s;font-family:monospace;}
.pnl-tab.active{color:var(--g);border-color:rgba(0,255,136,0.3);background:rgba(0,255,136,0.07);}

/* ── Trader columns grid ── */
.trader-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:14px 18px;}
@media(max-width:900px){.trader-grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:500px){.trader-grid{grid-template-columns:1fr;}}
.trader-card{background:var(--bg3);border:1px solid rgba(255,255,255,0.06);border-radius:2px;padding:14px;position:relative;transition:border-color 0.2s;}
.trader-card:hover{border-color:rgba(0,255,136,0.2);}
.trader-card.active-trader{border-color:rgba(0,255,136,0.25);}
.tc-addr{font-size:10px;color:var(--text);margin-bottom:6px;word-break:break-all;}
.tc-alloc{font-size:9px;color:var(--dim);margin-bottom:10px;}
.tc-metric{display:flex;justify-content:space-between;font-size:10px;margin-bottom:5px;}
.tc-metric .label{color:var(--dim);}
.tc-metric .val{font-weight:700;}
.tc-bar{height:3px;background:#111820;border-radius:2px;margin-top:8px;}
.tc-bar-fill{height:100%;border-radius:2px;transition:width 0.8s ease;}
.tc-badge{position:absolute;top:10px;right:10px;font-size:8px;padding:2px 6px;border-radius:2px;border:1px solid;letter-spacing:1px;}
.tc-badge.HEALTHY{background:rgba(0,255,136,0.08);color:var(--g);border-color:rgba(0,255,136,0.2);}
.tc-badge.WARNING{background:rgba(246,201,14,0.08);color:var(--y);border-color:rgba(246,201,14,0.2);}
.tc-badge.SUSPENDED{background:rgba(255,77,109,0.08);color:var(--r);border-color:rgba(255,77,109,0.2);}

/* ── Tier bar ── */
.tier-wrap{padding:16px 18px;}
.tier-track{height:6px;background:#111820;border-radius:3px;position:relative;margin-bottom:20px;overflow:visible;}
.tier-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--g),var(--b));transition:width 1s ease;}
.tier-dot{position:absolute;top:50%;transform:translate(-50%,-50%);width:12px;height:12px;border-radius:50%;border:2px solid;transition:all 0.4s;}
.tier-dot.hit{background:var(--g);border-color:var(--g);box-shadow:0 0 8px var(--g);}
.tier-dot.miss{background:var(--bg);border-color:var(--dim);}
.tiers-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;}
@media(max-width:500px){.tiers-grid{grid-template-columns:repeat(2,1fr);}}
.tier-card{padding:10px;text-align:center;border-radius:2px;border:1px solid;transition:all 0.3s;}
.tier-card.hit{background:rgba(0,255,136,0.07);border-color:rgba(0,255,136,0.25);color:var(--g);}
.tier-card.miss{background:rgba(255,255,255,0.02);border-color:rgba(255,255,255,0.06);color:var(--dim);}
.tier-label{font-size:13px;font-weight:700;}
.tier-status{font-size:9px;margin-top:4px;letter-spacing:1px;}

/* ── Two col ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;}
@media(max-width:700px){.two-col{grid-template-columns:1fr;}}

/* ── Exposure bars ── */
.exp-wrap{padding:14px 18px;}
.exp-row{margin-bottom:12px;}
.exp-meta{display:flex;justify-content:space-between;margin-bottom:5px;font-size:11px;}
.exp-bar-bg{height:4px;background:#111820;border-radius:2px;}
.exp-bar-fill{height:100%;border-radius:2px;transition:width 0.8s ease;}

/* ── Tables ── */
.tbl-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
.tbl-head{display:grid;gap:8px;padding:7px 18px;font-size:9px;letter-spacing:2px;color:var(--dim);border-bottom:1px solid rgba(255,255,255,0.04);text-transform:uppercase;min-width:480px;}
.tbl-row{display:grid;gap:8px;padding:10px 18px;border-bottom:1px solid rgba(255,255,255,0.03);transition:background 0.15s;font-size:11px;align-items:center;min-width:480px;}
.tbl-row:hover{background:rgba(0,255,136,0.025);}

/* ── Badges ── */
.badge{font-size:9px;padding:2px 7px;border-radius:2px;letter-spacing:1px;border:1px solid;white-space:nowrap;}
.badge-HEALTHY{background:rgba(0,255,136,0.1);color:var(--g);border-color:rgba(0,255,136,0.25);}
.badge-WARNING{background:rgba(246,201,14,0.1);color:var(--y);border-color:rgba(246,201,14,0.25);}
.badge-SUSPENDED{background:rgba(255,77,109,0.1);color:var(--r);border-color:rgba(255,77,109,0.25);}

/* ── Action colours ── */
.act-EXECUTE{color:var(--g);} .act-REDUCED{color:var(--y);} .act-SKIP{color:var(--dim);}
.act-LOCK{color:var(--b);} .act-STOP-LOSS,.act-FAILED,.act-EMERGENCY\ STOP{color:var(--r);}
.act-MANUAL\ CLOSE{color:var(--y);} .act-TRADER\ ADDED{color:var(--g);} .act-TRADER\ REMOVED{color:var(--r);}
.act-CLOSE{color:var(--y);}

/* ── Sub-tabs ── */
.tabs{display:flex;gap:6px;flex-wrap:wrap;}
.tab{padding:5px 12px;font-family:monospace;font-size:9px;letter-spacing:2px;cursor:pointer;border-radius:2px;border:1px solid transparent;text-transform:uppercase;transition:all 0.2s;color:var(--dim);white-space:nowrap;}
.tab.active{color:var(--g);background:rgba(0,255,136,0.07);border-color:rgba(0,255,136,0.25);}

/* ── Cal bar ── */
.cal-bar{height:4px;background:#111820;border-radius:2px;margin-top:4px;}
.cal-fill{height:100%;background:var(--g);border-radius:2px;}

/* ── Trader management inputs ── */
.trader-inputs{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px;}
.trader-inputs input{background:#0a0d11;border:1px solid rgba(0,255,136,0.2);color:var(--text);padding:9px 12px;font-family:monospace;font-size:11px;border-radius:2px;outline:none;}
.trader-inputs input:focus{border-color:rgba(0,255,136,0.5);}
.input-addr{flex:1;min-width:160px;} .input-alloc{width:80px;} .input-label{width:140px;}
.manage-row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid rgba(255,255,255,0.04);gap:10px;flex-wrap:wrap;}
.manage-row-info{display:flex;align-items:center;gap:12px;flex:1;min-width:0;}
.manage-row-addr{font-size:10px;color:var(--text);word-break:break-all;}
.manage-row-meta{font-size:9px;color:var(--dim);margin-top:2px;}

/* ── Account overview ── */
.account-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:16px 18px;}
@media(max-width:800px){.account-grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:400px){.account-grid{grid-template-columns:1fr;}}
.account-card{background:var(--bg3);border:1px solid rgba(255,255,255,0.06);border-radius:2px;padding:14px;}
.account-card .label{font-size:9px;color:var(--dim);letter-spacing:3px;text-transform:uppercase;margin-bottom:8px;}
.account-card .value{font-size:22px;font-weight:700;line-height:1;}
.account-card .sub{font-size:10px;color:var(--dim);margin-top:6px;}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-track{background:var(--bg);}
::-webkit-scrollbar-thumb{background:#1e2d3d;}

/* ── Modal ── */
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:1000;align-items:center;justify-content:center;padding:16px;}
.modal-box{background:#0a0d11;border:1px solid rgba(0,255,136,0.2);border-radius:2px;padding:24px 28px;max-width:420px;width:100%;}
.modal-title{font-size:13px;color:var(--text);margin-bottom:10px;letter-spacing:1px;}
.modal-msg{font-size:11px;color:var(--dim);margin-bottom:20px;line-height:1.6;}
.modal-btns{display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;}
</style>
</head>
<body>
<div class="gridbg"></div>
<div id="mode-banner" class="mode-banner paper">⬡ PAPER MODE — NO REAL ORDERS</div>

<header>
  <div class="hdr-l">
    <div class="logo">POLYBOT</div>
    <div style="width:1px;height:18px;background:var(--border)"></div>
    <div class="spill">
      <div class="dot" id="status-dot"></div>
      <span id="status-text" style="color:var(--g)">RUNNING</span>
    </div>
    <div style="font-size:9px;color:var(--dim);letter-spacing:1px">SYNC <span id="last-update">—</span></div>
  </div>
  <div class="hdr-r">
    <div class="hv">
      <div class="small">USDC BALANCE</div>
      <div class="big" id="hdr-usdc" style="color:var(--b)">$—</div>
    </div>
    <div class="hv">
      <div class="small">PORTFOLIO</div>
      <div class="big" id="hdr-total" style="color:var(--g)">$10</div>
    </div>
    <div class="hv">
      <div class="small">ROI</div>
      <div class="big" id="hdr-roi" style="color:var(--g)">0.00%</div>
    </div>
    <button class="btn btn-stop" id="toggle-btn" onclick="toggleBot()">STOP BOT</button>
  </div>
</header>

<!-- ── MAIN NAV TABS ── -->
<div class="main-tabs">
  <div class="main-tab active" id="mtab-overview" onclick="setMainTab('overview')">Overview</div>
  <div class="main-tab" id="mtab-account"  onclick="setMainTab('account')">Live Account</div>
  <div class="main-tab" id="mtab-traders"  onclick="setMainTab('traders')">Traders</div>
  <div class="main-tab" id="mtab-risk"     onclick="setMainTab('risk')">Risk & PnL</div>
  <div class="main-tab" id="mtab-controls" onclick="setMainTab('controls')">Controls</div>
</div>

<div class="main">

<!-- ══════════════════════════════════════════════════
     TAB 1 — OVERVIEW
════════════════════════════════════════════════════ -->
<div class="tab-panel active" id="panel-overview">

  <!-- Stat Cards -->
  <div class="cards">
    <div class="card" style="--accent:var(--g);animation-delay:0s">
      <div class="card-label">Total ROI</div>
      <div class="card-val" id="c-roi">0.00%</div>
      <div class="card-sub" id="c-roi-sub">Since start</div>
    </div>
    <div class="card" style="--accent:var(--b);animation-delay:0.05s">
      <div class="card-label">USDC Balance</div>
      <div class="card-val" id="c-usdc">$—</div>
      <div class="card-sub">Available to trade</div>
    </div>
    <div class="card" style="--accent:var(--b);animation-delay:0.08s">
      <div class="card-label">Portfolio Value</div>
      <div class="card-val" id="c-portval">$—</div>
      <div class="card-sub">Open positions</div>
    </div>
    <div class="card" style="--accent:var(--y);animation-delay:0.1s">
      <div class="card-label">Locked Stables</div>
      <div class="card-val" id="c-locked">$0</div>
      <div class="card-sub">20/30 rule</div>
    </div>
    <div class="card" style="--accent:var(--p);animation-delay:0.15s">
      <div class="card-label">Open Positions</div>
      <div class="card-val" id="c-pos">0</div>
      <div class="card-sub" id="c-pos-sub">$0 deployed</div>
    </div>
    <div class="card" style="--accent:var(--g);animation-delay:0.2s">
      <div class="card-label">Active Traders</div>
      <div class="card-val" id="c-traders">4</div>
      <div class="card-sub">Followed wallets</div>
    </div>
  </div>

  <!-- Equity Chart -->
  <div class="section">
    <div class="sec-head">
      <span>Equity Curve</span>
      <div style="display:flex;align-items:center;gap:12px">
        <div class="pnl-tabs">
          <div class="pnl-tab active" id="pt-1d"  onclick="setPnlTab('1d')">24H</div>
          <div class="pnl-tab"        id="pt-7d"  onclick="setPnlTab('7d')">7D</div>
          <div class="pnl-tab"        id="pt-30d" onclick="setPnlTab('30d')">30D</div>
        </div>
        <span class="hl" id="chart-roi">+0.00%</span>
      </div>
    </div>
    <div id="chart-wrap"><canvas id="equityChart"></canvas></div>
  </div>

  <!-- Positions / Log tabs -->
  <div class="section">
    <div class="sec-head">
      <div class="tabs">
        <div class="tab active" id="tab-pos" onclick="setSubTab('positions')">My Positions <span id="pos-count">(0)</span></div>
        <div class="tab" id="tab-log" onclick="setSubTab('log')">Activity Log</div>
      </div>
      <span style="color:var(--dim)">Stop-loss: −40%</span>
    </div>
    <div id="panel-positions">
      <div class="tbl-scroll">
        <div class="tbl-head" style="grid-template-columns:2fr 50px 65px 65px 70px 80px 90px 60px">
          <span>Market</span><span>Side</span><span style="text-align:right">Entry</span>
          <span style="text-align:right">Current</span><span style="text-align:right">Size</span>
          <span style="text-align:right">P&L</span><span style="text-align:right">Trader</span>
          <span style="text-align:right">Action</span>
        </div>
        <div id="pos-body"><div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">No open positions</div></div>
      </div>
    </div>
    <div id="panel-log" style="display:none">
      <div class="tbl-scroll">
        <div class="tbl-head" style="grid-template-columns:70px 80px 1fr 80px">
          <span>Time</span><span>Action</span><span>Market</span><span style="text-align:right">Info</span>
        </div>
        <div id="log-body"><div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">No activity yet</div></div>
      </div>
    </div>
  </div>

</div><!-- end overview -->

<!-- ══════════════════════════════════════════════════
     TAB 2 — LIVE ACCOUNT
════════════════════════════════════════════════════ -->
<div class="tab-panel" id="panel-account">

  <!-- Account balance cards -->
  <div class="section" style="margin-bottom:14px">
    <div class="sec-head"><span>My Account</span><span id="acct-status" style="color:var(--dim)">Loading...</span></div>
    <div class="account-grid">
      <div class="account-card">
        <div class="label">USDC Balance</div>
        <div class="value" id="acct-usdc" style="color:var(--b)">$—</div>
        <div class="sub">Available to trade</div>
      </div>
      <div class="account-card">
        <div class="label">Portfolio Value</div>
        <div class="value" id="acct-portval" style="color:var(--g)">$—</div>
        <div class="sub">Open positions value</div>
      </div>
      <div class="account-card">
        <div class="label">Total Value</div>
        <div class="value" id="acct-total" style="color:var(--g)">$—</div>
        <div class="sub">Balance + positions</div>
      </div>
      <div class="account-card">
        <div class="label">All-Time PnL</div>
        <div class="value" id="acct-pnl" style="color:var(--g)">$—</div>
        <div class="sub">Realised + unrealised</div>
      </div>
    </div>
  </div>

  <!-- Trader positions -->
  <div class="section" style="margin-bottom:14px">
    <div class="sec-head">
      <span>Followed Trader Positions</span>
      <span style="color:var(--dim)" id="pos-fetch-status">Fetching...</span>
    </div>
    <div id="trader-positions-body">
      <div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">Waiting for data...</div>
    </div>
  </div>

  <!-- Closed / redeemable positions -->
  <div class="section">
    <div class="sec-head"><span>Closed &amp; Redeemable</span><span style="color:var(--dim)">My resolved markets</span></div>
    <div class="tbl-scroll">
      <div class="tbl-head" style="grid-template-columns:2fr 60px 70px 70px 80px 80px">
        <span>Market</span><span>Outcome</span><span style="text-align:right">Avg Price</span>
        <span style="text-align:right">Size</span><span style="text-align:right">Cash PnL</span><span style="text-align:right">%PnL</span>
      </div>
      <div id="closed-pos-body"><div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">Loading...</div></div>
    </div>
  </div>

</div><!-- end account -->

<!-- ══════════════════════════════════════════════════
     TAB 3 — TRADERS
════════════════════════════════════════════════════ -->
<div class="tab-panel" id="panel-traders">

  <!-- Trader cards grid (collapsible) -->
  <div class="section" style="margin-bottom:14px">
    <div class="sec-head">
      <span>Followed Traders</span>
      <div style="display:flex;align-items:center;gap:10px">
        <span style="color:var(--dim)">Reranks weekly</span>
        <button class="collapse-btn" onclick="toggleCollapse('traders-grid-body')" title="Collapse">▲</button>
      </div>
    </div>
    <div class="collapsible-body" id="traders-grid-body">
      <div class="trader-grid" id="trader-cards"></div>
    </div>
  </div>

  <!-- Trader management -->
  <div class="section">
    <div class="sec-head">
      <span>Manage Traders</span>
      <button class="collapse-btn" onclick="toggleCollapse('trader-mgmt-body')" title="Collapse">▲</button>
    </div>
    <div class="collapsible-body" id="trader-mgmt-body">
      <div style="padding:16px 18px">
        <div class="trader-inputs">
          <input id="new-trader-addr"  type="text"   placeholder="0x... wallet address" class="input-addr"/>
          <input id="new-trader-alloc" type="number" placeholder="Alloc %" min="1" max="100" value="10" class="input-alloc"/>
          <input id="new-trader-label" type="text"   placeholder="Label (optional)" class="input-label"/>
          <button class="btn btn-start" onclick="addTrader()" style="padding:9px 18px;font-size:10px">+ ADD TRADER</button>
        </div>
        <div id="manage-traders-body"><div style="color:var(--dim);font-size:11px">Loading...</div></div>
      </div>
    </div>
  </div>

</div><!-- end traders -->

<!-- ══════════════════════════════════════════════════
     TAB 4 — RISK & PNL
════════════════════════════════════════════════════ -->
<div class="tab-panel" id="panel-risk">

  <!-- PnL chart with timeframe selector -->
  <div class="section" style="margin-bottom:14px">
    <div class="sec-head">
      <span>PnL History</span>
      <div class="pnl-tabs">
        <div class="pnl-tab active" id="pnl-1d"  onclick="loadPnlChart('1d')">24H</div>
        <div class="pnl-tab"        id="pnl-7d"  onclick="loadPnlChart('7d')">7D</div>
        <div class="pnl-tab"        id="pnl-30d" onclick="loadPnlChart('30d')">30D</div>
      </div>
    </div>
    <div style="padding:14px 4px 8px;height:180px;position:relative;">
      <canvas id="pnlChart"></canvas>
    </div>
  </div>

  <!-- Two col: profit tiers + exposure -->
  <div class="two-col">
    <div class="section">
      <div class="sec-head"><span>20/30 Profit-Taking</span><span class="hl" id="locked-lbl">$0 locked</span></div>
      <div class="tier-wrap">
        <div class="tier-track">
          <div class="tier-fill" id="tier-fill" style="width:0%"></div>
          <div class="tier-dot miss" id="td-20" style="left:26.7%"></div>
          <div class="tier-dot miss" id="td-35" style="left:46.7%"></div>
          <div class="tier-dot miss" id="td-50" style="left:66.7%"></div>
          <div class="tier-dot miss" id="td-75" style="left:100%"></div>
        </div>
        <div class="tiers-grid">
          <div class="tier-card miss" id="tier-20"><div class="tier-label">+20%</div><div class="tier-status">PENDING</div></div>
          <div class="tier-card miss" id="tier-35"><div class="tier-label">+35%</div><div class="tier-status">PENDING</div></div>
          <div class="tier-card miss" id="tier-50"><div class="tier-label">+50%</div><div class="tier-status">PENDING</div></div>
          <div class="tier-card miss" id="tier-75"><div class="tier-label">+75%</div><div class="tier-status">PENDING</div></div>
        </div>
      </div>
    </div>
    <div class="section">
      <div class="sec-head"><span>Category Exposure</span><span style="color:var(--dim)">25% max</span></div>
      <div class="exp-wrap" id="exposure-wrap">
        <div style="color:var(--dim);font-size:11px;text-align:center;padding:20px 0">No open positions</div>
      </div>
    </div>
  </div>

</div><!-- end risk -->

<!-- ══════════════════════════════════════════════════
     TAB 5 — CONTROLS
════════════════════════════════════════════════════ -->
<div class="tab-panel" id="panel-controls">

  <!-- Emergency stop -->
  <div id="emergency-banner" style="display:none;margin-bottom:14px">
    <div style="background:rgba(255,77,109,0.08);border:1px solid rgba(255,77,109,0.3);border-radius:2px;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
      <div>
        <div style="color:var(--r);font-size:13px;letter-spacing:3px;font-weight:700">⚠ EMERGENCY STOP ACTIVE</div>
        <div style="font-size:11px;color:#718096;margin-top:4px">Bot halted. All orders cancelled.</div>
      </div>
      <button class="btn btn-start" onclick="resumeBot()">RESUME BOT</button>
    </div>
  </div>

  <div class="section" style="margin-bottom:14px">
    <div class="sec-head"><span>Emergency Controls</span><span style="color:var(--r)">Use with caution</span></div>
    <div style="padding:16px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <button class="btn" onclick="emergencyStop()" style="background:rgba(255,77,109,0.12);color:var(--r);border-color:rgba(255,77,109,0.4);padding:10px 24px;font-size:11px;letter-spacing:2px">
        🛑 EMERGENCY STOP + CANCEL ALL ORDERS
      </button>
      <div style="font-size:10px;color:var(--dim);max-width:400px">
        Immediately halts the bot and cancels all open orders. Existing positions are held.
      </div>
    </div>
  </div>

</div><!-- end controls -->

</div><!-- end .main -->

<!-- Modal -->
<div id="modal">
  <div class="modal-box">
    <div id="modal-title" class="modal-title"></div>
    <div id="modal-msg"   class="modal-msg"></div>
    <div class="modal-btns">
      <button class="btn" onclick="closeModal()" style="color:var(--dim);border-color:rgba(255,255,255,0.1)">CANCEL</button>
      <button class="btn" id="modal-confirm">CONFIRM</button>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────
let activeMainTab = 'overview';
let activeSubTab  = 'positions';
let activePnlTab  = '7d';
let pnlChart      = null;
let equityChart   = null;
let lastEquityData = [];
let modalCallback  = null;
let collapsedSections = {};

// ── Main tab navigation ────────────────────────────────────
function setMainTab(t) {
  ['overview','account','traders','risk','controls'].forEach(id => {
    document.getElementById('mtab-'+id).className   = 'main-tab' + (id===t?' active':'');
    document.getElementById('panel-'+id).className  = 'tab-panel' + (id===t?' active':'');
  });
  activeMainTab = t;
  if (t === 'account')  loadClosedPositions();
  if (t === 'risk')     loadPnlChart(activePnlTab);
}

// ── Sub-tab (positions / log) ──────────────────────────────
function setSubTab(t) {
  activeSubTab = t;
  document.getElementById('panel-positions').style.display = t==='positions'?'block':'none';
  document.getElementById('panel-log').style.display       = t==='log'?'block':'none';
  document.getElementById('tab-pos').className = 'tab'+(t==='positions'?' active':'');
  document.getElementById('tab-log').className = 'tab'+(t==='log'?' active':'');
}

// ── Collapsible sections ───────────────────────────────────
function toggleCollapse(id) {
  const el  = document.getElementById(id);
  const btn = el.previousElementSibling.querySelector('.collapse-btn');
  if (collapsedSections[id]) {
    el.style.maxHeight = el.scrollHeight + 'px';
    el.classList.remove('collapsed');
    if (btn) btn.textContent = '▲';
    collapsedSections[id] = false;
  } else {
    el.style.maxHeight = el.scrollHeight + 'px';
    requestAnimationFrame(() => { el.classList.add('collapsed'); });
    if (btn) btn.textContent = '▼';
    collapsedSections[id] = true;
  }
}

// ── PnL timeframe tabs ─────────────────────────────────────
function setPnlTab(t) {
  activePnlTab = t;
  ['1d','7d','30d'].forEach(id => {
    document.getElementById('pt-'+id).className = 'pnl-tab'+(id===t?' active':'');
  });
  // Update equity chart with new window data
  loadEquityForWindow(t);
}

function loadEquityForWindow(window) {
  fetch('/api/pnl_history?window='+window)
    .then(r=>r.json())
    .then(data => {
      if (data && data.length > 0) {
        drawEquityChart(data.map(d=>({time:String(d.time||'').substring(0,16), value:d.value})));
      }
    }).catch(()=>{});
}

function loadPnlChart(window) {
  activePnlTab = window;
  ['1d','7d','30d'].forEach(id => {
    document.getElementById('pnl-'+id).className = 'pnl-tab'+(id===window?' active':'');
  });
  fetch('/api/pnl_history?window='+window)
    .then(r=>r.json())
    .then(data => {
      if (data && data.length > 0) {
        drawPnlChart(data.map(d=>({time:String(d.time||'').substring(0,16), value:d.value})));
      } else {
        drawPnlChart(lastEquityData); // fallback to local equity data
      }
    }).catch(()=>{ drawPnlChart(lastEquityData); });
}

function loadClosedPositions() {
  fetch('/api/closed_positions')
    .then(r=>r.json())
    .then(data => renderClosedPositions(data))
    .catch(()=>{});
}

// ── Formatting helpers ─────────────────────────────────────
function fmt(n) {
  if (Math.abs(n)>=1e6) return '$'+(n/1e6).toFixed(1)+'M';
  if (Math.abs(n)>=1e3) return '$'+(n/1e3).toFixed(0)+'K';
  return '$'+n.toFixed(0);
}
function fmtPct(n) { return (n>=0?'+':'')+n.toFixed(2)+'%'; }

// ── Canvas chart (shared draw logic) ──────────────────────
function drawLineChart(canvasId, data, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || data.length < 2) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth; const H = canvas.offsetHeight;
  canvas.width = W; canvas.height = H;
  if (!W || !H) return;
  const vals = data.map(d=>d.value);
  const minV = Math.min(...vals); const maxV = Math.max(...vals);
  const pad = {t:10,r:10,b:28,l:52};
  const cw = W-pad.l-pad.r; const ch = H-pad.t-pad.b;
  const range = maxV-minV || 1;
  const xStep = cw/(data.length-1);
  const yScale = v => pad.t + ch - ((v-minV)/range)*ch;

  ctx.clearRect(0,0,W,H);

  // Grid lines
  ctx.strokeStyle='rgba(255,255,255,0.04)'; ctx.lineWidth=1;
  [0,0.25,0.5,0.75,1].forEach(p => {
    const y = pad.t + ch*p;
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(W-pad.r,y); ctx.stroke();
    const v = maxV - (maxV-minV)*p;
    ctx.fillStyle='#2d3748'; ctx.font='9px monospace'; ctx.textAlign='right';
    ctx.fillText('$'+(v>=1000?(v/1000).toFixed(1)+'k':v.toFixed(0)), pad.l-4, y+3);
  });

  // Baseline
  const baseline = data[0].value;
  const baseY = yScale(baseline);
  ctx.strokeStyle='rgba(255,255,255,0.08)'; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad.l,baseY); ctx.lineTo(W-pad.r,baseY); ctx.stroke();
  ctx.setLineDash([]);

  // Fill
  const grad = ctx.createLinearGradient(0,pad.t,0,H-pad.b);
  grad.addColorStop(0, color+'33'); grad.addColorStop(1, color+'00');
  ctx.beginPath();
  data.forEach((d,i) => {
    const x=pad.l+i*xStep; const y=yScale(d.value);
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  });
  ctx.lineTo(pad.l+(data.length-1)*xStep, H-pad.b);
  ctx.lineTo(pad.l, H-pad.b);
  ctx.closePath(); ctx.fillStyle=grad; ctx.fill();

  // Line
  ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=2;
  data.forEach((d,i) => {
    const x=pad.l+i*xStep; const y=yScale(d.value);
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  });
  ctx.stroke();

  // X labels (first, mid, last)
  ctx.fillStyle='#2d3748'; ctx.font='9px monospace'; ctx.textAlign='center';
  [0, Math.floor(data.length/2), data.length-1].forEach(i => {
    if (data[i]) ctx.fillText(String(data[i].time).substring(11)||String(data[i].time).substring(0,10), pad.l+i*xStep, H-pad.b+14);
  });
}

function drawEquityChart(data) {
  if (data && data.length > 1) lastEquityData = data;
  drawLineChart('equityChart', lastEquityData.length>1?lastEquityData:data||[], '#00ff88');
  if (lastEquityData.length > 1) {
    const first = lastEquityData[0].value;
    const last  = lastEquityData[lastEquityData.length-1].value;
    const roi   = ((last-first)/first*100).toFixed(2);
    document.getElementById('chart-roi').textContent = (roi>=0?'+':'')+roi+'%';
    document.getElementById('chart-roi').style.color = roi>=0?'var(--g)':'var(--r)';
  }
}

function drawPnlChart(data) {
  if (!data || data.length < 2) return;
  const color = data[data.length-1].value >= data[0].value ? '#00ff88' : '#ff4d6d';
  drawLineChart('pnlChart', data, color);
}

window.addEventListener('resize', () => {
  if (lastEquityData.length) drawEquityChart();
  if (activePnlTab) drawPnlChart(lastEquityData);
});

// ── Bot toggle ─────────────────────────────────────────────
function toggleBot() {
  fetch('/api/toggle',{method:'POST'}).then(r=>r.json()).then(d=>updateStatusUI(d.running));
}
function updateStatusUI(running) {
  document.getElementById('status-dot').className    = 'dot'+(running?'':' off');
  document.getElementById('status-text').textContent  = running?'RUNNING':'STOPPED';
  document.getElementById('status-text').style.color  = running?'var(--g)':'var(--r)';
  document.getElementById('toggle-btn').textContent   = running?'STOP BOT':'START BOT';
  document.getElementById('toggle-btn').className     = 'btn '+(running?'btn-stop':'btn-start');
}

// ── Modal ──────────────────────────────────────────────────
function showModal(title,msg,label,style,cb) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-msg').textContent   = msg;
  const btn = document.getElementById('modal-confirm');
  btn.textContent = label; btn.style.cssText = style;
  modalCallback = cb;
  document.getElementById('modal').style.display = 'flex';
}
function closeModal() {
  document.getElementById('modal').style.display = 'none';
  modalCallback = null;
}
document.getElementById('modal-confirm').onclick = () => { if (modalCallback) modalCallback(); closeModal(); };

// ── Emergency stop ─────────────────────────────────────────
function emergencyStop() {
  showModal('🛑 EMERGENCY STOP',
    'Halt bot and cancel ALL open orders on Polymarket. Positions are held.',
    'CONFIRM EMERGENCY STOP',
    'color:var(--r);background:rgba(255,77,109,0.15);border:1px solid rgba(255,77,109,0.4);padding:8px 18px;font-family:monospace;font-size:10px;letter-spacing:2px;cursor:pointer;border-radius:2px',
    () => {
      fetch('/api/emergency_stop',{method:'POST'}).then(r=>r.json()).then(()=>{
        document.getElementById('emergency-banner').style.display='block';
        updateStatusUI(false); addToast('Emergency stop executed','red');
      });
    });
}
function resumeBot() {
  fetch('/api/toggle',{method:'POST'}).then(r=>r.json()).then(d=>{
    document.getElementById('emergency-banner').style.display='none';
    updateStatusUI(d.running); addToast('Bot resumed','green');
  });
}

// ── Close position ─────────────────────────────────────────
function closePosition(marketId, marketName, size) {
  showModal('Close Position',
    'Close "'+marketName+'" — $'+size+' position at current price?',
    'CLOSE POSITION',
    'color:var(--y);background:rgba(246,201,14,0.1);border:1px solid rgba(246,201,14,0.3);padding:8px 18px;font-family:monospace;font-size:10px;letter-spacing:2px;cursor:pointer;border-radius:2px',
    () => {
      fetch('/api/close_position',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({market_id:marketId})})
        .then(r=>r.json()).then(d=>{
          if (d.success) addToast('Closed: '+marketName.substring(0,30),'green');
          else addToast('Failed: '+(d.reason||'unknown'),'red');
        });
    });
}

// ── Add / remove trader ────────────────────────────────────
function addTrader() {
  const addr  = document.getElementById('new-trader-addr').value.trim();
  const alloc = parseFloat(document.getElementById('new-trader-alloc').value)/100;
  const label = document.getElementById('new-trader-label').value.trim()||addr.substring(0,12);
  if (!addr) { addToast('Enter a wallet address','red'); return; }
  if (alloc<=0||alloc>1) { addToast('Allocation must be 1–100%','red'); return; }
  fetch('/api/add_trader',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({trader_id:addr,allocation:alloc,label})})
    .then(r=>r.json()).then(d=>{
      if (d.success) { addToast('Trader added: '+label,'green');
        document.getElementById('new-trader-addr').value='';
        document.getElementById('new-trader-label').value=''; }
      else addToast('Failed: '+(d.reason||'unknown'),'red');
    });
}
function removeTrader(traderId, label) {
  showModal('Remove Trader','Stop following "'+label+'"? Open positions stay open.',
    'REMOVE TRADER',
    'color:var(--r);background:rgba(255,77,109,0.1);border:1px solid rgba(255,77,109,0.3);padding:8px 18px;font-family:monospace;font-size:10px;letter-spacing:2px;cursor:pointer;border-radius:2px',
    () => {
      fetch('/api/remove_trader',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({trader_id:traderId})})
        .then(r=>r.json()).then(d=>{
          if (d.success) addToast('Removed: '+label,'green');
          else addToast('Failed: '+(d.reason||'unknown'),'red');
        });
    });
}

// ── Toast ──────────────────────────────────────────────────
function addToast(msg,color) {
  const t=document.createElement('div');
  t.textContent=msg;
  const c=color==='green'?'rgba(0,255,136,0.3)':'rgba(255,77,109,0.3)';
  const fc=color==='green'?'var(--g)':'var(--r)';
  t.style.cssText=`position:fixed;bottom:24px;right:24px;z-index:2000;background:#0a0d11;
    border:1px solid ${c};color:${fc};padding:12px 20px;font-family:monospace;
    font-size:11px;border-radius:2px;letter-spacing:1px;box-shadow:0 4px 20px rgba(0,0,0,0.5);
    animation:fadeUp 0.3s ease;max-width:340px`;
  document.body.appendChild(t);
  setTimeout(()=>t.remove(),3500);
}
const style=document.createElement('style');
style.textContent='@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}';
document.head.appendChild(style);

// ── Render closed positions ────────────────────────────────
function renderClosedPositions(data) {
  const el = document.getElementById('closed-pos-body');
  if (!data || data.length===0) {
    el.innerHTML='<div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">No closed positions</div>';
    return;
  }
  el.innerHTML = data.map(p => {
    const pnlC = p.pnl>=0?'var(--g)':'var(--r)';
    const pctC = p.pnl_pct>=0?'var(--g)':'var(--r)';
    return `<div class="tbl-row" style="grid-template-columns:2fr 60px 70px 70px 80px 80px">
      <div style="font-size:11px;color:var(--text)">${p.market_name}</div>
      <div style="color:${p.outcome==='YES'?'var(--g)':'var(--y)'}">${p.outcome}</div>
      <div style="text-align:right;color:var(--dim)">${p.avg_price.toFixed(3)}</div>
      <div style="text-align:right;color:var(--text)">${p.size.toFixed(2)}</div>
      <div style="text-align:right;color:${pnlC}">${p.pnl>=0?'+':''}$${p.pnl.toFixed(2)}</div>
      <div style="text-align:right;color:${pctC}">${p.pnl_pct>=0?'+':''}${p.pnl_pct.toFixed(1)}%</div>
    </div>`;
  }).join('');
}

// ── Main render ────────────────────────────────────────────
function render(s) {
  const p   = s.portfolio;
  const roi = p.current_roi;
  const pnl = p.total_capital - p.starting_capital;
  const acc = s.account || {};

  // Header
  document.getElementById('hdr-total').textContent = '$'+p.total_capital.toLocaleString('en',{maximumFractionDigits:2});
  document.getElementById('hdr-roi').textContent   = fmtPct(roi);
  document.getElementById('hdr-roi').style.color   = roi>=0?'var(--g)':'var(--r)';
  document.getElementById('last-update').textContent = s.last_update;

  // USDC balance in header — show real value or clear indicator
  const usdcVal = acc.usdc_balance || 0;
  const portVal = acc.portfolio_value || 0;
  const totalVal = acc.total_value || 0;

  if (usdcVal > 0) {
    document.getElementById('hdr-usdc').textContent = '$'+usdcVal.toFixed(2);
    document.getElementById('hdr-usdc').style.color = 'var(--b)';
  } else {
    document.getElementById('hdr-usdc').textContent = s.connected ? '$0.00' : 'N/A';
    document.getElementById('hdr-usdc').style.color = 'var(--dim)';
  }

  // Portfolio value card — use real API value if available, else bot internal
  document.getElementById('c-usdc').textContent = usdcVal > 0 ? '$'+usdcVal.toFixed(2) : (s.connected?'$0.00':'N/A');
  document.getElementById('c-portval').textContent = portVal > 0
    ? '$'+portVal.toFixed(2)
    : '$'+p.total_capital.toLocaleString('en',{maximumFractionDigits:2});

  // Mode banner
  const banner = document.getElementById('mode-banner');
  banner.textContent = s.mode==='PAPER'?'⬡ PAPER MODE — NO REAL ORDERS PLACED':'⚡ LIVE MODE — REAL TRADES ACTIVE';
  banner.className   = 'mode-banner '+(s.mode==='PAPER'?'paper':'live');

  // Stat cards
  document.getElementById('c-roi').textContent     = fmtPct(roi);
  document.getElementById('c-roi').style.color     = roi>=0?'var(--g)':'var(--r)';
  document.getElementById('c-roi-sub').textContent = 'Started $'+p.starting_capital.toLocaleString();
  document.getElementById('c-locked').textContent  = fmt(s.profit.total_locked);
  document.getElementById('c-pos').textContent     = p.open_positions.length;
  document.getElementById('c-pos-sub').textContent = fmt(p.open_positions.reduce((a,x)=>a+x.size_usd,0))+' deployed';
  document.getElementById('c-traders').textContent = s.traders.filter(t=>t.health==='HEALTHY').length;

  // Account tab
  document.getElementById('acct-usdc').textContent    = usdcVal > 0 ? '$'+usdcVal.toFixed(2) : (s.connected?'$0.00':'Connect wallet');
  document.getElementById('acct-portval').textContent = portVal > 0 ? '$'+portVal.toFixed(2) : '$'+p.total_capital.toFixed(2);
  document.getElementById('acct-total').textContent   = totalVal > 0 ? '$'+totalVal.toFixed(2) : '$'+p.total_capital.toFixed(2);
  document.getElementById('acct-status').textContent  = s.connected ? (s.mode==='LIVE'?'LIVE':'Paper/Read-only') : 'Offline';
  document.getElementById('acct-status').style.color  = s.connected ? (s.mode==='LIVE'?'var(--g)':'var(--y)') : 'var(--r)';
  // Show total PnL if available
  if (acc.total_pnl !== undefined) {
    const pnlEl = document.getElementById('acct-pnl');
    if (pnlEl) {
      pnlEl.textContent = (acc.total_pnl>=0?'+':'')+'$'+acc.total_pnl.toFixed(2);
      pnlEl.style.color = acc.total_pnl>=0?'var(--g)':'var(--r)';
    }
  }

  // Equity chart (overview tab uses bot equity history)
  drawEquityChart(p.equity_history);

  // Profit tiers
  const progress = Math.min(roi/75,1)*100;
  document.getElementById('tier-fill').style.width = progress+'%';
  [20,35,50,75].forEach(t=>{
    const hit = s.profit.triggered_tiers.includes(t);
    document.getElementById('td-'+t).className   = 'tier-dot '+(hit?'hit':'miss');
    const card = document.getElementById('tier-'+t);
    card.className = 'tier-card '+(hit?'hit':'miss');
    card.querySelector('.tier-status').textContent = hit?'LOCKED ✓':'PENDING';
  });
  document.getElementById('locked-lbl').textContent = fmt(s.profit.total_locked)+' locked';

  // Exposure bars
  const expWrap = document.getElementById('exposure-wrap');
  const cats = Object.entries(s.exposure||{});
  if (!cats.length) {
    expWrap.innerHTML='<div style="color:var(--dim);font-size:11px;text-align:center;padding:20px 0">No open positions</div>';
  } else {
    const colors={politics:'var(--y)',crypto:'var(--b)',sports:'var(--g)',macro:'var(--p)',geo:'var(--r)',other:'var(--dim)'};
    expWrap.innerHTML=cats.map(([cat,pct])=>`
      <div class="exp-row">
        <div class="exp-meta"><span style="text-transform:capitalize">${cat}</span>
          <span style="color:${pct>20?'var(--r)':colors[cat]||'var(--g)'}">${pct}%</span></div>
        <div class="exp-bar-bg"><div class="exp-bar-fill" style="width:${Math.min(pct/25*100,100)}%;background:${pct>20?'var(--r)':colors[cat]||'var(--g)'}"></div></div>
      </div>`).join('');
  }

  // Trader CARDS (grid layout)
  const tcEl = document.getElementById('trader-cards');
  tcEl.innerHTML = s.traders.map(t => {
    const isActive = t.alloc > 0;
    const barW = Math.min(t.roi, 50) / 50 * 100;
    const barC = t.health==='HEALTHY'?'var(--g)':t.health==='WARNING'?'var(--y)':'var(--r)';
    const shortId = t.id.length > 20 ? t.id.substring(0,8)+'...'+t.id.substring(t.id.length-6) : t.id;
    return `<div class="trader-card ${isActive?'active-trader':''}">
      <span class="tc-badge ${t.health}">${t.health}</span>
      <div class="tc-addr">${shortId}</div>
      <div class="tc-alloc">${t.alloc}% allocation</div>
      <div class="tc-metric"><span class="label">ROI</span><span class="val" style="color:var(--g)">+${t.roi}%</span></div>
      <div class="tc-metric"><span class="label">30D ROI</span><span class="val" style="color:${t.roi_30d>=10?'var(--g)':'var(--r)'}">+${t.roi_30d}%</span></div>
      <div class="tc-metric"><span class="label">P&L</span><span class="val">${fmt(t.pnl)}</span></div>
      <div class="tc-metric"><span class="label">Efficiency</span><span class="val">${t.efficiency}</span></div>
      <div class="tc-metric"><span class="label">Calibration</span><span class="val">${t.calibration}%</span></div>
      <div class="tc-bar"><div class="tc-bar-fill" style="width:${barW}%;background:${barC}"></div></div>
    </div>`;
  }).join('');

  // Trader management list
  const mb = document.getElementById('manage-traders-body');
  mb.innerHTML = s.traders.map(t=>`
    <div class="manage-row">
      <div class="manage-row-info">
        <span class="badge badge-${t.health}">${t.health}</span>
        <div style="min-width:0">
          <div class="manage-row-addr">${t.id}</div>
          <div class="manage-row-meta">ROI: +${t.roi}% | Alloc: ${t.alloc}% | Score: ${t.score}</div>
        </div>
      </div>
      <button onclick="removeTrader('${t.id.replace(/'/g,"\\'")}','${t.id.replace(/'/g,"\\'")}')"
        style="background:rgba(255,77,109,0.08);color:var(--r);border:1px solid rgba(255,77,109,0.2);
        padding:4px 12px;font-size:9px;letter-spacing:1px;cursor:pointer;border-radius:2px;font-family:monospace;flex-shrink:0">
        REMOVE</button>
    </div>`).join('');

  // Positions
  document.getElementById('pos-count').textContent = '('+p.open_positions.length+')';
  if (!p.open_positions.length) {
    document.getElementById('pos-body').innerHTML='<div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">No open positions</div>';
  } else {
    document.getElementById('pos-body').innerHTML = p.open_positions.map(pos=>{
      const mid=pos.market_id.replace(/"/g,'&quot;');
      const name=pos.market_name.replace(/"/g,'&quot;');
      return `<div class="tbl-row" style="grid-template-columns:2fr 50px 65px 65px 70px 80px 90px 60px">
        <div><div style="font-size:11px;color:var(--text)">${pos.market_name}</div>
          <div style="font-size:9px;color:var(--dim);margin-top:2px">${pos.opened_at.substring(11,19)}</div></div>
        <div style="color:${pos.side==='YES'?'var(--g)':'var(--y)'}">${pos.side}</div>
        <div style="text-align:right;color:var(--dim)">${pos.entry_price.toFixed(3)}</div>
        <div style="text-align:right;color:var(--text)">—</div>
        <div style="text-align:right;color:var(--text)">${fmt(pos.size_usd)}</div>
        <div style="text-align:right;color:var(--dim)">—</div>
        <div style="text-align:right;font-size:10px;color:var(--dim)">${pos.trader_id.substring(0,10)}</div>
        <div style="text-align:right">
          <button onclick="closePosition('${mid}','${name}',${pos.size_usd})"
            style="background:rgba(255,77,109,0.1);color:var(--r);border:1px solid rgba(255,77,109,0.25);
            padding:3px 8px;font-size:9px;letter-spacing:1px;cursor:pointer;border-radius:2px;font-family:monospace">CLOSE</button>
        </div></div>`;
    }).join('');
  }

  // Activity log
  const allLog=[...s.activity,...p.trade_log].sort((a,b)=>b.time.localeCompare(a.time)).slice(0,50);
  if (!allLog.length) {
    document.getElementById('log-body').innerHTML='<div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">No activity yet</div>';
  } else {
    document.getElementById('log-body').innerHTML=allLog.map(e=>`
      <div class="tbl-row" style="grid-template-columns:70px 80px 1fr 80px">
        <div style="color:var(--dim)">${e.time}</div>
        <div><span class="act-${e.action}" style="font-size:10px;letter-spacing:1px">${e.action}</span></div>
        <div style="color:#718096;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e.market||e.market_name||'—'}</div>
        <div style="text-align:right;color:var(--dim);font-size:10px">
          ${e.size>0?fmt(e.size):(e.checks!=null?e.checks+'/6':'—')}</div>
      </div>`).join('');
  }

  // Trader positions (account tab)
  const tp=s.trader_positions||{};
  const tpWrap=document.getElementById('trader-positions-body');
  const statusEl=document.getElementById('pos-fetch-status');
  const wallets=Object.keys(tp);
  if (!wallets.length) {
    statusEl.textContent=s.connected?'Fetching...':'Add wallet credentials to .env';
    tpWrap.innerHTML='<div style="padding:20px;text-align:center;color:var(--dim);font-size:11px">'+(s.connected?'Waiting for fetch cycle...':'Connect wallet to see live positions')+'</div>';
  } else {
    const totalPos=wallets.reduce((a,w)=>a+(tp[w]||[]).length,0);
    statusEl.textContent=totalPos+' position'+(totalPos!==1?'s':'')+' across '+wallets.length+' wallet'+(wallets.length!==1?'s':'');
    tpWrap.innerHTML=wallets.map(wallet=>{
      const positions=tp[wallet]||[];
      const shortAddr=wallet.substring(0,6)+'...'+wallet.substring(wallet.length-4);
      const posHtml=positions.length===0
        ?'<div style="padding:10px 18px;font-size:11px;color:var(--dim)">No open positions</div>'
        :'<div class="tbl-scroll"><div class="tbl-head" style="grid-template-columns:2.5fr 50px 120px 70px 80px 100px">'
        +'<span>Market</span><span>Side</span><span style="text-align:right">Entry → Current</span>'
        +'<span style="text-align:right">Size</span><span style="text-align:right">Value</span><span style="text-align:right">PnL</span></div>'
        +positions.map(p=>{
          const pnlC=p.pnl>=0?'var(--g)':'var(--r)';
          return `<div class="tbl-row" style="grid-template-columns:2.5fr 50px 120px 70px 80px 100px">
            <div><div style="font-size:11px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.market_name}</div>
              <div style="font-size:9px;color:var(--dim);margin-top:2px">${p.end_date?'Ends '+p.end_date.substring(0,10):(p.market_id||'').substring(0,16)+'...'}</div></div>
            <div style="color:${p.side==='YES'?'var(--g)':'var(--y)'}">${p.side}</div>
            <div style="text-align:right;color:var(--dim)">${p.avg_price.toFixed(3)} → ${p.cur_price.toFixed(3)}</div>
            <div style="text-align:right;color:var(--text)">${p.size.toFixed(2)}</div>
            <div style="text-align:right;color:var(--text)">$${p.current_val.toFixed(2)}</div>
            <div style="text-align:right;color:${pnlC}">${p.pnl>=0?'+':''}$${p.pnl.toFixed(2)} (${p.pnl_pct>=0?'+':''}${p.pnl_pct.toFixed(1)}%)</div>
          </div>`;
        }).join('')+'</div>';
      return `<div style="border-bottom:1px solid rgba(255,255,255,0.05)">
        <div style="padding:8px 18px;font-size:9px;letter-spacing:2px;color:var(--dim);display:flex;justify-content:space-between;background:rgba(0,0,0,0.2)">
          <span style="color:var(--text)">${shortAddr}</span>
          <span>${positions.length} position${positions.length!==1?'s':''}</span></div>
        ${posHtml}</div>`;
    }).join('');
  }

  updateStatusUI(s.running);
}

// ── Poll ───────────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    render(s);
  } catch(e) { console.warn('Poll failed',e); }
}

setTimeout(poll, 100);
setInterval(poll, 3000);
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════════════════
# 11. FLASK APP + MAIN
# ══════════════════════════════════════════════════════════════════════════════

def create_app(bot: PolymarketCopyBot):
    app = Flask(__name__)

    # Add headers so Brave/Chrome don't block fetch() to localhost
    @app.after_request
    def add_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/")
    def dashboard(): return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.route("/api/state")
    def state(): return jsonify(bot.state())

    @app.route("/api/pnl_history")
    def pnl_history():
        window = request.args.get("window", "7d")
        return jsonify(bot.client.get_pnl_history(window))

    @app.route("/api/closed_positions")
    def closed_positions():
        return jsonify(bot.client.get_closed_positions())

    @app.route("/api/toggle", methods=["POST", "OPTIONS"])
    def toggle():
        if request.method == "OPTIONS": return Response(status=200)
        bot.running = not bot.running
        log.info(f"Bot {'started' if bot.running else 'stopped'} via dashboard")
        return jsonify({"running": bot.running})

    @app.route("/api/emergency_stop", methods=["POST"])
    def emergency_stop():
        bot.running = False
        bot.client.cancel_all_orders()
        bot._log_activity("EMERGENCY STOP", "All orders cancelled", "dashboard")
        log.warning("EMERGENCY STOP triggered via dashboard")
        return jsonify({"success": True})

    @app.route("/api/close_position", methods=["POST"])
    def close_position():
        data = request.get_json()
        market_id = data.get("market_id")
        pos = next((p for p in bot.portfolio.open_positions if p.market_id == market_id), None)
        if not pos:
            return jsonify({"success": False, "reason": "Position not found"})
        result = bot.client.close_position(pos)
        if result.success:
            bot.portfolio.close_position(pos, result.avg_price)
            bot._log_activity("MANUAL CLOSE", pos.market_name, "dashboard", size=pos.size_usd)
            log.info(f"Position manually closed: {market_id}")
        return jsonify({"success": result.success, "reason": result.reason})

    @app.route("/api/add_trader", methods=["POST"])
    def add_trader():
        data = request.get_json()
        trader_id = data.get("trader_id", "").strip()
        allocation = float(data.get("allocation", 0.10))
        if not trader_id:
            return jsonify({"success": False, "reason": "No trader ID provided"})
        if trader_id in bot.config.trader_allocations:
            return jsonify({"success": False, "reason": "Trader already followed"})
        bot.config.trader_allocations[trader_id] = allocation
        # Seed into ranker if not already tracked
        if trader_id not in bot.ranker.traders:
            bot.ranker.traders[trader_id] = TraderStats(
                trader_id=trader_id, roi_lifetime=0.20, roi_30d=0.15,
                pnl_usd=0, volume_usd=1_000_000, trade_count=999,
                calibration_score=0.66, win_rate_slope=0.0,
            )
        bot._log_activity("TRADER ADDED", trader_id, "dashboard")
        log.info(f"Trader added via dashboard: {trader_id} @ {allocation:.0%}")
        return jsonify({"success": True})

    @app.route("/api/remove_trader", methods=["POST"])
    def remove_trader():
        data = request.get_json()
        trader_id = data.get("trader_id", "").strip()
        if trader_id not in bot.config.trader_allocations:
            return jsonify({"success": False, "reason": "Trader not in follow list"})
        del bot.config.trader_allocations[trader_id]
        bot._log_activity("TRADER REMOVED", trader_id, "dashboard")
        log.info(f"Trader removed via dashboard: {trader_id}")
        return jsonify({"success": True})

    return app


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    config = BotConfig(
        starting_capital=float(os.getenv("STARTING_CAPITAL", 10.0)),
        paper_mode=os.getenv("PAPER_MODE", "true").lower() == "true",
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL", 60)),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", 5000)),
    )

    bot = PolymarketCopyBot(config)

    # Run bot in background thread
    bot_thread = threading.Thread(target=bot.run, daemon=True)
    bot_thread.start()

    # Run dashboard — Railway provides PORT dynamically, fall back to config for local
    port = int(os.getenv("PORT", config.dashboard_port))
    log.info(f"Dashboard → http://localhost:{port}")
    app = create_app(bot)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
