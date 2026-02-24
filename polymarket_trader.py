#!/usr/bin/env python3
"""
Polymarket Trader v4 - Multi-Strategy with Capital Management & Scaling
Strategies: AI Contrarian, Late Entry, TBO Trend, TBT Divergence, Execution Confidence
"""

import requests
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import uuid
import subprocess
import sys

sys.path.insert(0, '/Users/claudbot')
try:
    from polymarket_contrarian_v3 import SmartContrarian
except ImportError:
    SmartContrarian = None

try:
    from polymarket_technical_indicators import TechnicalIndicators
except ImportError:
    TechnicalIndicators = None

POLYMARKET_API = "https://gamma-api.polymarket.com"
TRADES_FILE = "/Users/claudbot/trades.json"
CIRCUIT_BREAKER_FILE = "/Users/claudbot/circuit_breaker_state.json"
MARKETS_FILE = "/Users/claudbot/polymarket_markets.json"
MARKET_HISTORY_FILE = "/Users/claudbot/market_history.json"
CAPITAL_FILE = "/Users/claudbot/capital_state.json"
MARKET_CACHE_FILE = "/Users/claudbot/market_cache.json"
MARKET_CACHE_TTL = 600  # 10 minutes

class PolymarketTraderV4:
    """Multi-strategy trader with intelligent capital management"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.session.timeout = 10

        # Supabase write-path config (optional, server-side env)
        self.supabase_url = os.getenv("SUPABASE_URL")
        self.supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        
        # Strategy starting capital: $100 each
        self.initial_capital = {
            "AI Contrarian": 100.00,
            "Late Entry": 100.00,
            "TBO Trend": 100.00,
            "TBT Divergence": 100.00,
            "Execution Confidence": 100.00
        }
        
        # Scaling parameters
        self.min_position = 5.00
        self.max_position = 500.00
        self.risk_per_trade = 0.05  # 5% of strategy capital
        self.profit_threshold_scale_up = 10  # Scale up after 10 wins
        self.loss_threshold_scale_down = 3   # Scale down after 3 losses
        
        # Technical thresholds
        self.adx_trend_threshold = 25  # ADX > 25 = strong trend
        self.rsi_divergence_threshold = 30  # RSI < 30 = oversold divergence
        
        self.trades = self.load_trades()
        self.capital_state = self.load_capital_state()
        self.circuit_breaker_state = self.load_circuit_breaker()
        self.market_history = self.load_market_history()
        self.contrarian = SmartContrarian() if SmartContrarian else None
        self.indicators = TechnicalIndicators() if TechnicalIndicators else None
    
    def load_trades(self) -> List[Dict]:
        """Load trades"""
        try:
            with open(TRADES_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else data.get("trades", [])
        except FileNotFoundError:
            return []
    
    def save_trades(self):
        """Save trades (archive old closed trades to save space)"""
        # Keep only last 100 closed trades + all open trades
        closed_trades = [t for t in self.trades if t.get("status") == "CLOSED"]
        open_trades = [t for t in self.trades if t.get("status") == "OPEN"]
        
        # Keep only recent closed trades
        archived_closed = closed_trades[-100:] if len(closed_trades) > 100 else closed_trades
        
        # Combine and save
        trades_to_save = archived_closed + open_trades
        
        with open(TRADES_FILE, "w") as f:
            json.dump(trades_to_save, f, indent=2, default=str)
        
        # Update in-memory trades list
        self.trades = trades_to_save
    
    def load_capital_state(self) -> Dict:
        """Load capital state for each strategy + timeframe"""
        try:
            with open(CAPITAL_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {
                strategy: {
                    timeframe: {
                        "initial": self.initial_capital[strategy],
                        "current": self.initial_capital[strategy],
                        "cumulative_pnl": 0.0,
                        "consecutive_wins": 0,
                        "consecutive_losses": 0
                    }
                    for timeframe in ["4h", "1h"]
                }
                for strategy in self.initial_capital
            }
    
    def save_capital_state(self):
        """Save capital state"""
        with open(CAPITAL_FILE, "w") as f:
            json.dump(self.capital_state, f, indent=2, default=str)
    
    def get_position_size(self, strategy: str, timeframe: str = "4h") -> float:
        """Calculate position size based on capital and scaling rules (per timeframe)"""
        strategy_state = self.capital_state.get(strategy, {})
        timeframe_state = strategy_state.get(timeframe, {})
        current_capital = timeframe_state.get("current", self.initial_capital.get(strategy, 100))
        
        # Base: 5% of strategy capital
        base_size = current_capital * self.risk_per_trade
        
        # Scale up if winning
        if timeframe_state.get("consecutive_wins", 0) >= self.profit_threshold_scale_up:
            base_size *= 1.5  # 1.5x position on winning streak
        
        # Scale down if losing
        if timeframe_state.get("consecutive_losses", 0) >= self.loss_threshold_scale_down:
            base_size *= 0.5  # 0.5x position after losses
        
        return max(self.min_position, min(self.max_position, base_size))
    
    def load_circuit_breaker(self) -> Dict:
        """Load circuit breaker (per-strategy, 24h reset)"""
        try:
            with open(CIRCUIT_BREAKER_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {
                strategy: {"consecutive_losses": 0, "circuit_broken": False, "broken_until": None}
                for strategy in self.initial_capital
            }
    
    def save_circuit_breaker(self):
        """Save circuit breaker"""
        with open(CIRCUIT_BREAKER_FILE, "w") as f:
            json.dump(self.circuit_breaker_state, f, indent=2, default=str)
    
    def check_circuit_breaker_expired(self, strategy: str) -> bool:
        """Check if CB expired (24h auto-reset)"""
        if strategy not in self.circuit_breaker_state:
            self.circuit_breaker_state[strategy] = {"consecutive_losses": 0, "circuit_broken": False, "broken_until": None}
        
        state = self.circuit_breaker_state[strategy]
        
        if not state.get("circuit_broken"):
            return False
        
        try:
            broken_time = datetime.fromisoformat(state.get("broken_until", ""))
            if datetime.now() > broken_time:
                state["consecutive_losses"] = 0
                state["circuit_broken"] = False
                state["broken_until"] = None
                self.save_circuit_breaker()
                return False
        except:
            pass
        
        return state.get("circuit_broken", False)
    
    def load_market_history(self) -> Dict:
        """Load market history"""
        try:
            with open(MARKET_HISTORY_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save_market_history(self):
        """Save market history"""
        with open(MARKET_HISTORY_FILE, "w") as f:
            json.dump(self.market_history, f, indent=2, default=str)
    
    def update_market_history(self, market_id: str, yes_price: float):
        """Track price history"""
        if market_id not in self.market_history:
            self.market_history[market_id] = {"prices": [], "timestamps": []}
        
        self.market_history[market_id]["prices"].append(yes_price)
        self.market_history[market_id]["timestamps"].append(datetime.now().isoformat())
        
        if len(self.market_history[market_id]["prices"]) > 20:
            self.market_history[market_id]["prices"].pop(0)
            self.market_history[market_id]["timestamps"].pop(0)
        
        self.save_market_history()
    
    def get_market_price(self, market_id: str, max_retries: int = 3) -> Optional[float]:
        """Fetch YES price with exponential backoff on failure"""
        import time
        
        for retry in range(max_retries):
            try:
                response = self.session.get(f"{POLYMARKET_API}/markets/{market_id}", timeout=10)
                response.raise_for_status()
                market = response.json()
                outcome_prices_str = market.get("outcomePrices", "[]")
                outcome_prices = json.loads(outcome_prices_str) if isinstance(outcome_prices_str, str) else outcome_prices_str
                return float(outcome_prices[0]) if outcome_prices else None
            except Exception as e:
                if retry < max_retries - 1:
                    # Exponential backoff: 1s, 2s, 4s
                    wait_time = 2 ** retry
                    time.sleep(wait_time)
                else:
                    return None
        
        return None
    
    def open_position(self, market_id: str, yes_price: float, strategy: str, question: str, timeframe: str = "4h") -> Dict:
        """Open a position"""
        position_size = self.get_position_size(strategy)
        
        position = {
            "id": str(uuid.uuid4())[:8],
            "market_id": market_id,
            "question": question,
            "strategy": strategy,
            "timeframe": timeframe,
            "entry_price": yes_price,
            "entry_time": datetime.now().isoformat(),
            "position_size": position_size,
            "exit_price": None,
            "exit_time": None,
            "pnl": None,
            "status": "OPEN"
        }
        
        self.trades.append(position)
        self.save_trades()
        # Dual-write: Supabase + local JSON (local already persisted above)
        self.write_trade_to_supabase(position, event="open")
        return position
    
    def close_position(self, trade_id: str, exit_price: float) -> Optional[Dict]:
        """Close a position and update capital (accounting for ~2% Polymarket fees)"""
        for trade in self.trades:
            if trade.get("id") == trade_id and trade.get("status") == "OPEN":
                entry = trade["entry_price"]
                size = trade["position_size"]
                
                # Calculate profit BEFORE fees
                profit_before_fees = (exit_price - entry) * (size / entry)
                
                # Subtract ~2% taker fee (entry + exit = ~4% total)
                polymarket_fee = size * 0.02
                profit = profit_before_fees - polymarket_fee
                
                strategy = trade.get("strategy", "Unknown")
                
                trade["exit_price"] = exit_price
                trade["exit_time"] = datetime.now().isoformat()
                trade["pnl"] = profit
                trade["pnl_before_fees"] = profit_before_fees
                trade["polymarket_fee"] = polymarket_fee
                trade["status"] = "CLOSED"
                
                self.save_trades()
                
                # Update capital (per timeframe)
                timeframe = trade.get("timeframe", "4h")
                
                if strategy not in self.capital_state:
                    self.capital_state[strategy] = {}
                if timeframe not in self.capital_state[strategy]:
                    self.capital_state[strategy][timeframe] = {"current": 100, "cumulative_pnl": 0, "consecutive_wins": 0, "consecutive_losses": 0}
                
                state = self.capital_state[strategy][timeframe]
                state["current"] = state.get("current", 100) + profit
                state["cumulative_pnl"] = state.get("cumulative_pnl", 0) + profit
                
                if profit < 0:
                    state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
                    state["consecutive_wins"] = 0
                    self.record_loss(strategy)
                else:
                    state["consecutive_wins"] = state.get("consecutive_wins", 0) + 1
                    state["consecutive_losses"] = 0
                    self.record_win(strategy)
                
                self.capital_state[strategy][timeframe] = state
                self.save_capital_state()

                # Dual-write close event to Supabase
                self.write_trade_to_supabase(trade, event="close")
                
                return trade
        
        return None
    
    def write_trade_to_supabase(self, trade: Dict, event: str = "upsert") -> bool:
        """Write trade event to Supabase trades table. Non-fatal on failure."""
        if not self.supabase_url or not self.supabase_service_role_key:
            return False

        try:
            payload = {
                "strategy": trade.get("strategy"),
                "timeframe": trade.get("timeframe"),
                "market": trade.get("market_id") or trade.get("question", "")[:120],
                "side": "BUY",  # current strategy enters YES positions
                "status": trade.get("status"),
                "entry_price": trade.get("entry_price"),
                "exit_price": trade.get("exit_price"),
                "pnl": trade.get("pnl"),
                "entry_time": trade.get("entry_time"),
                "exit_time": trade.get("exit_time")
            }

            response = self.session.post(
                f"{self.supabase_url}/rest/v1/trades",
                headers={
                    "apikey": self.supabase_service_role_key,
                    "Authorization": f"Bearer {self.supabase_service_role_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal"
                },
                json=payload,
                timeout=10
            )

            if not response.ok:
                print(f"⚠️ Supabase write failed ({response.status_code}): {response.text[:180]}")
                return False
            return True
        except Exception as e:
            print(f"⚠️ Supabase write exception: {e}")
            return False

    def record_loss(self, strategy: str):
        """Record loss for circuit breaker"""
        if strategy not in self.circuit_breaker_state:
            self.circuit_breaker_state[strategy] = {"consecutive_losses": 0, "circuit_broken": False, "broken_until": None}
        
        state = self.circuit_breaker_state[strategy]
        state["consecutive_losses"] += 1
        
        if state["consecutive_losses"] >= 3:
            state["circuit_broken"] = True
            state["broken_until"] = (datetime.now() + timedelta(hours=24)).isoformat()
            self.send_circuit_breaker_alert(strategy, state["consecutive_losses"])
        
        self.save_circuit_breaker()
    
    def record_win(self, strategy: str):
        """Record win for circuit breaker"""
        if strategy not in self.circuit_breaker_state:
            self.circuit_breaker_state[strategy] = {"consecutive_losses": 0, "circuit_broken": False, "broken_until": None}
        
        state = self.circuit_breaker_state[strategy]
        state["consecutive_losses"] = 0
        state["circuit_broken"] = False
        state["broken_until"] = None
        
        self.save_circuit_breaker()
    
    def send_circuit_breaker_alert(self, strategy: str, loss_count: int):
        """Send Telegram alert"""
        try:
            strat_trades = [t for t in self.trades if t.get("strategy") == strategy and t.get("status") == "CLOSED"]
            recent_losses = [t for t in strat_trades[-loss_count:] if t.get("pnl", 0) < 0]
            total_loss = sum(t.get("pnl", 0) for t in recent_losses)
            
            message = f"""
⛔ CIRCUIT BREAKER TRIGGERED

Strategy: {strategy}
Consecutive Losses: {loss_count}
Total Loss: ${total_loss:.2f}
Status: 🔴 LOCKED for 24 hours
            """.strip()
            
            subprocess.run([
                "openclaw", "message",
                "--action", "send",
                "--channel", "telegram",
                "--to", "2119792198",
                "--message", message
            ], capture_output=True)
        except:
            pass
    
    def check_coinbase_panic(self) -> bool:
        """Check if BTC on Coinbase shows panic signals (RSI oversold + price down)"""
        if not self.indicators:
            return True
        
        try:
            # Batch fetch all coins at once (more efficient)
            all_candles = self.indicators.batch_fetch_ohlcv(["BTC"])
            candles = all_candles.get("BTC")
            
            if not candles:
                return True
            
            rsi = self.indicators.calculate_rsi(candles)
            macd = self.indicators.calculate_macd(candles)
            
            # Panic: RSI < 30 (oversold) OR MACD bearish + price falling
            return rsi < 30 or (macd["direction"] == "BEARISH" and candles[-1][4] < candles[-5][4])
        except:
            return True
    
    def check_coinbase_reversal(self) -> bool:
        """Check if BTC on Coinbase shows reversal signals (RSI bouncing + ADX recovering)"""
        if not self.indicators:
            return True
        
        try:
            # Batch fetch (reuses any cached data from panic check)
            all_candles = self.indicators.batch_fetch_ohlcv(["BTC"])
            candles = all_candles.get("BTC")
            
            if not candles:
                return True
            
            rsi = self.indicators.calculate_rsi(candles)
            adx = self.indicators.calculate_adx(candles)
            
            # Reversal: RSI bouncing from oversold OR ADX > 20 (trend recovering)
            return (rsi > 35 and candles[-1][4] > candles[-3][4]) or adx > 20
        except:
            return True
    
    def check_market_regime(self) -> bool:
        """Check if market is tradeable (ADX > 20, not too choppy)"""
        if not self.indicators:
            return True
        
        try:
            # Batch fetch (reuses cached data)
            all_candles = self.indicators.batch_fetch_ohlcv(["BTC"])
            candles = all_candles.get("BTC")
            
            if not candles:
                return True
            
            adx = self.indicators.calculate_adx(candles)
            
            # Skip trading if ADX < 20 (too choppy/ranging)
            return adx > 20
        except:
            return True
    
    def find_signals(self, markets: List[Dict], timeframe: str = "4h") -> Dict:
        """Find trading signals from all strategies for specified timeframe (4h or 1h)"""
        signals = {
            "AI Contrarian": [],
            "Late Entry": [],
            "TBO Trend": [],
            "TBT Divergence": [],
            "Execution Confidence": []
        }
        
        for market in markets:
            market_id = market.get("market_id")
            yes_price = market.get("yes_price")
            question = market.get("question", "")
            
            if not yes_price:
                yes_price = self.get_market_price(market_id)
            
            # Filter for specified timeframe (prefer explicit field from discovery output)
            market_tf = market.get("timeframe")
            if market_tf:
                if market_tf != timeframe:
                    continue
            else:
                # Backward compatibility with legacy question-text tagging
                if timeframe not in question.lower():
                    continue

            if not yes_price:
                continue
            
            self.update_market_history(market_id, yes_price)
            price_hist = self.market_history.get(market_id, {}).get("prices", [])
            
            # AI Contrarian v3 (panic detection) - with Coinbase confirmation
            if self.contrarian and len(price_hist) > 5:
                panic = self.contrarian.detect_crowd_panic(market_id, yes_price, 100, 
                    [{"price": p, "timestamp": self.market_history[market_id]["timestamps"][i]} 
                     for i, p in enumerate(price_hist)])
                
                # Only signal if Polymarket panic + Coinbase confirms panic
                if panic and self.check_coinbase_panic():
                    signals["AI Contrarian"].append({
                        "market_id": market_id,
                        "yes_price": yes_price,
                        "confidence": panic["confidence"] * 1.1,  # Boost confidence with Coinbase confirmation
                        "reason": f"{panic['reasoning']} + Coinbase BTC panic confirmed"
                    })
            
            # Late Entry (reversal confirmation) - with Coinbase confirmation
            if yes_price < 0.35 and len(price_hist) >= 3:
                # Check Polymarket reversal
                polymarket_reversing = all(price_hist[i] <= price_hist[i+1] for i in range(-3, -1))
                
                # Check Coinbase reversal
                coinbase_reversing = self.check_coinbase_reversal()
                
                # Only signal if both Polymarket + Coinbase confirm reversal
                if polymarket_reversing and coinbase_reversing:
                    signals["Late Entry"].append({
                        "market_id": market_id,
                        "yes_price": yes_price,
                        "confidence": 0.85  # Higher confidence with dual confirmation
                    })
            
            # TBO Trend - Real CCXT Coinbase OHLCV indicators (batch fetched)
            if self.indicators and "BTC" in question.upper():
                tbo_sig = self.indicators.get_tbo_signal("BTC")
                if tbo_sig:
                    signals["TBO Trend"].append({
                        "market_id": market_id,
                        "yes_price": yes_price,
                        "confidence": tbo_sig["confidence"],
                        "adx": tbo_sig["adx"]
                    })
            elif self.indicators and "ETH" in question.upper():
                tbo_sig = self.indicators.get_tbo_signal("ETH")
                if tbo_sig:
                    signals["TBO Trend"].append({
                        "market_id": market_id,
                        "yes_price": yes_price,
                        "confidence": tbo_sig["confidence"],
                        "adx": tbo_sig["adx"]
                    })
            
            # TBT Divergence - Real CCXT Coinbase OHLCV indicators (batch fetched)
            if self.indicators and "BTC" in question.upper():
                tbt_sig = self.indicators.get_tbt_signal("BTC")
                if tbt_sig:
                    signals["TBT Divergence"].append({
                        "market_id": market_id,
                        "yes_price": yes_price,
                        "confidence": tbt_sig["confidence"],
                        "rsi_div": tbt_sig.get("rsi_divergence", False),
                        "macd_div": tbt_sig.get("macd_divergence", False)
                    })
            elif self.indicators and "ETH" in question.upper():
                tbt_sig = self.indicators.get_tbt_signal("ETH")
                if tbt_sig:
                    signals["TBT Divergence"].append({
                        "market_id": market_id,
                        "yes_price": yes_price,
                        "confidence": tbt_sig["confidence"],
                        "rsi_div": tbt_sig.get("rsi_divergence", False),
                        "macd_div": tbt_sig.get("macd_divergence", False)
                    })
        
        return signals
    
    def get_markets_cached(self) -> List[Dict]:
        """Load markets from cache if fresh, otherwise from file"""
        import time
        
        try:
            with open(MARKET_CACHE_FILE, "r") as f:
                cache = json.load(f)
                cache_age = time.time() - cache.get("timestamp", 0)
                
                if cache_age < MARKET_CACHE_TTL:  # Cache valid (< 10 min old)
                    return cache.get("markets", [])
        except:
            pass
        
        # Cache miss or expired - load from file
        try:
            with open(MARKETS_FILE, "r") as f:
                markets = json.load(f).get("markets", [])
                
                # Save to cache
                with open(MARKET_CACHE_FILE, "w") as cf:
                    json.dump({
                        "timestamp": time.time(),
                        "markets": markets
                    }, cf)
                
                return markets
        except:
            return []
    
    def run_trading_cycle(self):
        """Execute trading cycle for both 4h and 1h markets"""
        print("\n" + "=" * 80)
        print("🤖 POLYMARKET TRADER v4 - 4H vs 1H TEST")
        print("=" * 80)
        
        # Check market regime first
        if not self.check_market_regime():
            print("\n⏰ Market too choppy (ADX < 20). Skipping trading cycle.")
            return
        
        # Use cached market data (10-min TTL) to save API calls
        markets = self.get_markets_cached()
        
        if not markets:
            print("❌ No market data")
            return
        
        # Trade both timeframes
        for timeframe in ["4h", "1h"]:
            self._execute_trading_cycle(markets, timeframe)
    
    def _execute_trading_cycle(self, markets: List[Dict], timeframe: str):
        """Execute trading for a specific timeframe (4h or 1h)"""
        print(f"\n📊 {timeframe.upper()} MARKETS")
        
        signals = self.find_signals(markets, timeframe=timeframe)
        
        for strategy, signal_list in signals.items():
            # Skip if circuit breaker active
            if self.check_circuit_breaker_expired(strategy):
                continue
            
            if not signal_list:
                continue
            
            pos_size = self.get_position_size(strategy, timeframe=timeframe)
            
            # Strategy-specific execution logic
            max_positions = {"AI Contrarian": 1, "Late Entry": 2, "TBO Trend": 1, "TBT Divergence": 1, "Execution Confidence": 1}
            max_pos = max_positions.get(strategy, 1)
            
            print(f"  {strategy}: {len(signal_list)} signals")
            
            # Open positions (up to max)
            opened = 0
            for signal in signal_list[:max_pos]:
                market_id = signal.get("market_id")
                yes_price = signal.get("yes_price")
                question = signal.get("question", "")
                
                # Check if already have position in this market + timeframe
                has_position = any(
                    t.get("market_id") == market_id and t.get("status") == "OPEN" and t.get("strategy") == strategy and t.get("timeframe") == timeframe
                    for t in self.trades
                )
                
                if not has_position and opened < max_pos:
                    pos = self.open_position(market_id, yes_price, strategy, question, timeframe=timeframe)
                    print(f"    ✅ @ ${yes_price:.2f} | Size: ${pos_size:.2f}")
                    opened += 1
            
            # Check for exits (YES > sell threshold) - only for this timeframe
            sell_threshold = {"AI Contrarian": 0.70, "Late Entry": 0.65, "TBO Trend": 0.65, "TBT Divergence": 0.65, "Execution Confidence": 0.65}
            threshold = sell_threshold.get(strategy, 0.65)
            
            for trade in self.trades:
                if trade.get("status") != "OPEN" or trade.get("strategy") != strategy or trade.get("timeframe") != timeframe:
                    continue
                
                market_id = trade.get("market_id")
                yes_price = self.get_market_price(market_id)
                
                if yes_price and yes_price > threshold:
                    self.close_position(trade.get("id"), yes_price)
                    print(f"    ❌ Closed @ ${yes_price:.2f} | P/L: ${trade.get('pnl', 0):.2f}")


if __name__ == "__main__":
    trader = PolymarketTraderV4()
    trader.run_trading_cycle()
    
    # Auto-commit
    try:
        subprocess.run(["git", "-C", "/Users/claudbot", "add", "trades.json", "capital_state.json"], check=True)
        subprocess.run([
            "git", "-C", "/Users/claudbot", "commit",
            "-m", f"[{datetime.now().strftime('%H:%M')}] Polymarket v4 - multi-strategy update"
        ], check=False)
        subprocess.run(["git", "-C", "/Users/claudbot", "push", "origin", "main"], check=True)
    except Exception as e:
        print(f"⚠️ Git sync failed: {e}")
