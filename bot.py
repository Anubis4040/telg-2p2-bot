import collections
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
DB_DIR = os.environ.get("DB_DIR", "/app/data")
DB_FILE = os.path.join(DB_DIR, "prices.db")
API_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
}

MAX_COMMANDS_PER_MINUTE = 10
MAX_NOTIFICATIONS_PER_CYCLE = 5
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 1
HEALTH_PORT = int(os.environ.get("PORT", 8080))


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            adv_no TEXT,
            price REAL,
            fiat TEXT,
            crypto TEXT,
            advertiser TEXT,
            event_type TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_FILE)


def store_price(adv_no, price, fiat, crypto, advertiser, event_type):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO price_history (adv_no, price, fiat, crypto, advertiser, event_type) VALUES (?, ?, ?, ?, ?, ?)",
            (adv_no, price, fiat, crypto, advertiser, event_type),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB insert error: %s", e)


def get_recent_history(limit=20):
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute(
            "SELECT timestamp, adv_no, price, fiat, crypto, advertiser, event_type FROM price_history ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error("DB query error: %s", e)
        return []


def get_price_stats():
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT MIN(price), MAX(price), AVG(price), COUNT(*) FROM price_history").fetchone()
        conn.close()
        return row
    except Exception as e:
        logger.error("DB stats error: %s", e)
        return None


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    server.serve_forever()


def load_config():
    env = os.environ.get
    config = {
        "telegram_bot_token": env("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": env("TELEGRAM_CHAT_ID"),
        "crypto": env("CRYPTO", "USDT"),
        "fiat": env("FIAT", "USD"),
        "max_price": float(env("MAX_PRICE", 1.09)),
        "payment_methods": env("PAYMENT_METHODS", "").split(",") if env("PAYMENT_METHODS", "") else [],
        "poll_interval": int(env("POLL_INTERVAL", 60)),
    }
    if not config["telegram_bot_token"] or not config["telegram_chat_id"]:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            file_config = json.load(f)
        for key, value in file_config.items():
            if key not in config or config[key] in (None, "", []):
                config[key] = value
    return config


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def check_rate_limit(user_id, command_times):
    now = time.time()
    cutoff = now - 60
    command_times[user_id] = [t for t in command_times[user_id] if t > cutoff]
    if len(command_times[user_id]) >= MAX_COMMANDS_PER_MINUTE:
        return False
    command_times[user_id].append(now)
    return True


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning("Telegram rate limit, esperando %ds", retry_after)
                time.sleep(retry_after)
                continue
            return
        except requests.RequestException as e:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.error("Error Telegram (intento %d/%d): %s, retry en %ds", attempt + 1, RETRY_MAX_ATTEMPTS, e, delay)
            time.sleep(delay)


def get_updates(token, offset):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
        return resp.json().get("result", [])
    except requests.RequestException as e:
        logger.error("Error al obtener actualizaciones: %s", e)
        return []


def fetch_p2p_orders(config):
    payload = {
        "fiat": config["fiat"],
        "page": 1,
        "rows": 20,
        "tradeType": "BUY",
        "asset": config["crypto"],
        "payTypes": config.get("payment_methods", []),
    }
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            resp = requests.post(API_URL, json=payload, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.json().get("data", [])
        except requests.RequestException as e:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.error("Error API P2P (intento %d/%d): %s, retry en %ds", attempt + 1, RETRY_MAX_ATTEMPTS, e, delay)
            time.sleep(delay)
    return []


def get_valid_orders(orders, max_price):
    valid = []
    for order in orders:
        adv = order.get("adv", {})
        if adv.get("isTradable") is not True:
            continue
        try:
            price = float(adv.get("price", 0))
        except (ValueError, TypeError):
            continue
        if price <= max_price:
            valid.append(order)
    return valid


def find_best_price(valid_orders):
    best_order = None
    best_price = float("inf")
    for order in valid_orders:
        try:
            price = float(order.get("adv", {}).get("price", 0))
        except (ValueError, TypeError):
            continue
        if price < best_price:
            best_price = price
            best_order = order
    return best_order


def format_message(order):
    adv = order.get("adv", {})
    advertiser = order.get("advertiser", {})
    price = adv.get("price", "N/A")
    amount = adv.get("surplusAmount", "N/A")
    min_limit = adv.get("minSingleTransAmount", "N/A")
    max_limit = adv.get("maxSingleTransAmount", "N/A")
    pay_methods = ", ".join(pm.get("tradeMethodName", "") for pm in adv.get("tradeMethods", []))
    nickname = advertiser.get("nickName", "N/A")
    user_no = advertiser.get("userNo", "")
    link = f"https://p2p.binance.com/trade/buy/USDT?fiat=USD&advertiser={user_no}"
    bell = chr(0x1F514)
    flag = chr(0x1F3D7)
    return (
        f"{bell} {flag} Oportunidad de compra detectada!\n\n"
        f"Par: {adv.get('asset', 'USDT')}/{adv.get('fiatUnit', 'USD')}\n"
        f"Precio: ${price}\n"
        f"Cantidad disponible: {amount} USDT\n"
        f"Limite: ${min_limit} - ${max_limit}\n"
        f"Metodos de pago: {pay_methods}\n"
        f"Vendedor: {nickname}\n"
        f"Link: {link}"
    )


def format_best_price_message(order, max_price):
    adv = order.get("adv", {})
    advertiser = order.get("advertiser", {})
    price = float(adv.get("price", 0))
    amount = adv.get("surplusAmount", "N/A")
    min_limit = adv.get("minSingleTransAmount", "N/A")
    max_limit = adv.get("maxSingleTransAmount", "N/A")
    pay_methods = ", ".join(pm.get("tradeMethodName", "") for pm in adv.get("tradeMethods", []))
    nickname = advertiser.get("nickName", "N/A")
    user_no = advertiser.get("userNo", "")
    link = f"https://p2p.binance.com/trade/buy/USDT?fiat=USD&advertiser={user_no}"
    diff = price - max_price
    trophy = chr(0x1F4CA)
    return (
        f"{trophy} Mejor precio disponible ahora\n\n"
        f"Par: USDT/USD\n"
        f"Mejor precio: ${price:.4f}\n"
        f"Precio maximo: ${max_price:.4f}\n"
        f"Diferencia: {diff:.4f}\n"
        f"Cantidad disponible: {amount} USDT\n"
        f"Limite: ${min_limit} - ${max_limit}\n"
        f"Metodos de pago: {pay_methods}\n"
        f"Vendedor: {nickname}\n"
        f"Link: {link}"
    )


def handle_command(token, chat_id, text, bot_state):
    config = load_config()
    trophy = chr(0x1F4CA)
    coin = chr(0x1F4B0)
    chart = chr(0x1F4C8)
    robot = chr(0x1F916)
    active = chr(0x1F7E2)

    if text == "/start":
        bot_state["running"] = True
        send_telegram(token, chat_id, "Bot iniciado. Monitorizando ordenes de compra de USDT...")
        return True

    elif text == "/stop":
        bot_state["running"] = False
        send_telegram(token, chat_id, "Bot detenido.")
        return False

    elif text == "/status":
        seen_count = len(bot_state.get("seen", {}))
        best_price = bot_state.get("last_best_price")
        best_price_str = f"${best_price:.4f}" if best_price else "N/A"
        status_emoji = active if bot_state["running"] else chr(0x1F534)
        status_text = "Activo" if bot_state["running"] else "Inactivo"
        msg = (
            f"{trophy} Estado del bot\n\n"
            f"Estado: {status_emoji} {status_text}\n"
            f"Par: {config['crypto']}/{config['fiat']}\n"
            f"Metodo de pago: {', '.join(config['payment_methods']) or 'Todos'}\n"
            f"Precio tope: ${config['max_price']}\n"
            f"Intervalo: {config['poll_interval']}s\n"
            f"Ordenes rastreadas: {seen_count}\n\n"
            f"{coin} Mejor precio actual: {best_price_str}"
        )
        send_telegram(token, chat_id, msg)
        return None

    elif text == "/history":
        rows = get_recent_history(15)
        if not rows:
            send_telegram(token, chat_id, "No hay historial de precios todavia.")
            return None
        lines = [f"{chart} Ultimos precios:\n"]
        for ts, adv_no, price, fiat, crypto, adv, evt in rows:
            lines.append(f"{ts} | {price} | {evt} | {adv[:20] if adv else 'N/A'}")
        msg = "\n".join(lines)
        send_telegram(token, chat_id, msg[:4096])
        return None

    elif text == "/stats":
        stats = get_price_stats()
        if stats and stats[3] > 0:
            msg = (
                f"{trophy} Estadisticas de precios\n\n"
                f"Registros: {stats[3]}\n"
                f"Minimo: ${stats[0]:.4f}\n"
                f"Maximo: ${stats[1]:.4f}\n"
                f"Promedio: ${stats[2]:.4f}"
            )
        else:
            msg = "No hay datos de precios todavia."
        send_telegram(token, chat_id, msg)
        return None

    elif text == "/help":
        msg = (
            f"{robot} Comandos disponibles\n\n"
            f"/start - Inicia el monitoreo de ordenes\n"
            f"/stop - Detiene el monitoreo\n"
            f"/status - Muestra el estado actual del bot\n"
            f"/best - Muestra la orden con mejor precio ahora\n"
            f"/history - Muestra el historial de precios\n"
            f"/stats - Muestra estadisticas de precios\n"
            f"/setprice <valor> - Cambia el precio maximo (ej: /setprice 1.05)\n"
            f"/help - Muestra esta ayuda\n\n"
            f"{chart} Configuracion actual:\n"
            f"Par: {config['crypto']}/{config['fiat']}\n"
            f"Precio maximo: ${config['max_price']}\n"
            f"Metodo de pago: {', '.join(config['payment_methods']) or 'Todos'}\n"
            f"Intervalo: {config['poll_interval']}s"
        )
        send_telegram(token, chat_id, msg)
        return None

    elif text.startswith("/setprice "):
        try:
            new_price = float(text.split(" ")[1])
            config["max_price"] = new_price
            save_config(config)
            send_telegram(token, chat_id, f"Precio maximo actualizado a: ${new_price}")
        except (ValueError, IndexError):
            send_telegram(token, chat_id, "Uso: /setprice <valor>\nEjemplo: /setprice 1.05")
        return None

    return None


def telegram_listener(token, chat_id, bot_state, command_times):
    offset = 0
    cycle = chr(0x23F3)
    while True:
        updates = get_updates(token, offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "")
            msg_chat_id = str(msg.get("chat", {}).get("id", ""))

            if text and msg_chat_id == chat_id:
                if not check_rate_limit(msg_chat_id, command_times):
                    send_telegram(token, chat_id, f"{cycle} Limite de comandos alcanzado. Espera un minuto.")
                    continue
                handle_command(token, chat_id, text, bot_state)


def binance_monitor(token, chat_id, bot_state):
    arrow = chr(0x1F504)
    while True:
        if not bot_state["running"]:
            time.sleep(1)
            continue

        config = load_config()
        orders = fetch_p2p_orders(config)
        valid = get_valid_orders(orders, config["max_price"])
        logger.info("Consulta API: %d ordenes totales, %d validas (filtro <= $%s)", len(orders), len(valid), config["max_price"])

        notifications_sent = 0

        if bot_state["first_run"]:
            for order in valid:
                adv = order.get("adv", {})
                bot_state["seen"][adv.get("advNo")] = float(adv.get("price", 0))
            bot_state["first_run"] = False
            logger.info("Primera ejecucion: %d ordenes existentes registradas", len(valid))
        else:
            for order in valid:
                if notifications_sent >= MAX_NOTIFICATIONS_PER_CYCLE:
                    logger.warning("Limite de notificaciones por ciclo alcanzado (%d)", MAX_NOTIFICATIONS_PER_CYCLE)
                    break
                adv = order.get("adv", {})
                order_id = adv.get("advNo")
                try:
                    current_price = float(adv.get("price", 0))
                except (ValueError, TypeError):
                    continue

                advertiser_name = order.get("advertiser", {}).get("nickName", "N/A")

                if order_id not in bot_state["seen"]:
                    bot_state["seen"][order_id] = current_price
                    msg = format_message(order)
                    send_telegram(token, chat_id, msg)
                    notifications_sent += 1
                    store_price(order_id, current_price, config["fiat"], config["crypto"], advertiser_name, "new_order")
                    logger.info("Orden nueva notificada: %s ($%.4f)", order_id, current_price)
                elif current_price != bot_state["seen"][order_id]:
                    old_price = bot_state["seen"][order_id]
                    bot_state["seen"][order_id] = current_price
                    msg = f"{arrow} Cambio de precio detectado!\n\n" + format_message(order) + f"\n\nPrecio anterior: ${old_price:.4f}"
                    send_telegram(token, chat_id, msg)
                    notifications_sent += 1
                    store_price(order_id, current_price, config["fiat"], config["crypto"], advertiser_name, "price_change")
                    logger.info("Precio cambiado: %s $%.4f -> $%.4f", order_id, old_price, current_price)

                elif current_price == bot_state["seen"][order_id]:
                    store_price(order_id, current_price, config["fiat"], config["crypto"], advertiser_name, "check")

        best = find_best_price(valid)
        if best:
            best_price = float(best.get("adv", {}).get("price", 0))
            advertiser_name = best.get("advertiser", {}).get("nickName", "N/A")
            if bot_state["last_best_price"] is None or best_price != bot_state["last_best_price"]:
                if notifications_sent < MAX_NOTIFICATIONS_PER_CYCLE:
                    msg = format_best_price_message(best, config["max_price"])
                    send_telegram(token, chat_id, msg)
                    notifications_sent += 1
                store_price(None, best_price, config["fiat"], config["crypto"], advertiser_name, "best_price")
                bot_state["last_best_price"] = best_price
                logger.info("Mejor precio actualizado: %s", best_price)
            else:
                logger.info("Mejor precio sin cambios: %s", best_price)

        time.sleep(config["poll_interval"])


def handle_exit(signum, frame):
    logger.info("Signal %d received, shutting down...", signum)


def main():
    init_db()
    config = load_config()
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print("Bot de Binance P2P Iniciado")
    print(f"Token: {token[:10]}...")
    print(f"Chat ID: {chat_id}")
    print("Envia /start en Telegram para comenzar a monitorear.\n")

    bot_state = {"running": False, "seen": {}, "first_run": True, "last_best_price": None}
    command_times = collections.defaultdict(list)

    t1 = threading.Thread(target=telegram_listener, args=(token, chat_id, bot_state, command_times), daemon=True)
    t2 = threading.Thread(target=binance_monitor, args=(token, chat_id, bot_state), daemon=True)
    t3 = threading.Thread(target=start_health_server, daemon=True)

    t1.start()
    t2.start()
    t3.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down...")


if __name__ == "__main__":
    main()


# Last updated 09/09/2026 14:24:02
