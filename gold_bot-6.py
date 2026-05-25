"""
=======================================================
  GOLD AUTO TRADE BOT — XAUUSD
  Python + MetaTrader5 + Telegram
  กลยุทธ์: EMA Cross + RSI + ATR Stop Loss
=======================================================
ติดตั้ง:
  pip install MetaTrader5 python-telegram-bot pandas ta schedule
"""

import MetaTrader5 as mt5
import pandas as pd
import ta
import asyncio
import schedule
import time
import logging
from datetime import datetime
from config import CONFIG
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─── Logging ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("gold_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── State ──────────────────────────────────────────────
bot_running   = False
trade_count   = 0
win_count     = 0
total_profit  = 0.0
telegram_app  = None


# ════════════════════════════════════════════════════════
#  MT5 CONNECTION
# ════════════════════════════════════════════════════════

def connect_mt5() -> bool:
    if not mt5.initialize(
        login=CONFIG["MT5_LOGIN"],
        password=CONFIG["MT5_PASSWORD"],
        server=CONFIG["MT5_SERVER"]
    ):
        log.error(f"MT5 เชื่อมต่อไม่ได้: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    log.info(f"✅ เชื่อมต่อ MT5 สำเร็จ | Balance: ${info.balance:.2f}")
    return True


def disconnect_mt5():
    mt5.shutdown()
    log.info("MT5 ตัดการเชื่อมต่อแล้ว")


# ════════════════════════════════════════════════════════
#  MARKET DATA
# ════════════════════════════════════════════════════════

def get_candles(symbol: str, timeframe, count: int = 100) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def get_price(symbol: str) -> dict:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {}
    return {"bid": tick.bid, "ask": tick.ask, "spread": round(tick.ask - tick.bid, 2)}


# ════════════════════════════════════════════════════════
#  INDICATORS & SIGNAL
# ════════════════════════════════════════════════════════

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = CONFIG
    df["ema_fast"]  = ta.trend.ema_indicator(df["close"], c["EMA_FAST"])
    df["ema_slow"]  = ta.trend.ema_indicator(df["close"], c["EMA_SLOW"])
    df["ema_trend"] = ta.trend.ema_indicator(df["close"], c["EMA_TREND"])
    df["rsi"]       = ta.momentum.rsi(df["close"], c["RSI_PERIOD"])
    df["atr"]       = ta.volatility.average_true_range(
                        df["high"], df["low"], df["close"], c["ATR_PERIOD"])
    return df


def get_signal(df: pd.DataFrame) -> str:
    """คืนค่า 'BUY', 'SELL' หรือ 'NONE'"""
    if len(df) < 3:
        return "NONE"

    prev = df.iloc[-2]
    curr = df.iloc[-1]
    price = curr["close"]
    c = CONFIG

    # Golden Cross — BUY
    cross_up   = prev["ema_fast"] < prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
    trend_up   = price > curr["ema_trend"]
    rsi_ok_buy = c["RSI_OVERSOLD"] < curr["rsi"] < c["RSI_OVERBOUGHT"]

    # Death Cross — SELL
    cross_down  = prev["ema_fast"] > prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]
    trend_down  = price < curr["ema_trend"]
    rsi_ok_sell = c["RSI_OVERSOLD"] < curr["rsi"] < c["RSI_OVERBOUGHT"]

    if cross_up and trend_up and rsi_ok_buy:
        return "BUY"
    if cross_down and trend_down and rsi_ok_sell:
        return "SELL"
    return "NONE"


# ════════════════════════════════════════════════════════
#  ORDER MANAGEMENT
# ════════════════════════════════════════════════════════

def calc_lot(sl_distance: float) -> float:
    info    = mt5.account_info()
    balance = info.balance
    risk    = balance * (CONFIG["RISK_PERCENT"] / 100)
    sym     = mt5.symbol_info(CONFIG["SYMBOL"])
    if sym is None or sl_distance <= 0:
        return CONFIG["MIN_LOT"]
    tick_val  = sym.trade_tick_value
    tick_size = sym.trade_tick_size
    lot = risk / (sl_distance / tick_size * tick_val)
    lot = max(CONFIG["MIN_LOT"], min(CONFIG["MAX_LOT"], round(lot, 2)))
    return lot


def count_open_positions() -> int:
    positions = mt5.positions_get(symbol=CONFIG["SYMBOL"])
    if positions is None:
        return 0
    return sum(1 for p in positions if p.magic == CONFIG["MAGIC"])


def has_position(order_type: int) -> bool:
    positions = mt5.positions_get(symbol=CONFIG["SYMBOL"])
    if positions is None:
        return False
    for p in positions:
        if p.magic == CONFIG["MAGIC"] and p.type == order_type:
            return True
    return False


def open_order(signal: str, atr: float) -> bool:
    global trade_count
    sym    = CONFIG["SYMBOL"]
    tick   = mt5.symbol_info_tick(sym)
    digits = mt5.symbol_info(sym).digits

    if signal == "BUY":
        price  = tick.ask
        sl     = round(price - atr * CONFIG["SL_MULT"], digits)
        tp     = round(price + atr * CONFIG["TP_MULT"], digits)
        otype  = mt5.ORDER_TYPE_BUY
    else:
        price  = tick.bid
        sl     = round(price + atr * CONFIG["SL_MULT"], digits)
        tp     = round(price - atr * CONFIG["TP_MULT"], digits)
        otype  = mt5.ORDER_TYPE_SELL

    lot = calc_lot(abs(price - sl))

    request = {
        "action":     mt5.TRADE_ACTION_DEAL,
        "symbol":     sym,
        "volume":     lot,
        "type":       otype,
        "price":      price,
        "sl":         sl,
        "tp":         tp,
        "magic":      CONFIG["MAGIC"],
        "comment":    "GoldBot",
        "type_time":  mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        trade_count += 1
        msg = (f"✅ {signal} XAUUSD เปิดสำเร็จ\n"
               f"💰 ราคา: {price} | Lot: {lot}\n"
               f"🛡 SL: {sl} | 🎯 TP: {tp}")
        log.info(msg)
        send_telegram(msg)
        return True
    else:
        err = f"❌ เปิด Order ไม่สำเร็จ: {result.retcode} — {result.comment}"
        log.error(err)
        send_telegram(err)
        return False


def manage_trailing_stop():
    """ขยับ SL ตาม ATR เมื่อกำไร"""
    positions = mt5.positions_get(symbol=CONFIG["SYMBOL"])
    if not positions:
        return
    df = get_candles(CONFIG["SYMBOL"], CONFIG["TIMEFRAME"], 20)
    if df.empty:
        return
    df = calculate_indicators(df)
    atr = df.iloc[-1]["atr"]

    for pos in positions:
        if pos.magic != CONFIG["MAGIC"]:
            continue
        sym    = CONFIG["SYMBOL"]
        digits = mt5.symbol_info(sym).digits
        tick   = mt5.symbol_info_tick(sym)

        if pos.type == mt5.ORDER_TYPE_BUY:
            new_sl = round(tick.bid - atr * CONFIG["TRAIL_ATR"], digits)
            if new_sl > pos.sl:
                _modify_sl(pos.ticket, new_sl, pos.tp)

        elif pos.type == mt5.ORDER_TYPE_SELL:
            new_sl = round(tick.ask + atr * CONFIG["TRAIL_ATR"], digits)
            if new_sl < pos.sl:
                _modify_sl(pos.ticket, new_sl, pos.tp)


def _modify_sl(ticket: int, sl: float, tp: float):
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       sl,
        "tp":       tp,
    }
    mt5.order_send(request)


def check_drawdown() -> bool:
    info = mt5.account_info()
    dd   = (info.balance - info.equity) / info.balance * 100
    if dd >= CONFIG["MAX_DRAWDOWN"]:
        msg = f"⚠️ Drawdown {dd:.1f}% เกิน {CONFIG['MAX_DRAWDOWN']}% — Bot หยุดทำงาน!"
        log.warning(msg)
        send_telegram(msg)
        return True
    return False


def is_valid_session() -> bool:
    hour = datetime.now().hour
    london  = 14 <= hour < 23
    newyork = hour >= 20 or hour < 3
    return london or newyork


# ════════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════════

def run_bot_cycle():
    global bot_running
    if not bot_running:
        return

    if check_drawdown():
        bot_running = False
        return

    manage_trailing_stop()

    if not is_valid_session():
        log.info("⏰ นอก Session — ไม่เทรด")
        return

    if count_open_positions() >= CONFIG["MAX_TRADES"]:
        return

    df = get_candles(CONFIG["SYMBOL"], CONFIG["TIMEFRAME"])
    if df.empty:
        return
    df = calculate_indicators(df)

    signal = get_signal(df)
    atr    = df.iloc[-1]["atr"]

    if signal == "BUY" and not has_position(mt5.ORDER_TYPE_BUY):
        open_order("BUY", atr)
    elif signal == "SELL" and not has_position(mt5.ORDER_TYPE_SELL):
        open_order("SELL", atr)


# ════════════════════════════════════════════════════════
#  TELEGRAM BOT
# ════════════════════════════════════════════════════════

def send_telegram(message: str):
    """ส่งข้อความ Telegram (non-async wrapper)"""
    try:
        bot = Bot(token=CONFIG["TELEGRAM_TOKEN"])
        asyncio.get_event_loop().run_until_complete(
            bot.send_message(chat_id=CONFIG["TELEGRAM_CHAT_ID"], text=message)
        )
    except Exception as e:
        log.warning(f"Telegram ส่งไม่ได้: {e}")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global bot_running
    if not bot_running:
        bot_running = True
        await update.message.reply_text(
            "🟢 Gold Bot เริ่มทำงานแล้ว!\n"
            f"Symbol: {CONFIG['SYMBOL']} | TF: H1\n"
            f"Risk: {CONFIG['RISK_PERCENT']}% | MaxTrades: {CONFIG['MAX_TRADES']}"
        )
    else:
        await update.message.reply_text("⚡ Bot ทำงานอยู่แล้ว")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global bot_running
    bot_running = False
    await update.message.reply_text("🔴 Gold Bot หยุดทำงานแล้ว")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not mt5.initialize():
        await update.message.reply_text("❌ MT5 ไม่ได้เชื่อมต่อ")
        return
    info  = mt5.account_info()
    tick  = mt5.symbol_info_tick(CONFIG["SYMBOL"])
    pos   = count_open_positions()
    dd    = (info.balance - info.equity) / info.balance * 100

    text = (
        f"📊 *Gold Bot Status*\n"
        f"{'🟢 กำลังทำงาน' if bot_running else '🔴 หยุดอยู่'}\n\n"
        f"💰 Balance: ${info.balance:.2f}\n"
        f"📈 Equity:  ${info.equity:.2f}\n"
        f"📉 Drawdown: {dd:.1f}%\n\n"
        f"🏅 XAUUSD: {tick.bid:.2f} / {tick.ask:.2f}\n"
        f"📂 Open Trades: {pos}/{CONFIG['MAX_TRADES']}\n"
        f"🔢 Total Trades: {trade_count}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    positions = mt5.positions_get(symbol=CONFIG["SYMBOL"])
    if not positions:
        await update.message.reply_text("ไม่มี Position ที่เปิดอยู่")
        return
    closed = 0
    for pos in positions:
        if pos.magic != CONFIG["MAGIC"]:
            continue
        tick  = mt5.symbol_info_tick(CONFIG["SYMBOL"])
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        otype = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        req   = {
            "action":     mt5.TRADE_ACTION_DEAL,
            "position":   pos.ticket,
            "symbol":     CONFIG["SYMBOL"],
            "volume":     pos.volume,
            "type":       otype,
            "price":      price,
            "magic":      CONFIG["MAGIC"],
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        r = mt5.order_send(req)
        if r.retcode == mt5.TRADE_RETCODE_DONE:
            closed += 1
    await update.message.reply_text(f"✅ ปิด {closed} Position สำเร็จ")


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = get_price(CONFIG["SYMBOL"])
    if not p:
        await update.message.reply_text("❌ ดึงราคาไม่ได้")
        return
    await update.message.reply_text(
        f"🏅 *XAUUSD*\n"
        f"BID: {p['bid']:.2f}\n"
        f"ASK: {p['ask']:.2f}\n"
        f"Spread: {p['spread']:.2f}",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 *คำสั่ง Gold Bot*\n\n"
        "/start     — เริ่ม Bot\n"
        "/stop      — หยุด Bot\n"
        "/status    — ดูสถานะ\n"
        "/price     — ดูราคาทอง\n"
        "/closeall  — ปิดทุก Position\n"
        "/help      — คำสั่งทั้งหมด",
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════

def main():
    log.info("=== Gold Auto Trade Bot เริ่มต้น ===")

    if not connect_mt5():
        log.error("ไม่สามารถเชื่อมต่อ MT5 ได้ — ตรวจสอบ config.py")
        return

    # ตั้ง Schedule รัน Bot ทุก 5 วินาที
    schedule.every(5).seconds.do(run_bot_cycle)

    # สร้าง Telegram App
    app = ApplicationBuilder().token(CONFIG["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("price",    cmd_price))
    app.add_handler(CommandHandler("closeall", cmd_close_all))
    app.add_handler(CommandHandler("help",     cmd_help))

    send_telegram("🚀 Gold Bot พร้อมทำงานแล้ว! พิมพ์ /start เพื่อเริ่ม")
    log.info("Telegram Bot พร้อมแล้ว")

    # รัน schedule loop ใน thread แยก
    import threading
    def run_schedule():
        while True:
            schedule.run_pending()
            time.sleep(1)

    t = threading.Thread(target=run_schedule, daemon=True)
    t.start()

    # รัน Telegram polling
    app.run_polling()

    disconnect_mt5()


if __name__ == "__main__":
    main()
