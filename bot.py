import os
import sys
import html
import time
import json
import subprocess
import threading
state_lock = threading.Lock()
import telebot
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
    "early_cut_loss": 1.0,
    "dump_pause_until": 0,
        "budget": 1000.0,
        "dynamic_step": False,
        "trailing_drop": 100.0,
        "trailing_active": False,
        "trailing_high": 0.0
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
        with open("trades.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {action} | QTY: {qty} | Price: {price} | PnL: {pnl}\n")
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

def check_bearish_breakdown_for_cut(klines_5m):
    """
    Умный анализ пробоя вниз:
    Проверяет, не начался ли уверенный дамп, чтобы вовремя срезать микро-минус
    """
    if not klines_5m or len(klines_5m) < 6:
        return False, "Недостаточно данных"
    try:
        closes = [float(k[4]) for k in klines_5m]
        opens = [float(k[1]) for k in klines_5m]
        lows = [float(k[3]) for k in klines_5m]
        volumes = [float(k[5]) for k in klines_5m]

        curr_red = closes[-1] < opens[-1]
        c1_red = closes[-2] < opens[-2]
        c2_red = closes[-3] < opens[-3]

        local_min_past = min(lows[-6:-2])
        is_breakdown = closes[-1] < local_min_past

        avg_vol = sum(volumes[-10:-2]) / 8 if len(volumes) >= 10 else volumes[-1]
        high_sell_vol = volumes[-1] > (avg_vol * 1.2) or volumes[-2] > (avg_vol * 1.2)

        # Сигнал слива: пробой локального лоя на красных свечах ИЛИ 3 красные свечи подряд с повышенным объемом
        if curr_red and c1_red and is_breakdown:
            return True, f"Пробой поддержки вниз ({closes[-1]:.1f}) на 2+ красных свечах"
        if curr_red and c1_red and c2_red and high_sell_vol:
            return True, f"Давление медведей: 3 красные свечи подряд с ростом объема"
        return False, ""
    except Exception as e:
        return False, str(e)

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
        "/start_auto - Запустить автоторговлю\n"
        "/stop_auto - Остановить автоторговлю\n"
        "/status - Статус бота\n"
        "/set_dynamic_step [1/0] - Вкл/Выкл умный шаг (ATR)\n"
        "/set_trailing [DROP] - Настроить откат трейлинга\n"
        "/check_ai - Запросить ИИ-анализ рынка\n\n"
        "<i>Стандартные настройки:</i>\n"
        "/profit, /balance, /price\n"
        "/set_leverage, /set_budget, /set_step, /set_qty, /set_sl, /close_all", parse_mode="HTML"
    )

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
        f"Автоторговля: {'✅ ВКЛ' if state['auto_trade'] else '⏸ ВЫКЛ'}\n"
        f"Базовая цена: {state['base_price']}\n"
        f"Динамический шаг (ATR): {'✅ ВКЛ' if state.get('dynamic_step') else '⏸ ВЫКЛ'}\n"
        f"Шаг (статика): {state['step']}\n"
        f"Объем: {state['qty']}\n"
        f"Бюджет: {state['budget']}\n"
        f"Stop-Loss: {state['sl_percent']}%\n"
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
        state = load_state()
        state["early_cut_loss"] = val
        save_state(state)
        bot.reply_to(message, f"✅ Порог умного сброса микро-минуса установлен на -{val:.2f} USDT.")
    except (IndexError, ValueError):
        bot.reply_to(message, "Использование: /set_early_cut 1.0 (закрывать при минусе от 1.0$ если свечи сливают)")

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

@bot.message_handler(commands=['profit'])
def profit_cmd(message):
    if not check_auth(message): return
    try:
        resp = session.get_closed_pnl(category="linear", limit=50)
        total_pnl = sum(safe_float(x.get('closedPnl', 0)) for x in resp['result']['list'])
        bot.reply_to(message, f"💸 <b>Прибыль (последние 50 сделок):</b>\n{total_pnl:.2f} USDT", parse_mode="HTML")
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
                        f"📝 <b>Регулярный отчет (каждые 10 мин)</b>\n\n"
                        f"🤖 Статус: Автоторговля {status_str}\n"
                        f"📈 Текущая цена BTC: {rep_price}\n"
                        f"🎯 Цена отсчета (база): {state.get('base_price', rep_price)}\n"
                        f"📊 Открытая позиция: {r_pos_size} BTC\n"
                        f"💧 Нереализованный PnL: {r_unrealised:.2f} USDT\n"
                        f"💸 Прибыль (за 50 сделок): {total_pnl:.2f} USDT"
                    )
                    bot.send_message(ALLOWED_USER_ID, report_text, parse_mode="HTML")
                except Exception as e:
                    print(f"Report error: {e}")
                    
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
                    # Умный вход: определяем тренд по EMA и RSI
                    klines = get_klines("BTCUSDT", "15", 200)
                    rsi = calculate_rsi(klines)
                    closes = [float(k[4]) for k in klines]
                    ema_100 = calculate_ema(closes, 100)
                    
                    is_uptrend = current_price > ema_100
                    
                    # Логика входа (ТОЛЬКО LONG)
                    if rsi >= 45: # Ждем пока RSI не опустится (просадка) для выгодной покупки
                        import time
                        if time.time() - state.get("last_entry_wait_msg", 0) > 300: # Каждые 5 минут напоминаем
                            bot.send_message(ALLOWED_USER_ID, f"⏳ <b>Слежу за рынком (LONG)</b>\nЖду когда RSI упадет ниже 45. Сейчас RSI: {rsi:.1f}.\n(Текущая цена: {current_price})", parse_mode="HTML")
                            state["last_entry_wait_msg"] = time.time()
                            save_state(state)
                        continue
                    else:
                        trade_side = "Buy"
                        
                    state.pop("waiting_entry_notified", None)
                    qty = str(float(state.get("qty", 0.001)))
                    session.place_order(category="linear", symbol="BTCUSDT", side=trade_side, orderType="Market", qty=qty)
                    log_trade(f"First {trade_side}", float(qty), current_price, 0.0)
                    state["base_price"] = current_price
                    state["dca_step"] = 1
                    state["highest_price"] = current_price
                    state["lowest_price"] = current_price
                    state["breakeven_notified"] = False
                    state["trade_direction"] = trade_side
                    save_state(state)
                    
                    dir_str = "ЛОНГ (Вверх) 🟢" if trade_side == "Buy" else "ШОРТ (Вниз) 🔴"
                    bot.send_message(ALLOWED_USER_ID, f"🚀 <b>Открыта новая сделка (BTCUSDT)!</b>\nНаправление: {dir_str}\nЗашел в рынок по цене: {current_price:.2f}\n🛒 Объем: {qty} BTC", parse_mode="HTML")
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

                    sl_percent = state.get("sl_percent", 5.0)  # Жесткий стоп-лосс 5% по умолчанию
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
                    # Если позиция ушла в небольшой минус (-0.5$ ... -2.5$) и свечи показывают реальный слив
                    early_cut_threshold = float(state.get("early_cut_loss", 1.0))
                    if is_long and (unrealised_pnl <= -early_cut_threshold) and (unrealised_pnl > -max_loss_usdt):
                        klines_dump = get_klines("BTCUSDT", "5", 15)
                        is_dumping, dump_reason = check_bearish_breakdown_for_cut(klines_dump)
                        if is_dumping:
                            close_side = "Sell"
                            session.place_order(category="linear", symbol="BTCUSDT", side=close_side, orderType="Market", qty=str(pos_size), reduceOnly=True)
                            log_trade("Smart Early Cut", pos_size, current_price, unrealised_pnl)
                            
                            state["dump_pause_until"] = time_module.time() + 600 # 10 минут пауза
                            state["base_price"] = current_price
                            state["dca_step"] = 0
                            state["highest_price"] = 0
                            state["lowest_price"] = 0
                            state["trailing_active"] = False
                            save_state(state)
                            
                            bot.send_message(
                                ALLOWED_USER_ID,
                                f"✂️ <b>Умный сброс микро-минуса (Smart Cut)!</b>\n"
                                f"Зафиксировал небольшой минус: <b>{unrealised_pnl:.2f} USDT</b> во избежание глубокой просадки.\n"
                                f"Причина: <i>{dump_reason}</i>\n\n"
                                f"⏸ Включена пауза 10 минут. Подождем спокойное дно и отыграем в плюс!",
                                parse_mode="HTML"
                            )
                            continue

                    # 3.5 Смена тренда (Переворот позиции)
                    klines_trend = get_klines("BTCUSDT", "15", 200)
                    closes_trend = [float(k[4]) for k in klines_trend]
                    ema_100 = calculate_ema(closes_trend, 100)
                    # Закрытие сделок и смена тренда выключены (режим ТОЛЬКО ЛОНГ)

                    # 4. Динамический Take-Profit (Трейлинг)
                    # Вместо жесткого закрытия, когда цена доходит до TP, мы просто даем ей расти.
                    # Но если профит есть, и цена пошла назад - закрываем
                    tp_step = max(step * 0.8, 50) # Снизили порог активации
                    tp_condition_met = (current_price >= avg_price + tp_step) if is_long else (current_price <= avg_price - tp_step)
                    
                    if tp_condition_met and unrealised_pnl > 0:
                        # Включаем трейлинг, если он еще не включен
                        if not state.get("trailing_active"):
                            state["trailing_active"] = True
                            state["highest_price"] = current_price
                            save_state(state)
                            bot.send_message(ALLOWED_USER_ID, f"🚀 <b>Цена вышла в хороший плюс!</b>\nАктивирован Трейлинг-Стоп. Тянем профит...", parse_mode="HTML")
                        
                        # Если цена упала на 20% от пройденного роста (откатывается)
                        highest = state.get("highest_price", current_price)
                        if is_long and current_price < highest - (highest - avg_price) * 0.25:
                            close_side = "Sell"
                            session.place_order(category="linear", symbol="BTCUSDT", side=close_side, orderType="Market", qty=str(pos_size), reduceOnly=True)
                            log_trade("Take-Profit (Smart Trailing)", pos_size, current_price, unrealised_pnl)
                            bot.send_message(ALLOWED_USER_ID, f"🎯 <b>Сделка закрыта по Smart Трейлингу!</b>\nПозиция закрыта по {current_price:.2f}\nПрибыль: ~{unrealised_pnl:.2f} USDT\n\n🔄 Ждем новую сделку...", parse_mode="HTML")
                            
                            state["base_price"] = current_price
                            state["dca_step"] = 0
                            state["highest_price"] = 0
                            state["lowest_price"] = 0
                            state["trailing_active"] = False
                            state.pop("budget_exceeded_notified", None)
                            state.pop("margin_error_notified", None)
                            save_state(state)
                            continue
                            
                    # Если трейлинг не сработал на откате, просто обновляем хаи
                    if state.get("trailing_active") and is_long:
                        if current_price > state.get("highest_price", current_price):
                            state["highest_price"] = current_price
                            # Спамим об обновлении максимума каждые 20$ профита
                            last_notified = state.get("highest_price_notified", avg_price)
                            if current_price > last_notified + 20:
                                bot.send_message(ALLOWED_USER_ID, f"🔥 <b>Трейлинг растет!</b>\nНовый максимум: {current_price:.2f} (Защищенный профит увеличился)", parse_mode="HTML")
                                state["highest_price_notified"] = current_price
                            save_state(state)
                    # --- конец нового блока TP ---
                    if False: # Отключаем старый кусок
                        close_side = "Sell" if is_long else "Buy"
                        session.place_order(category="linear", symbol="BTCUSDT", side=close_side, orderType="Market", qty=str(pos_size), reduceOnly=True)
                        log_trade("Take-Profit (100%)", pos_size, current_price, unrealised_pnl)
                        bot.send_message(ALLOWED_USER_ID, f"🎯 <b>Сделка закрыта в ПЛЮС по Тейк-профиту!</b>\nПозиция закрыта по {current_price:.2f}\nПрибыль: ~{unrealised_pnl:.2f} USDT\n\n🔄 Ждем новую сделку...", parse_mode="HTML")
                        
                        state["base_price"] = current_price
                        state["dca_step"] = 0
                        state["highest_price"] = 0
                        state["lowest_price"] = 0
                        state.pop("budget_exceeded_notified", None)
                        state.pop("margin_error_notified", None)
                        save_state(state)
                        continue

                    # 5. Мгновенное усреднение (Smart DCA) при минусе
                    # Увеличиваем шаг с каждым усреднением (чтобы не закупаться слишком часто на сильном падении)
                    current_dca_step = state.get("dca_step", 1)
                    dynamic_dca_step_size = step * (1 + (current_dca_step - 1) * 0.5) # 1x, 1.5x, 2x, 2.5x...
                    target_dca_price = (state.get("base_price", avg_price) - dynamic_dca_step_size) if is_long else (state.get("base_price", avg_price) + dynamic_dca_step_size)
                    
                    dca_condition_met = (current_price <= target_dca_price) if is_long else (current_price >= target_dca_price)
                    
                    if dca_condition_met:
                        klines_dca = get_klines("BTCUSDT", "5", 20)
                        rsi_dca = calculate_rsi(klines_dca)
                        
                        # Строгий индикатор для откупа (RSI < 30 для лонга)
                        if (is_long and rsi_dca >= 30) or (not is_long and rsi_dca <= 70):
                            import time
                            if time.time() - state.get("last_dca_wait_msg", 0) > 180: # Каждые 3 минуты
                                bot.send_message(ALLOWED_USER_ID, f"⏳ <b>Готов усреднять, но жду дно!</b>\nЦена ({current_price}) дошла до уровня покупки, но RSI еще высокий ({rsi_dca:.1f}). Ждем паники (RSI < 30)...", parse_mode="HTML")
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
                                bot.send_message(ALLOWED_USER_ID, f"⚡ <b>Мгновенное усреднение!</b>\nЦена достигла {current_price:.2f}.\n🛒 Добавлен объем {buy_qty}.", parse_mode="HTML")
                            else:
                                if not state.get("max_dca_notified"):
                                    bot.send_message(ALLOWED_USER_ID, f"⚠️ Максимальное количество шагов усреднения достигнуто. Ждем профита.")
                                    state["max_dca_notified"] = True
                                    save_state(state)

            elif state.get("auto_paused"):
                # Авто-возобновление после ИИ-паузы
                klines = get_klines("BTCUSDT", "15", 50)
                rsi = calculate_rsi(klines)
                resume_rsi = 55
                if rsi < resume_rsi:
                    response = session.get_tickers(category="linear", symbol="BTCUSDT")
                    current_price = float(response['result']['list'][0]['lastPrice'])
                    state["auto_trade"] = True
                    state["auto_paused"] = False
                    state["base_price"] = current_price
                    save_state(state)
                    bot.send_message(ALLOWED_USER_ID, f"🟢 <b>Рынок готов (RSI: {rsi:.1f})!</b>\nАвтоторговля BTCUSDT ВОЗОБНОВЛЕНА.", parse_mode="HTML")

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
        telebot.types.BotCommand("start", "ℹ️ Информация"),
        telebot.types.BotCommand("status", "📊 Статус бота"),
        telebot.types.BotCommand("start_auto", "▶️ Запуск автоторговли"),
        telebot.types.BotCommand("stop_auto", "⏸ Стоп автоторговли"),
        telebot.types.BotCommand("set_mode", "🚀 Выбрать режим (scalp/standard)"),
        telebot.types.BotCommand("set_dynamic_step", "⚙️ Умный шаг сетки"),
        telebot.types.BotCommand("set_trailing", "⚙️ Откат трейлинга"),
        telebot.types.BotCommand("check_ai", "🧠 Проверить ИИ"),
        telebot.types.BotCommand("check_keys", "🔑 Проверить API ключи"),
        telebot.types.BotCommand("balance", "💰 Баланс"),
        telebot.types.BotCommand("profit", "💸 Статистика PnL"),
        telebot.types.BotCommand("price", "📈 Цена BTC"),
        telebot.types.BotCommand("dca", "🚑 Принудительное усреднение"),
        telebot.types.BotCommand("set_step", "⚙️ Изменить шаг сетки"),
        telebot.types.BotCommand("set_qty", "⚙️ Изменить объем"),
        telebot.types.BotCommand("set_sl", "⚙️ Изменить Stop-Loss"),
        telebot.types.BotCommand("set_budget", "⚙️ Изменить бюджет"),
        telebot.types.BotCommand("set_leverage", "⚙️ Изменить плечо"),
        telebot.types.BotCommand("close_all", "🛑 Закрыть позиции")
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
