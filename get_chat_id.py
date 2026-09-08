import json
import requests


def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    config = load_config()
    token = config["telegram_bot_token"]
    base_url = f"https://api.telegram.org/bot{token}"

    print("Verificando conexión con el bot...")
    resp = requests.get(f"{base_url}/getMe")
    if resp.status_code != 200:
        print("Error: No se pudo conectar al bot. Verifica el token.")
        return

    bot_info = resp.json()["result"]
    print(f"Bot conectado: @{bot_info['username']}\n")
    print("Envía un mensaje a tu bot en Telegram ahora.")
    print("Esperando mensajes... (presiona Ctrl+C para salir)\n")

    offset = 0
    while True:
        resp = requests.get(f"{base_url}/getUpdates", params={"offset": offset, "timeout": 30})
        data = resp.json()

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat = msg.get("chat", {})
            user = msg.get("from", {})

            chat_id = chat.get("id")
            first_name = user.get("first_name", "")
            last_name = user.get("last_name", "")

            print(f"========================================")
            print(f"  TU CHAT ID ES: {chat_id}")
            print(f"  Usuario: {first_name} {last_name}")
            print(f"========================================\n")
            print(f"Cópialo y pégalo en config.json")


if __name__ == "__main__":
    main()
