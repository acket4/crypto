import os
import sys
import html
import time
import json
import csv
import io
import datetime
import subprocess
import threading
state_lock = threading.Lock()
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
import requests
import xml.etree.ElementTree as ET

def safe_float(val, default=0.0):
    try:
        if val == "" or val is None:
            return default
        return float(val)
    except (ValueError, TypeError):
        return default
import google.generativeai as genai

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET") or os.getenv("API_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
user_id_str = os.getenv("TELEGRAM_USER_ID") or os.getenv("USER_ID") or os.getenv("ADMIN_ID") or os.getenv("ALLOWED_USER_ID") or "0"
ALLOWED_USER_ID = int(user_id_str)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    print("CRITICAL ERROR: Telegram Bot Token is missing in environment variables!")

session = HTTP(testnet=False, demo=False, api_key=API_KEY, api_secret=API_SECRET)
bot = telebot.TeleBot(BOT_TOKEN)
STATE_FILE = "bot_state.json"

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def check_auth(message):
    return message.from_user.id == ALLOWED_USER_ID

def load_state():
    default_state = {
        "step": 500.0,
        "qty": 0.002,
        "auto_trade": False,
        "base_price": 0,
        "max_dca": 5,
        "sl_percent": 15.0,
        "early_cut_loss": 3.0,
        "dump_pause_until": 0,
        "budget": 1000.0,
        "dynamic_step": False,
        "trailing_drop": 100.0,
        "trailing_active": False,
        "trailing_high": 0.0,
        "aggression": "high"
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                data = json.load(f)
                if "symbols" in data:
                    return default_state # Reset if it has multi-coin format
                for k, v in default_state.items():
                    if k not in data:
                        data[k] = v
                data["aggression"] = "high"
                return data
            except Exception:
                return default_state
    return default_state

def save_state(st):
    with state_lock:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f, indent=4)

state = load_state()

def log_trade(action, qty, price, pnl=0.0):
    try:
        t_str = time.strftime('%Y-%m-%d %H:%M:%S')
        with open("trades.log", "a") as f:
            f.write(f"{t_str} | {action} | QTY: {qty} | Price: {price} | PnL: {pnl}\n")
            
        # Ведем структурированный JSON-журнал сделок
        trades_json_file = "trades_history.json"
        trades_list = []
        if os.path.exists(trades_json_file):
            try:
                with open(trades_json_file, "r") as f:
                    trades_list = json.load(f)
            except Exception:
                trades_list = []
        trades_list.append({
            "timestamp": t_str,
            "epoch": time.time(),
            "action": action,
            "symbol": "BTCUSDT",
            "qty": float(qty),
            "price": float(price),
            "pnl": float(pnl)
        })
        if len(trades_list) > 1000:
            trades_list = trades_list[-1000:]
        with open(trades_json_file, "w") as f:
            json.dump(trades_list, f, indent=2)
    except Exception as e:
        pass

def get_klines(symbol="BTCUSDT", interval="15", limit=50):
    try:
        resp = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
        return list(reversed(resp['result']['list']))
    except Exception:
        return []

def calculate_atr(klines):
    if len(klines) < 2: return 0
    tr_list = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list[-14:]) / min(14, len(tr_list))

def calculate_rsi(klines):
    if len(klines) < 15: return 50
    gains = []
    losses = []
    for i in range(1, len(klines)):
        change = float(klines[i][4]) - float(klines[i-1][4])
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_ema(closes, period=200):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema
def check_volume_spike(klines, multiplier=1.3):
    if len(klines) < 20: return False
    try:
        volumes = [float(k[5]) for k in klines]
        avg_vol = sum(volumes[-20:-1]) / 19
        current_vol = volumes[-1]
        return current_vol > (avg_vol * multiplier)
    except:
        return False

def check_rsi_divergence(klines, period=14):
    if len(klines) < period + 15: return False
    try:
        rsis = []
        for end_idx in range(period, len(klines)):
            window = klines[end_idx-period:end_idx+1]
            gains = [max(0, float(window[i][4]) - float(window[i-1][4])) for i in range(1, len(window))]
            losses = [abs(min(0, float(window[i][4]) - float(window[i-1][4]))) for i in range(1, len(window))]
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            rsis.append(100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
        
        current_price = float(klines[-1][4])
        current_rsi = rsis[-1]
        
        past_prices = [float(k[4]) for k in klines[-15:-3]]
        past_rsis = rsis[-15:-3]
        
        min_past_price = min(past_prices)
        min_past_idx = past_prices.index(min_past_price)
        min_past_rsi = past_rsis[min_past_idx]
        
        if current_price < min_past_price and current_rsi > min_past_rsi and min_past_rsi < 40:
            return True
        return False
    except:
        return False

def check_bearish_breakdown_for_cut(klines_15m):
    """
    Надежный анализ реального слома структуры тренда (на 15m свечах):
    Защищает от ложных срезов на 5-минутном рыночном шуме.
    Срабатывает ТОЛЬКО при реальной смене тренда на дамп.
    """
    if not klines_15m or len(klines_15m) < 15:
        return False, "Недостаточно данных"
    try:
        closes = [float(k[4]) for k in klines_15m]
        opens = [float(k[1]) for k in klines_15m]
        lows = [float(k[3]) for k in klines_15m]
        volumes = [float(k[5]) for k in klines_15m]

        curr_red = closes[-1] < opens[-1]
        c1_red = closes[-2] < opens[-2]
        c2_red = closes[-3] < opens[-3]

        # Локальный минимум за последние 8 свечей (2 часа)
        local_min_past = min(lows[-10:-2])
        is_breakdown = closes[-1] < local_min_past

        avg_vol = sum(volumes[-15:-2]) / 13 if len(volumes) >= 15 else volumes[-1]
        high_sell_vol = volumes[-1] > (avg_vol * 1.5) or volumes[-2] > (avg_vol * 1.5)

        # Сигнал истинного слива:
        if is_breakdown and curr_red and high_sell_vol:
            return True, f"Истинный пробой поддержки ({closes[-1]:.1f}) на 15m с ростом объема"
        if curr_red and c1_red and c2_red and high_sell_vol:
            return True, "Мощный безоткатный дамп (3 красные 15m свечи с объемом)"
        return False, ""
    except Exception as e:
        return False, str(e)

def analyze_market_entry(klines_15m, current_price, aggression="high"):
    """
    Агрессивный алгоритм с анализом RSI и структуры движения:
    1. По умолчанию бот ВСЕГДА АГРЕССИВЕН (не ждет на заборе).
    2. Обязательно рассчитывает и учитывает RSI (14 периодов, 15m свечи).
    3. РЫНОК С ВЫСОКИМ RSI (52-80), НО ИДЕТ ВВЕРХ:
       Если тренд бычий (EMA20 > EMA50 или цена выше скользящих),
       и свечи показывают подъем (зеленый моментум, рост объемов, обновление вершин),
       бот понимает, что это продолжение мощного ралли, и СМЕЛО ВЛИВАЕТСЯ ТУДА!
    4. Защита от покупки на самом излете:
       - Не входить, если RSI > 82 (экстремальный перегрев перед резким сбросом).
       - Не входить, если при RSI > 72 виден явный разворотный слив (длинная тень сверху или 2 красных бара с объемом).
    5. В боковике или на откате (RSI 30-52):
       - Мгновенно подбирает позицию при первом признаке движения вверх.
    """
    if not klines_15m or len(klines_15m) < 40:
        return False, "Сбор данных свечей...", 50.0, "UNKNOWN"

    closes = [float(k[4]) for k in klines_15m]
    opens = [float(k[1]) for k in klines_15m]
    highs = [float(k[2]) for k in klines_15m]
    volumes = [float(k[5]) for k in klines_15m]

    rsi = calculate_rsi(klines_15m)
    ema_20 = calculate_ema(closes, 20)
    ema_50 = calculate_ema(closes, 50)

    curr_green = closes[-1] > opens[-1]
    prev_green = closes[-2] > opens[-2] if len(closes) > 1 else False

    # Бычий тренд по EMA или динамике цен
    is_bullish = False
    if ema_20 and ema_50:
        is_bullish = (current_price >= ema_50) or (ema_20 >= ema_50) or (current_price >= ema_20)
    else:
        is_bullish = (current_price >= closes[-10])

    avg_vol = sum(volumes[-15:-1]) / 14 if len(volumes) >= 15 else volumes[-1]
    local_high = max(highs[-10:])
    pushing_highs = (current_price >= local_high * 0.996) # Цена у вершины диапазона / пробой

    # Проверка разворотной падающей звезды (Shooting Star с длинным фитилем сверху)
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    body = abs(closes[-1] - opens[-1])
    is_rejection_candle = (upper_wick > body * 2.2) and (upper_wick > (current_price * 0.002))

    # 1. ЗАЩИТА ОТ КРАХА НА САМОМ ПИКЕ
    if rsi > 88:
        return False, f"Экстремальный перегрев (RSI: {rsi:.1f} > 88). Жду короткой паузы, чтобы не купить на абсолютном хае", rsi, "EXTREME_OVERBOUGHT"

    if rsi > 74 and is_rejection_candle and not curr_green:
        return False, f"RSI высокий ({rsi:.1f}) с разворотной верхней тенью. Жду подтверждения движения", rsi, "REVERSAL_EXHAUSTION"

    if rsi > 72 and (not curr_green) and (not prev_green) and (volumes[-1] > avg_vol * 1.3):
        return False, f"RSI высокий ({rsi:.1f}) на волне фиксации прибыли (2 красные свечи с объемом)", rsi, "PROFIT_TAKING"

    # 2. РЫНОК С ВЫСОКИМ RSI (52 - 80), НО ИДЕТ ВВЕРХ -> ВЛИВАЕМСЯ!
    if rsi >= 52:
        market_heading_up = is_bullish and (curr_green or prev_green or pushing_highs or (current_price > closes[-3]))
        if market_heading_up:
            return True, f"Вливаемся в растущий тренд! Высокий RSI ({rsi:.1f}) подтверждает силу покупателей (зеленый моментум, EMA бычьи)", rsi, "MOMENTUM_BULL_ENTRY"
        if aggression == "high" and (curr_green or not is_rejection_candle):
            return True, f"Агрессивное вливание в моментум (RSI: {rsi:.1f}, цена: {current_price:.1f})", rsi, "AGGRESSIVE_HIGH_RSI_FLOW"

    # 3. УМЕРЕННАЯ ЗОНА RSI (40 - 52)
    if is_bullish or curr_green or prev_green:
        return True, f"Вход по тренду (RSI: {rsi:.1f}, бычья структура)", rsi, "TREND_FLOW"

    # 4. ЗОНА ОТКАТА / ПЕРЕПРОДАННОСТИ (RSI < 40)
    if curr_green or rsi < 34:
        return True, f"Ловля отскока от локального дна (RSI: {rsi:.1f})", rsi, "PULLBACK_BOUNCE"

    # 5. ЕСЛИ БЕЗОТКАТНЫЙ СПАД ВНИЗ
    trend_str = "Бычий 🐂" if is_bullish else "Коррекция/Дамп 🔻"
    return False, f"Тренд: {trend_str}, RSI: {rsi:.1f}. Жду остановки локального спада", rsi, "WAITING"

def fetch_crypto_news():
    try:
        url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
        resp = requests.get(url, timeout=10)
        root = ET.fromstring(resp.content)
        headlines = [f"- {item.find('title').text}" for item in root.findall('./channel/item')[:5]]
        return "\n".join(headlines)
    except Exception:
        return ""

def analyze_sentiment(news_text, rsi, atr, current_price):
    if not GEMINI_API_KEY or not news_text: 
        return "ERROR_NO_KEY", news_text
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = (
            "Ты профессиональный крипто-аналитик. Оцени, как новости и тех. индикаторы повлияют на цену BTC в краткосрок.\n"
            f"Текущая цена BTC: {current_price}, RSI(14): {rsi:.1f}, ATR: {atr:.1f}\n"
            "Если RSI > 70 — рынок перегрет (перекуплен), если < 30 — перепродан. ATR показывает волатильность.\n"
            "Ответь СТРОГО в следующем формате (без лишних слов и markdown-разметки):\n"
            "СЕНТИМЕНТ: [одно слово POSITIVE, NEUTRAL или NEGATIVE]\n"
            "ПЕРЕВОД:\n"
            "[перевод заголовков и краткий вывод тех. анализа на русском, каждый с новой строки через дефис]\n\n"
            f"Заголовки:\n{news_text}"
        )
        response = model.generate_content(prompt)
        result = response.text.strip()
        
        sentiment = "NEUTRAL"
        translated = news_text
        
        if "СЕНТИМЕНТ:" in result:
            lines = result.split('\n')
            for i, line in enumerate(lines):
                if line.startswith("СЕНТИМЕНТ:"):
                    sent_str = line.replace("СЕНТИМЕНТ:", "").strip().upper()
                    if "NEGATIVE" in sent_str: sentiment = "NEGATIVE"
                    elif "POSITIVE" in sent_str: sentiment = "POSITIVE"
                    else: sentiment = "NEUTRAL"
                elif line.startswith("ПЕРЕВОД:"):
                    translated = "\n".join(lines[i+1:]).strip()
                    break
        else:
            if "NEGATIVE" in result.upper(): sentiment = "NEGATIVE"
            elif "POSITIVE" in result.upper(): sentiment = "POSITIVE"
            translated = result
            
        return sentiment, translated
    except Exception as e:
        return "NEUTRAL", news_text


# ================= КОМАНДЫ БОТА =================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not check_auth(message): return
    bot.reply_to(message, 
        f"🤖 <b>Торговый бот Уровень 3 (BTC)</b>\n\n"
        "📊 <b>Статистика и отчеты:</b>\n"
        "/stats или /report - 📈 Полная интерактивная статистика (WinRate, PnL, Fees)\n"
        "/export_csv - 📥 Выгрузить историю сделок в CSV (Excel)\n"
        "/last_trades - 🧾 Список последних 10 закрытых сделок\n"
        "/profit - 💸 Быстрый PnL за последние 50 сделок\n\n"
        "⚡ <b>Управление торговлей:</b>\n"
        "/start_auto - Запустить автоторговлю\n"
        "/stop_auto - Остановить автоторговлю\n"
        "/status - Статус бота и параметров\n"
        "/force_buy - Мгновенно открыть лонг по рынку\n"
        "/dca - Принудительно усреднить позицию\n"
        "/close_all - Закрыть все позиции\n\n"
        "⚙️ <b>Параметры и ИИ:</b>\n"
        "/set_aggression [high/normal] - Режим активности\n"
        "/set_dynamic_step [1/0] - Вкл/Выкл умный шаг (ATR)\n"
        "/set_trailing [DROP] - Настроить откат трейлинга\n"
        "/check_ai - Запросить ИИ-анализ рынка\n"
        "/balance, /price, /set_early_cut, /set_budget, /set_step, /set_qty, /set_sl", parse_mode="HTML"
    )

@bot.message_handler(commands=['force_buy'])
def force_buy_cmd(message):
    if not check_auth(message): return
    try:
        response = session.get_tickers(category="linear", symbol="BTCUSDT")
        current_price = float(response['result']['list'][0]['lastPrice'])
        qty = str(float(state.get("qty", 0.001)))
        
        pos_resp = session.get_positions(category="linear", symbol="BTCUSDT")
        pos_size = 0.0
        if pos_resp.get('result') and pos_resp['result'].get('list'):
            pos_size = safe_float(pos_resp['result']['list'][0].get('size', 0))
            
        if pos_size > 0:
            bot.reply_to(message, f"⚠️ У вас уже открыта позиция {pos_size} BTC! Для добавления объема используйте /dca.", parse_mode="HTML")
            return
            
        session.place_order(category="linear", symbol="BTCUSDT", side="Buy", orderType="Market", qty=qty)
        log_trade("Force Buy", float(qty), current_price, 0.0)
        
        state["auto_trade"] = True
        state["base_price"] = current_price
        state["dca_step"] = 1
        state["highest_price"] = current_price
        state["lowest_price"] = current_price
        state["trailing_active"] = False
        state["breakeven_notified"] = False
        state["trade_direction"] = "Buy"
        state["auto_paused"] = False
        state.pop("waiting_entry_notified", None)
        save_state(state)
        
        bot.reply_to(message, f"⚡ <b>Принудительный вход выполнен!</b>\nКуплено: <b>{qty} BTC</b> по цене <b>{current_price:.2f}</b>\nСделка передана под автосопровождение бота (Трейлинг + DCA).", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при входе: {e}")

@bot.message_handler(commands=['set_aggression'])
def set_aggression_cmd(message):
    if not check_auth(message): return
    try:
        parts = message.text.split()
        if len(parts) > 1 and parts[1].lower() in ['high', 'normal']:
            val = parts[1].lower()
            state["aggression"] = val
            save_state(state)
            desc = "Максимально активный вход: вливается в растущий рынок даже при высоком RSI" if val == "high" else "Сбалансированный вход"
            bot.reply_to(message, f"✅ Режим активности установлен: <b>{val.upper()}</b>\n{desc}", parse_mode="HTML")
        else:
            bot.reply_to(message, "Использование: <code>/set_aggression high</code> (или <code>normal</code>)\n<i>По умолчанию: HIGH (агрессивно вливается в растущий тренд)</i>", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=['set_dynamic_step'])
def set_dynamic_step_cmd(message):
    if not check_auth(message): return
    try:
        parts = message.text.split()
        if len(parts) > 1:
            val = int(parts[1])
            state["dynamic_step"] = (val == 1)
            save_state(state)
            status = "ВКЛЮЧЕН" if val == 1 else "ВЫКЛЮЧЕН"
            bot.reply_to(message, f"✅ Динамический шаг (ATR) {status}")
        else:
            bot.reply_to(message, "Использование: /set_dynamic_step 1 (или 0)")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

@bot.message_handler(commands=['set_trailing'])
def set_trailing_cmd(message):
    if not check_auth(message): return
    try:
        parts = message.text.split()
        if len(parts) > 1:
            drop = float(parts[1])
            state["trailing_drop"] = drop
            save_state(state)
            bot.reply_to(message, f"✅ Трейлинг откат установлен на {drop}")
        else:
            bot.reply_to(message, "Использование: /set_trailing 100")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

@bot.message_handler(commands=['start_auto'])
def start_auto(message):
    if not check_auth(message): return
    try:
        response = session.get_tickers(category="linear", symbol="BTCUSDT")
        current_price = float(response['result']['list'][0]['lastPrice'])
        state["base_price"] = current_price
        state["auto_trade"] = True
        state["trailing_active"] = False
        state["auto_paused"] = False
        save_state(state)
        bot.reply_to(message, f"✅ Автоторговля BTC ЗАПУЩЕНА!\nБазовая цена: {current_price}")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=['stop_auto'])
def stop_auto(message):
    if not check_auth(message): return
    state["auto_trade"] = False
    state["auto_paused"] = False
    save_state(state)
    bot.reply_to(message, f"⏸ Автоторговля BTC ОСТАНОВЛЕНА вручную (авто-возобновление отключено).")

@bot.message_handler(commands=['status'])
def get_status(message):
    if not check_auth(message): return
    status_text = (
        f"📊 <b>Статус бота</b>\n\n"
        f"Автоторговля: {'✅ ВКЛ' if state.get('auto_trade') else '⏸ ВЫКЛ'}\n"
        f"Режим входа: <b>{state.get('aggression', 'high').upper()}</b> (Всегда агрессивен + RSI Trend Flow)\n"
        f"Базовая цена: {state.get('base_price', 0)}\n"
        f"Динамический шаг (ATR): {'✅ ВКЛ' if state.get('dynamic_step') else '⏸ ВЫКЛ'}\n"
        f"Шаг (статика): {state.get('step', 500)}\n"
        f"Объем: {state.get('qty', 0.001)}\n"
        f"Бюджет: {state.get('budget', 240)} USDT\n"
        f"Stop-Loss: {state.get('sl_percent', 15.0)}%\n"
        f"Сброс при просадке (Early Cut): <b>-{state.get('early_cut_loss', 3.0):.2f} USDT</b>\n"
        f"Трейлинг откат: {state.get('trailing_drop', 100)}\n"
        f"Активен трейлинг: {'✅ ДА' if state.get('trailing_active') else 'НЕТ'}\n"
    )
    bot.reply_to(message, status_text, parse_mode="HTML")

@bot.message_handler(commands=['check_keys'])
def check_keys_cmd(message):
    if not check_auth(message): return
    key_status = "Установлен" if API_KEY else "НЕ УСТАНОВЛЕН"
    secret_status = "Установлен" if API_SECRET else "НЕ УСТАНОВЛЕН"
    
    masked_key = f"{API_KEY[:4]}...{API_KEY[-4:]}" if API_KEY and len(API_KEY) > 8 else "Нет"
    
    reply = (
        f"🔑 <b>Диагностика API ключей:</b>\n"
        f"<b>API_KEY:</b> {key_status} ({masked_key})\n"
        f"<b>API_SECRET:</b> {secret_status}\n"
        f"<b>Длина KEY:</b> {len(API_KEY) if API_KEY else 0} симв.\n"
        f"<b>Длина SECRET:</b> {len(API_SECRET) if API_SECRET else 0} симв.\n\n"
        f"Если ключи не совпадают с твоими (с теми, что ты создал в Testnet Bybit), значит Railway не обновил их! Удали переменные, добавь заново и нажми Restart."
    )
    bot.reply_to(message, reply, parse_mode="HTML")

@bot.message_handler(commands=['check_ai'])
def check_ai_manual(message):
    if not check_auth(message): return
    bot.reply_to(message, f"🧠 Собираю новости и тех. анализ для BTC...")
    news = fetch_crypto_news()
    klines = get_klines("BTCUSDT", "60", 30)
    rsi = calculate_rsi(klines)
    atr = calculate_atr(klines)
    
    try:
        response = session.get_tickers(category="linear", symbol="BTCUSDT")
        current_price = float(response['result']['list'][0]['lastPrice'])
    except Exception:
        current_price = 0.0
        
    sentiment, translated = analyze_sentiment(news, rsi, atr, current_price)
    
    bot.reply_to(message, f"📊 <b>Тех. данные:</b>\nЦена: {current_price}\nRSI: {rsi:.1f}\nATR: {atr:.1f}\n\n<b>ИИ-Сентимент:</b> {sentiment}\n\n<b>Отчет ИИ:</b>\n{translated}", parse_mode="HTML")

@bot.message_handler(commands=['balance'])
def get_balance(message):
    if not check_auth(message): return
    try:
        response = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        
        # Если аккаунт не единый (UNIFIED), пробуем деривативный (CONTRACT)
        if not response.get('result') or not response['result'].get('list'):
            response = session.get_wallet_balance(accountType="CONTRACT", coin="USDT")
            
        if not response.get('result') or not response['result'].get('list'):
            bot.reply_to(message, "💰 <b>Баланс:</b> 0.00 USDT\n(Возможно, аккаунт пуст или ключи не имеют нужных прав)", parse_mode="HTML")
            return
            
        coins = response['result']['list'][0].get('coin', [])
        balance = 0.0
        for c in coins:
            if c.get('coin') == 'USDT':
                balance = safe_float(c.get('walletBalance', 0))
                break
                
        bot.reply_to(message, f"💰 <b>Баланс:</b> {balance:.2f} USDT", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}\nПожалуйста, проверьте правильность API ключей Bybit и тип вашего аккаунта.")

@bot.message_handler(commands=['price'])
def get_price(message):
    if not check_auth(message): return
    try:
        response = session.get_tickers(category="linear", symbol="BTCUSDT")
        price = float(response['result']['list'][0]['lastPrice'])
        bot.reply_to(message, f"📈 Текущая цена <b>BTCUSDT</b>: {price}", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=['set_mode'])
def set_mode(message):
    if not check_auth(message): return
    try:
        mode = message.text.split()[1].lower()
        if mode == 'scalp':
            state["step"] = 250
            state["trailing_drop"] = 150
            bot.reply_to(message, "🚀 <b>Режим СКАЛЬПИНГ включен!</b>\nБот ловит микро-движения. Шаг: 250, Откат: 150.", parse_mode="HTML")
        elif mode == 'standard':
            state["step"] = 500
            state["trailing_drop"] = 250
            bot.reply_to(message, "⚖️ <b>Режим СТАНДАРТ включен!</b>\nБаланс риска и прибыли. Шаг: 500, Откат: 250.", parse_mode="HTML")
        elif mode == 'safe':
            state["step"] = 800
            state["trailing_drop"] = 400
            bot.reply_to(message, "🛡 <b>Режим БЕЗОПАСНЫЙ включен!</b>\nТолько крупные движения. Шаг: 800, Откат: 400.", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ Неизвестный режим.\nДоступно: scalp, standard, safe")
        save_state(state)
    except:
        bot.reply_to(message, "Использование: /set_mode [scalp|standard|safe]")

@bot.message_handler(commands=['set_step'])
def set_step(message):
    if not check_auth(message): return
    try:
        val = float(message.text.split()[1])
        state["step"] = val
        save_state(state)
        bot.reply_to(message, f"✅ Шаг сетки установлен на {val}")
    except:
        bot.reply_to(message, "Использование: /set_step 50")

@bot.message_handler(commands=['set_qty'])
def set_qty(message):
    if not check_auth(message): return
    try:
        val = float(message.text.split()[1])
        state["qty"] = val
        save_state(state)
        bot.reply_to(message, f"✅ Объем установлен на {val}")
    except:
        bot.reply_to(message, "Использование: /set_qty 0.001")

@bot.message_handler(commands=['dca'])
def force_dca(message):
    if not check_auth(message): return
    try:
        buy_qty = str(state.get("qty", 0.005))
        if len(message.text.split()) > 1:
            buy_qty = str(float(message.text.split()[1]))
            
        response = session.get_tickers(category="linear", symbol="BTCUSDT")
        current_price = float(response['result']['list'][0]['lastPrice'])
        
        session.place_order(category="linear", symbol="BTCUSDT", side="Buy", orderType="Market", qty=buy_qty)
        log_trade("Manual DCA", float(buy_qty), current_price, 0.0)
        
        # Обновляем базовую цену, чтобы бот не купил сразу же еще раз по старому уровню
        state["base_price"] = current_price
        state["trailing_active"] = False
        state["dca_step"] = state.get("dca_step", 0) + 1
        state.pop("highest_price", None)
        state.pop("breakeven_notified", None)
        save_state(state)
        
        bot.reply_to(message, f"🚑 <b>Принудительное усреднение!</b>\nКуплено {buy_qty} BTC по {current_price}.\nСредняя цена входа значительно снизилась!", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при усреднении: {e}")

@bot.message_handler(commands=['set_early_cut'])
def handle_set_early_cut(message):
    if not check_auth(message): return
    try:
        val = float(message.text.split()[1])
        if val <= 0:
            bot.reply_to(message, "❌ Значение должно быть больше 0.")
            return
        state["early_cut_loss"] = val
        save_state(state)
        bot.reply_to(message, f"✅ Порог сброса позиции при просадке установлен на <b>-{val:.2f} USDT</b>.\nБот теперь НЕ закроет сделку, пока просадка меньше этой суммы.", parse_mode="HTML")
    except (IndexError, ValueError):
        bot.reply_to(message, "Использование: <code>/set_early_cut 3.0</code> (установить допустимый минус в USDT перед сбросом)", parse_mode="HTML")

@bot.message_handler(commands=['set_sl'])
def set_sl(message):
    if not check_auth(message): return
    try:
        val = float(message.text.split()[1])
        state["sl_percent"] = val
        save_state(state)
        bot.reply_to(message, f"✅ Stop-Loss (%) установлен на {val}%")
    except:
        bot.reply_to(message, "Использование: /set_sl 15")

@bot.message_handler(commands=['set_budget'])
def set_budget(message):
    if not check_auth(message): return
    try:
        val = float(message.text.split()[1])
        state["budget"] = val
        save_state(state)
        bot.reply_to(message, f"✅ Бюджет установлен на {val} USDT")
    except:
        bot.reply_to(message, "Использование: /set_budget 1000")

@bot.message_handler(commands=['close_all'])
def close_all(message):
    if not check_auth(message): return
    try:
        pos_resp = session.get_positions(category="linear", symbol="BTCUSDT")
        for p in pos_resp['result']['list']:
            if safe_float(p.get('size', 0)) > 0:
                side = "Sell" if p['side'] == "Buy" else "Buy"
                session.place_order(category="linear", symbol="BTCUSDT", side=side, orderType="Market", qty=p['size'], reduceOnly=True)
        state["dca_step"] = 0
        state.pop("tp_level", None)
        state.pop("budget_exceeded_notified", None)
        state.pop("margin_error_notified", None)
        save_state(state)
        bot.reply_to(message, f"✅ Все позиции по BTCUSDT закрыты!")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=['set_leverage'])
def set_leverage(message):
    if not check_auth(message): return
    try:
        val = int(message.text.split()[1])
        session.set_leverage(category="linear", symbol="BTCUSDT", buyLeverage=str(val), sellLeverage=str(val))
        bot.reply_to(message, f"✅ Плечо установлено на {val}x")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: (Возможно плечо уже стоит {val}x) {e}")


@bot.message_handler(commands=['auto_pilot'])
def auto_pilot_toggle(message):
    if not check_auth(message): return
    current = state.get("auto_pilot", False)
    state["auto_pilot"] = not current
    save_state(state)
    
    if state["auto_pilot"]:
        bot.reply_to(message, "🧠 <b>АВТОПИЛОТ ВКЛЮЧЕН!</b>\nБот теперь сам высчитывает шаг (Step), откат (Trailing) и объем сделки (Qty) в зависимости от текущей волатильности (ATR) и вашего бюджета.", parse_mode="HTML")
    else:
        bot.reply_to(message, "🧠 Автопилот отключен. Возврат к ручным настройкам.", parse_mode="HTML")

# ================= СИСТЕМА ПОЛНОЙ СТАТИСТИКИ И ОТЧЕТНОСТИ =================

def fetch_closed_trades(period_hours=None, limit=100):
    """
    Загружает закрытые сделки напрямую из Bybit Closed PnL API.
    При необходимости фильтрует по заданному окну в часах (например, 24ч, 168ч, 720ч).
    """
    try:
        try:
            resp = session.get_closed_pnl(category="linear", symbol="BTCUSDT", limit=min(limit, 100))
        except Exception:
            resp = session.get_closed_pnl(category="linear", limit=min(limit, 100))
            
        raw_list = resp.get('result', {}).get('list', [])
        if not raw_list:
            return []
            
        now_ms = time.time() * 1000
        cutoff_ms = (now_ms - period_hours * 3600 * 1000) if period_hours else 0
        
        filtered = []
        for t in raw_list:
            t_time = safe_float(t.get('updatedTime') or t.get('createdTime'), 0)
            if cutoff_ms == 0 or t_time >= cutoff_ms:
                filtered.append(t)
        return filtered
    except Exception as e:
        print(f"Error fetching closed trades: {e}")
        return []

def compute_trade_metrics(trades):
    """
    Вычисляет полную профессиональную аналитику торговли:
    - Net PnL, Win Rate, Profit Factor, Gross Profit/Loss, Avg Win/Loss, Best/Worst Trade, Fees, Volume
    """
    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "total_fees": 0.0,
            "total_volume": 0.0
        }
        
    wins_list = []
    losses_list = []
    be_list = []
    pnls = []
    total_fees = 0.0
    total_volume = 0.0
    
    for t in trades:
        pnl = safe_float(t.get('closedPnl', 0))
        qty = safe_float(t.get('qty', 0))
        exit_p = safe_float(t.get('avgExitPrice', 0)) or safe_float(t.get('orderPrice', 0))
        fee = safe_float(t.get('cumExecFee', 0)) or safe_float(t.get('execFee', 0))
        
        total_fees += abs(fee)
        total_volume += (qty * exit_p)
        pnls.append(pnl)
        
        if pnl > 0.00001:
            wins_list.append(pnl)
        elif pnl < -0.00001:
            losses_list.append(abs(pnl))
        else:
            be_list.append(pnl)
            
    total_count = len(trades)
    wins_count = len(wins_list)
    losses_count = len(losses_list)
    be_count = len(be_list)
    
    net_pnl = sum(pnls)
    gross_profit = sum(wins_list)
    gross_loss = sum(losses_list)
    
    win_rate = (wins_count / total_count * 100.0) if total_count > 0 else 0.0
    
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = 99.99
    else:
        profit_factor = 1.0
        
    avg_win = (gross_profit / wins_count) if wins_count > 0 else 0.0
    avg_loss = (gross_loss / losses_count) if losses_count > 0 else 0.0
    best_trade = max(pnls) if pnls else 0.0
    worst_trade = min(pnls) if pnls else 0.0
    
    return {
        "total_trades": total_count,
        "wins": wins_count,
        "losses": losses_count,
        "breakeven": be_count,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "total_fees": total_fees,
        "total_volume": total_volume
    }

def get_stats_inline_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    b1 = InlineKeyboardButton("⏱ 24 часа", callback_data="stats_24h")
    b2 = InlineKeyboardButton("📅 7 дней", callback_data="stats_7d")
    b3 = InlineKeyboardButton("🗓 30 дней", callback_data="stats_30d")
    b4 = InlineKeyboardButton("🏆 Все время", callback_data="stats_all")
    b5 = InlineKeyboardButton("🧾 Посл. 10 сделок", callback_data="stats_recent")
    b6 = InlineKeyboardButton("📥 Скачать CSV", callback_data="stats_csv")
    markup.add(b1, b2)
    markup.add(b3, b4)
    markup.add(b5, b6)
    return markup

def format_stats_report(metrics, period_name="За все время"):
    pnl = metrics["net_pnl"]
    pnl_sign = "+" if pnl > 0 else ""
    pnl_badge = "🟢 В ПЛЮСЕ" if pnl > 0 else ("🔴 В МИНУСЕ" if pnl < 0 else "⚪ В НОЛЬ")
    
    filled_blocks = min(10, max(0, int(round(metrics["win_rate"] / 10))))
    bar = "█" * filled_blocks + "░" * (10 - filled_blocks)
    
    text = (
        f"📊 <b>ПОЛНЫЙ ОТЧЕТ И СТАТИСТИКА БОТА</b>\n"
        f"Период: <b>{period_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Чистый PnL:</b> <code>{pnl_sign}{pnl:.2f} USDT</code> ({pnl_badge})\n\n"
        f"🎯 <b>Винрейт (Win Rate):</b> <b>{metrics['win_rate']:.1f}%</b>\n"
        f"<code>[{bar}]</code>\n"
        f"• Всего закрыто сделок: <b>{metrics['total_trades']}</b>\n"
        f"• Прибыльных (Win): <b>{metrics['wins']}</b> 🟢\n"
        f"• Убыточных (Loss): <b>{metrics['losses']}</b> 🔴\n"
        f"• В безубыток (BE): <b>{metrics['breakeven']}</b> ⚪\n\n"
        f"⚖️ <b>Показатели эффективности:</b>\n"
        f"• Профит-фактор: <b>{metrics['profit_factor']:.2f}</b>\n"
        f"• Общая прибыль (Gross): <code>+{metrics['gross_profit']:.2f} USDT</code>\n"
        f"• Общий убыток (Gross): <code>-{metrics['gross_loss']:.2f} USDT</code>\n"
        f"• Средний плюс (Avg Win): <code>+{metrics['avg_win']:.2f} USDT</code>\n"
        f"• Средний минус (Avg Loss): <code>-{metrics['avg_loss']:.2f} USDT</code>\n"
        f"• Лучшая сделка: <code>+{metrics['best_trade']:.2f} USDT</code> 🚀\n"
        f"• Худшая сделка: <code>{metrics['worst_trade']:.2f} USDT</code> 🛡\n\n"
        f"💳 <b>Комиссии биржи:</b> <code>{metrics['total_fees']:.2f} USDT</code>\n"
        f"📦 <b>Оборот (Volume):</b> <code>~{metrics['total_volume']:.2f} USDT</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Нажмите кнопки ниже для переключения периода или выгрузки:</i>"
    )
    return text

def format_recent_trades_message(trades, count=10):
    if not trades:
        return "ℹ️ Нет закрытых сделок в истории биржи."
        
    trades_subset = trades[:count]
    lines = [f"🧾 <b>Последние {len(trades_subset)} закрытых сделок (Bybit)</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    
    for i, t in enumerate(trades_subset, 1):
        t_time_ms = safe_float(t.get('updatedTime') or t.get('createdTime'), 0)
        dt_str = time.strftime('%d.%m %H:%M', time.gmtime(t_time_ms / 1000)) if t_time_ms else "Н/Д"
        side = t.get('side', 'Buy')
        action_name = "LONG" if side == "Buy" else "SHORT"
        qty = safe_float(t.get('qty', 0))
        entry_p = safe_float(t.get('avgEntryPrice', 0)) or safe_float(t.get('orderPrice', 0))
        exit_p = safe_float(t.get('avgExitPrice', 0))
        pnl = safe_float(t.get('closedPnl', 0))
        symbol = t.get('symbol', 'BTCUSDT')
        
        sign = "+" if pnl > 0 else ""
        icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
        
        lines.append(
            f"{i}️⃣ <b>{dt_str}</b> | <b>{action_name}</b> {qty} {symbol.replace('USDT','')}\n"
            f"   Вход: <code>{entry_p:.1f}</code> ➔ Выход: <code>{exit_p:.1f}</code>\n"
            f"   PnL: <b>{sign}{pnl:.2f} USDT</b> {icon}"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

def generate_trades_csv_content(trades):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "DateTime_UTC", "Symbol", "Side", "Qty", 
        "AvgEntryPrice", "AvgExitPrice", "ClosedPnL_USDT", 
        "Fee_USDT", "Leverage", "OrderID"
    ])
    
    for t in trades:
        t_time_ms = safe_float(t.get('updatedTime') or t.get('createdTime'), 0)
        dt_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t_time_ms / 1000)) if t_time_ms else ""
        writer.writerow([
            dt_str,
            t.get('symbol', 'BTCUSDT'),
            t.get('side', ''),
            t.get('qty', ''),
            t.get('avgEntryPrice', ''),
            t.get('avgExitPrice', ''),
            t.get('closedPnl', ''),
            t.get('cumExecFee', '') or t.get('execFee', ''),
            t.get('leverage', ''),
            t.get('orderId', '')
        ])
    return output.getvalue().encode('utf-8')

@bot.message_handler(commands=['stats', 'report'])
def stats_cmd(message):
    if not check_auth(message): return
    try:
        trades = fetch_closed_trades(period_hours=None)
        metrics = compute_trade_metrics(trades)
        text = format_stats_report(metrics, period_name="За все время (история Bybit)")
        bot.reply_to(message, text, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при получении статистики: {e}")

@bot.message_handler(commands=['export_csv'])
def export_csv_cmd(message):
    if not check_auth(message): return
    try:
        trades = fetch_closed_trades(limit=100)
        if not trades:
            bot.reply_to(message, "ℹ️ На бирже пока нет закрытых сделок для экспорта.")
            return
        csv_bytes = generate_trades_csv_content(trades)
        csv_file = io.BytesIO(csv_bytes)
        csv_file.name = f"trades_report_BTC_{time.strftime('%Y%m%d_%H%M')}.csv"
        bot.send_document(
            message.chat.id, 
            csv_file, 
            caption=f"📊 <b>Экспорт торговой истории Bybit</b>\nВсего сделок в выгрузке: {len(trades)}\nФормат: CSV (Excel / Google Sheets).",
            parse_mode="HTML"
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при генерации CSV: {e}")

@bot.message_handler(commands=['last_trades'])
def last_trades_cmd(message):
    if not check_auth(message): return
    try:
        trades = fetch_closed_trades(limit=10)
        text = format_recent_trades_message(trades, count=10)
        bot.reply_to(message, text, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith('stats_'))
def handle_stats_callbacks(call):
    if call.from_user.id != ALLOWED_USER_ID:
        try:
            bot.answer_callback_query(call.id, "Доступ запрещен!")
        except Exception:
            pass
        return
        
    data = call.data
    try:
        if data == "stats_24h":
            trades = fetch_closed_trades(period_hours=24)
            metrics = compute_trade_metrics(trades)
            text = format_stats_report(metrics, period_name="За последние 24 часа")
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
            bot.answer_callback_query(call.id, "Статистика за 24 часа")
        elif data == "stats_7d":
            trades = fetch_closed_trades(period_hours=168)
            metrics = compute_trade_metrics(trades)
            text = format_stats_report(metrics, period_name="За последние 7 дней")
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
            bot.answer_callback_query(call.id, "Статистика за 7 дней")
        elif data == "stats_30d":
            trades = fetch_closed_trades(period_hours=720)
            metrics = compute_trade_metrics(trades)
            text = format_stats_report(metrics, period_name="За последние 30 дней")
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
            bot.answer_callback_query(call.id, "Статистика за 30 дней")
        elif data == "stats_all":
            trades = fetch_closed_trades(period_hours=None)
            metrics = compute_trade_metrics(trades)
            text = format_stats_report(metrics, period_name="За все время (история Bybit)")
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
            bot.answer_callback_query(call.id, "Статистика за все время")
        elif data == "stats_recent":
            trades = fetch_closed_trades(limit=10)
            text = format_recent_trades_message(trades, count=10)
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
            bot.answer_callback_query(call.id, "Последние 10 сделок")
        elif data == "stats_csv":
            bot.answer_callback_query(call.id, "Формирую CSV...")
            trades = fetch_closed_trades(limit=100)
            if not trades:
                bot.send_message(call.message.chat.id, "ℹ️ Нет закрытых сделок для экспорта.")
                return
            csv_bytes = generate_trades_csv_content(trades)
            csv_file = io.BytesIO(csv_bytes)
            csv_file.name = f"trading_report_BTC_{time.strftime('%Y%m%d_%H%M')}.csv"
            bot.send_document(call.message.chat.id, csv_file, caption="📥 <b>Полный отчет по сделкам Bybit (CSV)</b>\nФайл можно открыть в Excel или Google Таблицах.", parse_mode="HTML")
    except Exception as e:
        try:
            bot.answer_callback_query(call.id, f"Ошибка: {e}")
        except Exception:
            pass

@bot.message_handler(commands=['profit'])
def profit_cmd(message):
    if not check_auth(message): return
    try:
        trades = fetch_closed_trades(limit=50)
        metrics = compute_trade_metrics(trades)
        pnl = metrics["net_pnl"]
        sign = "+" if pnl > 0 else ""
        badge = "🟢 В ПЛЮСЕ" if pnl > 0 else ("🔴 В МИНУСЕ" if pnl < 0 else "⚪")
        
        reply = (
            f"💸 <b>Финансовый результат (последние 50 сделок):</b>\n"
            f"Чистый PnL: <b>{sign}{pnl:.2f} USDT</b> ({badge})\n"
            f"Винрейт: <b>{metrics['win_rate']:.1f}%</b> ({metrics['wins']}W / {metrics['losses']}L)\n"
            f"Комиссии биржи: <code>{metrics['total_fees']:.2f} USDT</code>\n\n"
            f"<i>Для детальной аналитики по периодам и выгрузки нажмите кнопки ниже или используйте /stats</i>"
        )
        bot.reply_to(message, reply, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")


# ================= ОСНОВНОЙ ЦИКЛ БОТА =================

def monitor_price():
    global last_ai_check, last_report_time
    last_ai_check = 0
    last_report_time = 0
    
    while True:
        try:
            # Не загружаем state из файла каждый цикл, используем глобальный словарь state!
            # Это устраняет баг рассинхронизации.

            # --- РЕГУЛЯРНЫЙ ОТЧЕТ (Раз в 10 минут) ---
            import time as time_module
            if time_module.time() - last_report_time >= 600:
                last_report_time = time_module.time()
                try:
                    # Получаем текущие данные для отчета
                    rep_ticker = session.get_tickers(category="linear", symbol="BTCUSDT")
                    rep_price = float(rep_ticker['result']['list'][0]['lastPrice'])
                    
                    rep_pos = session.get_positions(category="linear", symbol="BTCUSDT")
                    r_pos_size = 0.0
                    r_unrealised = 0.0
                    if rep_pos.get('result') and rep_pos['result'].get('list'):
                        p_data = rep_pos['result']['list'][0]
                        r_pos_size = safe_float(p_data.get('size', 0))
                        r_unrealised = safe_float(p_data.get('unrealisedPnl', 0))
                        
                    resp_pnl = session.get_closed_pnl(category="linear", limit=50)
                    total_pnl = sum(safe_float(x.get('closedPnl', 0)) for x in resp_pnl['result']['list'])
                    
                    status_str = "АКТИВНА" if state.get("auto_trade") else "ПАУЗА (ИИ)" if state.get("auto_paused") else "ВЫКЛ"
                    
                    report_text = (
                        f"📝 <b>Регулярный отчет (BTCUSDT)</b>\n\n"
                        f"🤖 Статус: Автоторговля <b>{status_str}</b>\n"
                        f"📈 Текущая цена: <b>{rep_price:.1f} USDT</b>\n"
                        f"🎯 Базовая цена: <code>{state.get('base_price', rep_price)}</code>\n"
                        f"📊 Позиция: <b>{r_pos_size} BTC</b> (uPnL: <code>{r_unrealised:.2f} USDT</code>)\n"
                        f"💸 Прибыль (посл. 50): <b>{total_pnl:.2f} USDT</b>"
                    )
                    quick_kb = InlineKeyboardMarkup(row_width=2)
                    quick_kb.add(
                        InlineKeyboardButton("📊 Полная статистика", callback_data="stats_all"),
                        InlineKeyboardButton("📥 Скачать CSV", callback_data="stats_csv")
                    )
                    bot.send_message(ALLOWED_USER_ID, report_text, reply_markup=quick_kb, parse_mode="HTML")
                except Exception as e:
                    print(f"Report error: {e}")

            # --- ЕЖЕДНЕВНЫЙ АВТОМАТИЧЕСКИЙ ОТЧЕТ (Раз в сутки) ---
            try:
                utc_today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                if state.get("last_daily_report_date") != utc_today:
                    d_trades = fetch_closed_trades(period_hours=24)
                    d_metrics = compute_trade_metrics(d_trades)
                    
                    bal_resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
                    if not bal_resp.get('result') or not bal_resp['result'].get('list'):
                        bal_resp = session.get_wallet_balance(accountType="CONTRACT", coin="USDT")
                    cur_bal = 0.0
                    if bal_resp.get('result') and bal_resp['result'].get('list'):
                        for c in bal_resp['result']['list'][0].get('coin', []):
                            if c.get('coin') == 'USDT':
                                cur_bal = safe_float(c.get('walletBalance', 0))
                                break
                    pnl_sign = "+" if d_metrics['net_pnl'] > 0 else ""
                    daily_msg = (
                        f"🌅 <b>ИТОГОВЫЙ ОТЧЕТ ЗА ДЕНЬ ({utc_today} UTC)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 <b>PnL за 24 часа:</b> <code>{pnl_sign}{d_metrics['net_pnl']:.2f} USDT</code>\n"
                        f"🎯 <b>Сделок за день:</b> {d_metrics['total_trades']} (Win: {d_metrics['wins']} | Loss: {d_metrics['losses']})\n"
                        f"📈 <b>Винрейт (24ч):</b> <b>{d_metrics['win_rate']:.1f}%</b>\n"
                        f"⚖️ <b>Профит-фактор:</b> <b>{d_metrics['profit_factor']:.2f}</b>\n"
                        f"💳 <b>Комиссии биржи:</b> <code>{d_metrics['total_fees']:.2f} USDT</code>\n"
                        f"🏦 <b>Баланс кошелька:</b> <b>{cur_bal:.2f} USDT</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"<i>Для детальной истории введите /stats или нажмите кнопку:</i>"
                    )
                    bot.send_message(ALLOWED_USER_ID, daily_msg, reply_markup=get_stats_inline_keyboard(), parse_mode="HTML")
                    state["last_daily_report_date"] = utc_today
                    save_state(state)
            except Exception as d_err:
                print(f"Daily digest error: {d_err}")
                    
            if state.get("auto_trade"):

                response = session.get_tickers(category="linear", symbol="BTCUSDT")
                current_price = float(response['result']['list'][0]['lastPrice'])
                
                pos_resp = session.get_positions(category="linear", symbol="BTCUSDT")
                pos_size = 0.0
                unrealised_pnl = 0.0
                position_im = 0.0
                avg_price = current_price
                
                if pos_resp.get('result') and pos_resp['result'].get('list'):
                    pos_data = pos_resp['result']['list'][0]
                    pos_size = safe_float(pos_data.get('size', 0))
                    unrealised_pnl = safe_float(pos_data.get('unrealisedPnl', 0))
                    position_im = safe_float(pos_data.get('positionIM', 0))
                    avg_price = safe_float(pos_data.get('avgPrice'), current_price) if pos_size > 0 else current_price

                budget = state.get("budget", 240.0)
                step = float(state.get("step", 500))
                
                # --- AUTO PILOT LOGIC ---
                if state.get("auto_pilot"):
                    try:
                        klines_ap = get_klines("BTCUSDT", "15", 30)
                        atr_ap = calculate_atr(klines_ap)
                        if atr_ap > 0:
                            # Адаптируем шаг и откат под волатильность
                            step = max(100.0, float(atr_ap * 1.5))
                            state["step"] = step
                            state["trailing_drop"] = max(50.0, float(atr_ap * 0.8))
                            
                            # Адаптируем объем: торгуем на 50% от выделенного бюджета
                            calc_qty = round((budget * 0.15) / current_price, 3)  # Используем только 15% депозита для безопасности
                            state["qty"] = max(0.001, calc_qty)
                    except Exception as e:
                        print("AutoPilot Error:", e)
                # ------------------------
                
                                # --- AI CHECK (Раз в 10 мин) ---
                import time as time_module
                if time_module.time() - last_ai_check >= 1800:
                    last_ai_check = time_module.time()
                    try:
                        news = fetch_crypto_news()
                        klines = get_klines("BTCUSDT", "60", 30)
                        rsi = calculate_rsi(klines)
                        atr = calculate_atr(klines)
                        sentiment, translated = analyze_sentiment(news, rsi, atr, current_price)
                        bot.send_message(ALLOWED_USER_ID, f"🧠 <b>Анализ рынка ИИ (BTCUSDT)</b>\nСентимент: {sentiment}\nRSI: {rsi:.1f}, ATR: {atr:.1f}\n{translated}", parse_mode="HTML")
                        if sentiment == "NEGATIVE":
                            state["auto_trade"] = False
                            state["auto_paused"] = True
                            save_state(state)
                            bot.send_message(ALLOWED_USER_ID, f"🚨 <b>ИИ-ТРЕВОГА (BTCUSDT)</b>\nРынок негативен. Торговля приостановлена.", parse_mode="HTML")
                            continue
                    except Exception as e:
                        print("AI Check error:", e)

                                # Проверка паузы после раннего защитного сброса
                now_ts = time_module.time()
                if now_ts < state.get("dump_pause_until", 0):
                    rem_sec = int(state.get("dump_pause_until", 0) - now_ts)
                    if now_ts - state.get("last_pause_msg", 0) > 180:
                        bot.send_message(ALLOWED_USER_ID, f"⏸ <b>Пауза после сброса позиции</b>\nРынок штормит. Жду стабилизации еще {rem_sec // 60} мин {rem_sec % 60} сек...", parse_mode="HTML")
                        state["last_pause_msg"] = now_ts
                        save_state(state)
                    continue

                # --- 1. ЕСЛИ НЕТ ПОЗИЦИИ ---
                if pos_size == 0 and not state.get("auto_paused"):
                    # Умный адаптивный вход: всегда агрессивен, вливается в растущий тренд с высоким RSI
                    klines = get_klines("BTCUSDT", "15", 100)
                    aggression = state.get("aggression", "high")
                    should_enter, entry_reason, entry_rsi, market_trend = analyze_market_entry(klines, current_price, aggression)
                    
                    if not should_enter:
                        import time
                        if time.time() - state.get("last_entry_wait_msg", 0) > 300: # Напоминаем раз в 5 минут
                            bot.send_message(
                                ALLOWED_USER_ID,
                                f"""⏳ <b>Слежу за рынком BTC</b>
Статус: {entry_reason}
Текущая цена: <b>{current_price:.1f}</b> | RSI(15m): <b>{entry_rsi:.1f}</b>
<i>Режим: {aggression.upper()} (вливание в тренд включено)</i>""",
                                parse_mode="HTML"
                            )
                            state["last_entry_wait_msg"] = time.time()
                            save_state(state)
                        continue

                    trade_side = "Buy"
                    state.pop("waiting_entry_notified", None)
                    qty = str(float(state.get("qty", 0.001)))
                    session.place_order(category="linear", symbol="BTCUSDT", side=trade_side, orderType="Market", qty=qty)
                    log_trade(f"First {trade_side}", float(qty), current_price, 0.0)
                    state["base_price"] = current_price
                    state["dca_step"] = 1
                    state["highest_price"] = current_price
                    state["lowest_price"] = current_price
                    state["trailing_active"] = False
                    state["breakeven_notified"] = False
                    state["trade_direction"] = trade_side
                    save_state(state)
                    
                    bot.send_message(
                        ALLOWED_USER_ID,
                        f"""🚀 <b>Открыта сделка ЛОНГ 🟢 (BTCUSDT)!</b>
Причина входа: <i>{entry_reason}</i>
Вход по цене: <b>{current_price:.2f}</b>
🛒 Рабочий объем: <b>{qty} BTC</b> (RSI: {entry_rsi:.1f})""",
                        parse_mode="HTML"
                    )
                    continue
                    
                # --- ЕСЛИ ПОЗИЦИЯ ЕСТЬ ---
                if pos_size > 0:
                    pos_side = pos_data.get('side', state.get("trade_direction", "Buy"))
                    is_long = (pos_side == "Buy")
                    
                    # Динамический шаг (ATR)
                    if state.get("dynamic_step"):
                        klines = get_klines("BTCUSDT", "15", 30)
                        atr = calculate_atr(klines)
                        if atr > 0:
                            step = max(step, atr * 1.5)

                    sl_percent = float(state.get("sl_percent", 15.0))  # Стоп-лосс в % от бюджета
                    max_loss_usdt = budget * (sl_percent / 100.0)

                    # 1. Жесткий Stop-Loss
                    if unrealised_pnl <= -max_loss_usdt:
                        close_side = "Sell" if is_long else "Buy"
                        session.place_order(category="linear", symbol="BTCUSDT", side=close_side, orderType="Market", qty=str(pos_size), reduceOnly=True)
                        log_trade("Stop-Loss", pos_size, current_price, unrealised_pnl)
                        state["auto_trade"] = False
                        state["dca_step"] = 0
                        save_state(state)
                        bot.send_message(ALLOWED_USER_ID, f"🚨 <b>STOP-LOSS (BTCUSDT)!</b>\nПозиция закрыта: {pos_size} BTC по {current_price:.2f}\nУбыток {abs(unrealised_pnl):.2f}. Бот остановлен во избежание слива.", parse_mode="HTML")
                        continue

                    # 2. Умный сброс микро-минуса (Smart Early Cut)
                    # Режет позицию ТОЛЬКО при реальном сломе структуры на 15m свечах, а не на 5m шуме
                    early_cut_threshold = float(state.get("early_cut_loss", 3.0))
                    if is_long and (unrealised_pnl <= -early_cut_threshold) and (unrealised_pnl > -max_loss_usdt):
                        klines_dump = get_klines("BTCUSDT", "15", 20)
                        is_dumping, dump_reason = check_bearish_breakdown_for_cut(klines_dump)
                        if is_dumping:
                            close_side = "Sell"
                            session.place_order(category="linear", symbol="BTCUSDT", side=close_side, orderType="Market", qty=str(pos_size), reduceOnly=True)
                            log_trade("Smart Early Cut", pos_size, current_price, unrealised_pnl)
                            
                            state["dump_pause_until"] = time_module.time() + 300 # 5 минут пауза
                            state["base_price"] = current_price
                            state["dca_step"] = 0
                            state["highest_price"] = 0
                            state["lowest_price"] = 0
                            state["trailing_active"] = False
                            save_state(state)
                            
                            bot.send_message(
                                ALLOWED_USER_ID,
                                f"""✂️ <b>Умный сброс (Smart Cut)!</b>
Зафиксирован убыток: <b>{unrealised_pnl:.2f} USDT</b> во избежание просадки.
Причина: <i>{dump_reason}</i>

⏸ Пауза 5 минут. Ищем разворот вверх!""",
                                parse_mode="HTML"
                            )
                            continue

                    # 4. Динамический Take-Profit (Трейлинг)
                    # Если было усреднение (dca_step > 1), цель приближаем к рынку для быстрого гарантированного плюса
                    current_dca_step = state.get("dca_step", 1)
                    if current_dca_step > 1:
                        tp_step = max(50.0, step * 0.25) # Быстрый выход из усреднения
                    else:
                        tp_step = max(70.0, step * 0.45) # Стандартная фиксация прибыли

                    tp_condition_met = (current_price >= avg_price + tp_step) if is_long else (current_price <= avg_price - tp_step)
                    
                    if tp_condition_met and unrealised_pnl > 0:
                        if not state.get("trailing_active"):
                            state["trailing_active"] = True
                            state["highest_price"] = current_price
                            save_state(state)
                            bot.send_message(ALLOWED_USER_ID, f"🚀 <b>Цена вышла в хороший плюс! (+{unrealised_pnl:.2f}$)</b>\nАктивирован Трейлинг-Стоп. Тянем прибыль...", parse_mode="HTML")
                        
                        highest = state.get("highest_price", current_price)
                        # Откат трейлинга: если цена откатывает на 25% от пика прибыли или на 70$
                        profit_dist = max(10.0, highest - avg_price)
                        trailing_pullback = min(state.get("trailing_drop", 80.0), profit_dist * 0.3)
                        
                        if is_long and (current_price <= highest - trailing_pullback):
                            close_side = "Sell"
                            session.place_order(category="linear", symbol="BTCUSDT", side=close_side, orderType="Market", qty=str(pos_size), reduceOnly=True)
                            log_trade("Take-Profit (Smart Trailing)", pos_size, current_price, unrealised_pnl)
                            bot.send_message(
                                ALLOWED_USER_ID,
                                f"""🎯 <b>Сделка закрыта в ПЛЮС!</b>
Цена выхода: <b>{current_price:.2f}</b>
Прибыль: <b>+{unrealised_pnl:.2f} USDT</b> 💸

🔄 Анализирую рынок для следующего входа...""",
                                parse_mode="HTML"
                            )
                            
                            state["base_price"] = current_price
                            state["dca_step"] = 0
                            state["highest_price"] = 0
                            state["lowest_price"] = 0
                            state["trailing_active"] = False
                            state.pop("budget_exceeded_notified", None)
                            state.pop("margin_error_notified", None)
                            save_state(state)
                            continue

                    if state.get("trailing_active") and is_long:
                        if current_price > state.get("highest_price", current_price):
                            state["highest_price"] = current_price
                            last_notified = state.get("highest_price_notified", avg_price)
                            if current_price > last_notified + 25:
                                bot.send_message(ALLOWED_USER_ID, f"🔥 <b>Трейлинг растет!</b>\nПик: <b>{current_price:.2f}</b> (PnL: +{unrealised_pnl:.2f}$)", parse_mode="HTML")
                                state["highest_price_notified"] = current_price
                            save_state(state)

                    # 5. Умное усреднение (DCA)
                    dynamic_dca_step_size = step * (1 + (current_dca_step - 1) * 0.4)
                    target_dca_price = (state.get("base_price", avg_price) - dynamic_dca_step_size) if is_long else (state.get("base_price", avg_price) + dynamic_dca_step_size)
                    
                    dca_condition_met = (current_price <= target_dca_price) if is_long else (current_price >= target_dca_price)
                    
                    if dca_condition_met:
                        klines_dca = get_klines("BTCUSDT", "5", 20)
                        rsi_dca = calculate_rsi(klines_dca)
                        
                        # Проверяем, не летит ли сейчас безоткатный нож (свеча падения > 300$)
                        is_knife = False
                        if len(klines_dca) >= 2:
                            c_open = float(klines_dca[-1][1])
                            c_close = float(klines_dca[-1][4])
                            if c_close < c_open and (c_open - c_close) > 300:
                                is_knife = True
                        
                        if is_knife and rsi_dca > 42:
                            import time
                            if time.time() - state.get("last_dca_wait_msg", 0) > 180:
                                bot.send_message(ALLOWED_USER_ID, f"⏳ <b>Цена дошла до уровня DCA ({current_price:.1f})</b>\nЖду завершения импульсной минутной свечи...", parse_mode="HTML")
                                state["last_dca_wait_msg"] = time.time()
                                save_state(state)
                            continue
                        
                        state.pop("waiting_dca_notified", None)
                        
                        if position_im >= budget:
                            if not state.get("budget_exceeded_notified"):
                                bot.send_message(ALLOWED_USER_ID, f"⚠️ Достигнут лимит маржи ({budget} USDT)! Усреднение остановлено. Ждем профита.")
                                state["budget_exceeded_notified"] = True
                                state["base_price"] = current_price
                                save_state(state)
                        else:
                            dca_step = state.get("dca_step", 1)
                            if dca_step <= state.get("max_dca", 10):
                                base_qty = float(state.get("qty", 0.001))
                                multiplier = 1.0 + (dca_step * 0.2) if dca_step < 5 else 2.0
                                current_dca_qty = round(base_qty * multiplier, 3)
                                if current_dca_qty < 0.001: current_dca_qty = 0.001
                                buy_qty = str(round(current_dca_qty, 3))
                                
                                trade_side = "Buy" if is_long else "Sell"
                                session.place_order(category="linear", symbol="BTCUSDT", side=trade_side, orderType="Market", qty=buy_qty)
                                log_trade("Instant DCA", float(buy_qty), current_price, 0.0)
                                state["base_price"] = current_price
                                state["dca_step"] = dca_step + 1
                                state["budget_exceeded_notified"] = False
                                state.pop("highest_price", None)
                                state.pop("lowest_price", None)
                                state.pop("breakeven_notified", None)
                                save_state(state)
                                bot.send_message(ALLOWED_USER_ID, f"⚡ <b>Усреднение выполнено!</b>\nДобавлен объем: <b>{buy_qty} BTC</b> по цене <b>{current_price:.2f}</b>\nНовая средняя цена снижена, цель выхода приближена!", parse_mode="HTML")
                            else:
                                if not state.get("max_dca_notified"):
                                    bot.send_message(ALLOWED_USER_ID, f"⚠️ Максимальное количество шагов усреднения достигнуто. Ждем профита.")
                                    state["max_dca_notified"] = True
                                    save_state(state)
            elif state.get("auto_paused"):
                # Авто-возобновление после ИИ-паузы или дампа
                klines = get_klines("BTCUSDT", "15", 50)
                rsi = calculate_rsi(klines)
                closes = [float(k[4]) for k in klines]
                ema_50 = calculate_ema(closes, 50) or closes[-1]
                
                # Если рынок бычий (выше EMA50) или RSI остыл ниже 60 - возобновляем!
                if (current_price >= ema_50 and rsi < 70) or (rsi < 55):
                    state["auto_trade"] = True
                    state["auto_paused"] = False
                    state["base_price"] = current_price
                    save_state(state)
                    bot.send_message(ALLOWED_USER_ID, f"🟢 <b>Рынок стабилизировался (RSI: {rsi:.1f})!</b>\nАвтоторговля BTCUSDT ВОЗОБНОВЛЕНА.", parse_mode="HTML")
        except Exception as e:
            err_str = str(e)
            print(f"Ошибка в мониторинге: {err_str}")
            
            if "110007" in err_str or "ab not enough" in err_str:
                if not state.get("margin_error_notified"):
                    try:
                        bot.send_message(ALLOWED_USER_ID, f"⚠️ <b>Недостаточно маржи (USDT)!</b>\nПопытка усреднить позицию отклонена биржей (ErrCode: 110007). На балансе Единого торгового аккаунта не хватает свободных средств.\n\nУменьшите объем (/set_qty) или ждите закрытия текущей позиции.", parse_mode="HTML")
                        state["margin_error_notified"] = True
                        save_state(state)
                    except:
                        pass
                if "current_price" in locals():
                    state["base_price"] = current_price
                import time
                time.sleep(10)
                continue
            
            if not state.get("error_notified"):
                try:
                    bot.send_message(ALLOWED_USER_ID, f"❌ <b>Ошибка в торговом цикле (BTCUSDT):</b>\n<code>{html.escape(err_str)}</code>", parse_mode="HTML")
                    state["error_notified"] = True
                    save_state(state)
                except:
                    pass
            import time
            time.sleep(10)
            continue
        
        # Если цикл прошел успешно
        if state.get("error_notified"):
            state["error_notified"] = False
            save_state(state)
            
        import time
        time.sleep(10)

def setup_commands():
    commands = [
        telebot.types.BotCommand("start", "ℹ️ Меню и команды"),
        telebot.types.BotCommand("stats", "📊 Полная статистика и аналитика"),
        telebot.types.BotCommand("export_csv", "📥 Выгрузить историю сделок (CSV)"),
        telebot.types.BotCommand("last_trades", "🧾 Последние закрытые сделки"),
        telebot.types.BotCommand("profit", "💸 Экспресс-профит за 50 сделок"),
        telebot.types.BotCommand("force_buy", "⚡ Немедленно войти в ЛОНГ"),
        telebot.types.BotCommand("start_auto", "▶️ Запуск автоторговли"),
        telebot.types.BotCommand("stop_auto", "⏸ Стоп автоторговли"),
        telebot.types.BotCommand("status", "📊 Полный статус бота"),
        telebot.types.BotCommand("balance", "💰 Баланс аккаунта"),
        telebot.types.BotCommand("price", "📈 Текущая цена BTC"),
        telebot.types.BotCommand("check_ai", "🧠 Проверить ИИ"),
        telebot.types.BotCommand("dca", "🚑 Принудительное усреднение"),
        telebot.types.BotCommand("set_aggression", "🔥 Активность (high/normal)"),
        telebot.types.BotCommand("set_early_cut", "✂️ Порог сброса минуса"),
        telebot.types.BotCommand("set_dynamic_step", "⚙️ Умный шаг сетки (ATR)"),
        telebot.types.BotCommand("set_trailing", "⚙️ Откат трейлинга"),
        telebot.types.BotCommand("set_step", "⚙️ Шаг сетки"),
        telebot.types.BotCommand("set_qty", "⚙️ Объем ордера"),
        telebot.types.BotCommand("set_sl", "⚙️ Изменить Stop-Loss"),
        telebot.types.BotCommand("set_budget", "⚙️ Изменить бюджет"),
        telebot.types.BotCommand("close_all", "🛑 Закрыть все позиции")
    ]
    bot.set_my_commands(commands)

if __name__ == "__main__":
    setup_commands()
    if os.getenv("TG_API_ID") and os.getenv("TG_SESSION"):
        print("Запуск Userbot...")
        subprocess.Popen([sys.executable, "userbot.py"])
    t = threading.Thread(target=monitor_price, daemon=True)
    t.start()
    print("Бот (Уровень 3 - Только BTC) запущен!")
    bot.polling(none_stop=True)
