# app/scripts/stock_algos/base_runner.py
from typing import Optional, Literal, Tuple
import logging
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

Signal = Optional[Literal["BUY", "SELL", "EXIT_LONG", "EXIT_SHORT"]]

class BaseAlgoRunner:
    """
    Owns the end-to-end bot tick flow:
      - DB load
      - market-hours/staleness/once-per-bar guards
      - price history + resample
      - signal -> place order -> history/balance
      - email notification
    Children override only `compute_signal(df, bot)`.
    """

    # ---- plumbing hooks (wired to your existing functions) ----
    def get_db(self): raise NotImplementedError
    def load_bot(self, db, bot_id): raise NotImplementedError
    def is_market_open_now(self) -> bool: raise NotImplementedError
    def fetch_source_bars(self, symbol: str, *args, **kwargs): raise NotImplementedError
    def csv_fallback(self, user_id: int, symbol: str, interval: str): raise NotImplementedError
    def resample_interval(self, df, interval: str, symbol: str): raise NotImplementedError
    def max_age_for_interval(self, interval: str): raise NotImplementedError
    def once_per_bar_guard(self, bot_id: int, latest_bar_ts) -> bool: raise NotImplementedError
    def get_open_trade(self, db, bot_id: int, user_id: int, symbol: str): raise NotImplementedError
    def place_paper_trade_and_bookkeep(self, *, user_id: int, symbol: str, side: str,
                                       price: float, qty: float, bot_id: int, algo_name: str) -> bool: ...
    def update_bot_status(self, db, bot, status: str): raise NotImplementedError
    def now_et(self): raise NotImplementedError

    # ---- per-algo (only this changes across files) ----
    def compute_signal(self, df, bot) -> Signal:
        raise NotImplementedError

    def run_bot_tick(self, bot_id: int, *, algo_name: str):
        db = self.get_db()
        try:
            bot = self.load_bot(db, bot_id)
            if not bot or not bot.is_active:
                logging.warning(f"[BOT-TICK] Bot {bot_id} inactive or missing. Exit.")
                return

            symbol = bot.symbol
            user_id = bot.user_id
            trade_size = bot.trade_size
            interval = (bot.interval or "1min").strip().lower()
            if not trade_size or trade_size <= 0:
                logging.warning(f"[BOT-TICK] Invalid trade_size ({trade_size}) for bot_id={bot_id}")
                return

            # 0) market gate
            if not self.is_market_open_now():
                logging.info(f"[MARKET] Closed ({self.now_et()}). Skip {symbol}@{interval} for bot {bot_id}.")
                self.update_bot_status(db, bot, "IDLE")
                return

            logging.info(f"[BOT-TICK] {algo_name} bot_id={bot_id} user={user_id} {symbol}@{interval} size={trade_size}")

            # 1) primary fetch
            df = self.fetch_source_bars(symbol)

            # 1a) CSV fallback (only when primary missing)
            if df is None or df.empty:
                df = self.csv_fallback(user_id, symbol, interval)

            if df is None or df.empty:
                logging.warning(f"[BOT-TICK] No data for {symbol}@{interval} (tick skipped)")
                self.update_bot_status(db, bot, "IDLE")
                return

            # 2) resample + latest bar + freshness
            latest_time, _, resampled = self.resample_interval(df, interval, symbol)
            if latest_time is None or resampled is None or resampled.empty:
                logging.warning(f"[BOT-TICK] No valid price data for {symbol}@{interval}")
                self.update_bot_status(db, bot, "IDLE")
                return

            # once-per-bar guard
            if self.once_per_bar_guard(bot_id, latest_time):
                logging.info(f"[BOT-TICK] Already processed bar {latest_time} for bot {bot_id}. Skip.")
                self.update_bot_status(db, bot, "IDLE")
                return

            # staleness
            latest_time_et = latest_time
            if getattr(latest_time_et, "tzinfo", None) is None:
                if hasattr(latest_time_et, "tz_localize"):
                    latest_time_et = latest_time_et.tz_localize("US/Eastern")
                else:
                    latest_time_et = self.now_et().tzinfo.localize(latest_time_et)
            else:
                latest_time_et = latest_time_et.astimezone(self.now_et().tzinfo)
            age = self.now_et() - latest_time_et
            max_age = self.max_age_for_interval(interval)
            if age > max_age:
                logging.info(f"[STALE] Last bar age {age} > {max_age}. Skip.")
                self.update_bot_status(db, bot, "IDLE")
                return

            # 3) signal
            signal = self.compute_signal(resampled, bot)
            if not signal:
                logging.info(f"[DEBUG] No signal for {symbol}@{interval}.")
                self.update_bot_status(db, bot, "RUNNING")
                return

            # 4) open vs close
            closes = resampled["close"]
            last_px = float(closes.iloc[-1])
            open_trade = self.get_open_trade(db, bot_id, user_id, symbol)

            # Action routing (same semantics as your current Algo1)
            if signal == "BUY":
                if open_trade and open_trade.position_side == "short":
                    placed = self.place_paper_trade_and_bookkeep(
                        user_id=user_id, symbol=symbol, side="BUY",
                        price=last_px, qty=open_trade.quantity, bot_id=bot_id, algo_name=algo_name
                    )
                elif not open_trade:
                    placed = self.place_paper_trade_and_bookkeep(
                        user_id=user_id, symbol=symbol, side="BUY",
                        price=last_px, qty=trade_size, bot_id=bot_id, algo_name=algo_name
                    )
                else:
                    placed = True  # idempotent duplicate long
            elif signal == "SELL":
                if open_trade and open_trade.position_side == "long":
                    placed = self.place_paper_trade_and_bookkeep(
                        user_id=user_id, symbol=symbol, side="SELL",
                        price=last_px, qty=open_trade.quantity, bot_id=bot_id, algo_name=algo_name
                    )
                elif not open_trade:
                    placed = self.place_paper_trade_and_bookkeep(
                        user_id=user_id, symbol=symbol, side="SELL",
                        price=last_px, qty=trade_size, bot_id=bot_id, algo_name=algo_name
                    )
                else:
                    placed = True  # idempotent duplicate short
            elif signal in ("EXIT_LONG", "EXIT_SHORT"):
                # normalize to proper close direction
                if open_trade:
                    side = "SELL" if open_trade.position_side == "long" else "BUY"
                    placed = self.place_paper_trade_and_bookkeep(
                        user_id=user_id, symbol=symbol, side=side,
                        price=last_px, qty=open_trade.quantity, bot_id=bot_id, algo_name=algo_name
                    )
                else:
                    placed = True
            else:
                placed = False

            self.update_bot_status(db, bot, "RUNNING" if placed else "IDLE")

        except Exception as e:
            db.rollback()
            logging.error(f"[BOT-TICK] {algo_name} error: {e}")
            try:
                bot = locals().get("bot")
                if bot:
                    self.update_bot_status(db, bot, "STOPPED")
            finally:
                pass
        finally:
            db.close()
