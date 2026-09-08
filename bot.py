import json
import logging
import os
import time
import requests

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
API_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
}


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


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except requests.RequestException as e:
        logger.error("Error al enviar mensaje de Telegram: %s", e)


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
    try:
        resp = requests.post(API_URL, json=payload, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except requests.RequestException as e:
        logger.error("Error al consultar API P2P: %s", e)
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

    return (
        f"🔔 ¡Oportunidad de compra detectada!\n\n"
        f"Par: {adv.get('asset', 'USDT')}/{adv.get('fiatUnit', 'USD')}\n"
        f"Precio: ${price}\n"
        f"Cantidad disponible: {amount} USDT\n"
        f"Límite: ${min_limit} - ${max_limit}\n"
        f"Métodos de pago: {pay_methods}\n"
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

    return (
        f"📊 Mejor precio disponible ahora\n\n"
        f"Par: USDT/USD\n"
        f"Mejor precio: ${price:.4f}\n"
        f"Precio máximo: ${max_price:.4f}\n"
        f"Diferencia: {diff:.4f}\n"
        f"Cantidad disponible: {amount} USDT\n"
        f"Límite: ${min_limit} - ${max_limit}\n"
        f"Métodos de pago: {pay_methods}\n"
        f"Vendedor: {nickname}\n"
        f"Link: {link}"
    )


def handle_command(token, chat_id, text, bot_state):
    config = load_config()

    if text == "/start":
        bot_state["running"] = True
        send_telegram(token, chat_id, "Bot iniciado. Monitorizando órdenes de compra de USDT...")
        return True

    elif text == "/stop":
        bot_state["running"] = False
        send_telegram(token, chat_id, "Bot detenido.")
        return False

    elif text == "/status":
        seen_count = len(bot_state.get("seen", set()))
        msg = (
            f"📊 Estado del bot\n\n"
            f"Estado: {'🟢 Activo' if bot_state['running'] else '🔴 Inactivo'}\n"
            f"Par: {config['crypto']}/{config['fiat']}\n"
            f"Precio máximo: ${config['max_price']}\n"
            f"Intervalo: {config['poll_interval']}s\n"
            f"Órdenes notificadas: {seen_count}"
        )
        send_telegram(token, chat_id, msg)
        return None

    elif text == "/help":
        msg = (
            f"🤖 Comandos disponibles\n\n"
            f"/start - Inicia el monitoreo de órdenes\n"
            f"/stop - Detiene el monitoreo\n"
            f"/status - Muestra el estado actual del bot\n"
            f"/setprice <valor> - Cambia el precio máximo (ej: /setprice 1.05)\n"
            f"/best - Muestra la orden con mejor precio ahora\n"
            f"/help - Muestra esta ayuda\n\n"
            f"📌 Configuración actual:\n"
            f"Par: {config['crypto']}/{config['fiat']}\n"
            f"Precio máximo: ${config['max_price']}\n"
            f"Método de pago: {', '.join(config['payment_methods']) or 'Todos'}\n"
            f"Intervalo: {config['poll_interval']}s"
        )
        send_telegram(token, chat_id, msg)
        return None

    elif text == "/best":
        orders = fetch_p2p_orders(config)
        valid = get_valid_orders(orders, config["max_price"])
        best = find_best_price(valid)
        if best:
            msg = format_best_price_message(best, config["max_price"])
            send_telegram(token, chat_id, msg)
        else:
            send_telegram(token, chat_id, "No se encontraron órdenes con tu filtro actual.")
        return None

    elif text.startswith("/setprice "):
        try:
            new_price = float(text.split(" ")[1])
            config["max_price"] = new_price
            save_config(config)
            send_telegram(token, chat_id, f"Precio máximo actualizado a: ${new_price}")
        except (ValueError, IndexError):
            send_telegram(token, chat_id, "Uso: /setprice <valor>\nEjemplo: /setprice 1.05")
        return None

    return None


def main():
    config = load_config()
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]

    print("Bot de Binance P2P Iniciado")
    print(f"Token: {token[:10]}...")
    print(f"Chat ID: {chat_id}")
    print("Envía /start en Telegram para comenzar a monitorear.\n")

    bot_state = {"running": False, "seen": {}, "first_run": True, "last_best_price": None}
    offset = 0

    while True:
        updates = get_updates(token, offset)

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "")
            msg_chat_id = str(msg.get("chat", {}).get("id", ""))

            if text and msg_chat_id == chat_id:
                result = handle_command(token, chat_id, text, bot_state)
                if result is True:
                    break
                elif result is False:
                    break

        if bot_state["running"]:
            config = load_config()
            orders = fetch_p2p_orders(config)
            valid = get_valid_orders(orders, config["max_price"])
            logger.info("Consulta API: %d ordenes totales, %d validas (filtro <= $%s)", len(orders), len(valid), config["max_price"])

            if bot_state["first_run"]:
                for order in valid:
                    adv = order.get("adv", {})
                    bot_state["seen"][adv.get("advNo")] = float(adv.get("price", 0))
                bot_state["first_run"] = False
                logger.info("Primera ejecucion: %d ordenes existentes registradas", len(valid))
            else:
                for order in valid:
                    adv = order.get("adv", {})
                    order_id = adv.get("advNo")
                    try:
                        current_price = float(adv.get("price", 0))
                    except (ValueError, TypeError):
                        continue

                    if order_id not in bot_state["seen"]:
                        bot_state["seen"][order_id] = current_price
                        msg = format_message(order)
                        send_telegram(token, chat_id, msg)
                        logger.info("Orden nueva notificada: %s ($%.4f)", order_id, current_price)
                    elif current_price != bot_state["seen"][order_id]:
                        old_price = bot_state["seen"][order_id]
                        bot_state["seen"][order_id] = current_price
                        msg = f"🔄 Cambio de precio detectado!\n\n" + format_message(order) + f"\n\nPrecio anterior: ${old_price:.4f}"
                        send_telegram(token, chat_id, msg)
                        logger.info("Precio cambiado: %s $%.4f -> $%.4f", order_id, old_price, current_price)

            best = find_best_price(valid)
            if best:
                best_price = float(best.get("adv", {}).get("price", 0))
                if bot_state["last_best_price"] is None or best_price != bot_state["last_best_price"]:
                    msg = format_best_price_message(best, config["max_price"])
                    send_telegram(token, chat_id, msg)
                    bot_state["last_best_price"] = best_price
                    logger.info("Mejor precio actualizado: %s", best_price)
                else:
                    logger.info("Mejor precio sin cambios: %s", best_price)

            time.sleep(config["poll_interval"])
        else:
            time.sleep(1)


if __name__ == "__main__":
    main()
