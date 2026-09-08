# Binance P2P Telegram Bot

Bot de Telegram que monitorea órdenes P2P de Binance y te notifica cuando hay órdenes de compra de USDT que cumplen con tus condiciones de precio.

## Configuración

### 1. Crear bot de Telegram

1. Abre Telegram y busca `@BotFather`
2. Envía `/newbot` y sigue las instrucciones
3. Copia el token que te proporcione

### 2. Obtener tu Chat ID

1. Busca `@userinfobot` en Telegram
2. Envía cualquier mensaje
3. Copia tu `Chat ID`

### 3. Configurar el bot

Edita el archivo `config.json`:

```json
{
  "telegram_bot_token": "TU_TOKEN_AQUI",
  "telegram_chat_id": "TU_CHAT_ID_AQUI",
  "crypto": "USDT",
  "fiat": "USD",
  "min_price": 1.02,
  "payment_methods": [],
  "poll_interval": 60
}
```

| Campo | Descripción |
|-------|-------------|
| `telegram_bot_token` | Token obtenido de @BotFather |
| `telegram_chat_id` | Tu ID de chat de Telegram |
| `crypto` | Criptocurrency a comprar (USDT) |
| `fiat` | Moneda fiat (USD) |
| `min_price` | Precio mínimo para filtrar órdenes |
| `payment_methods` | Métodos de pago específicos (vacío = todos) |
| `poll_interval` | Segundos entre cada consulta (60 = 1 minuto) |

## Instalación

```bash
pip install -r requirements.txt
```

## Ejecución

```bash
python bot.py
```

## Comandos del bot en Telegram

| Comando | Descripción |
|---------|-------------|
| `/start` | Iniciar monitoreo |
| `/stop` | Detener monitoreo |
| `/status` | Ver estado actual |
| `/setprice <valor>` | Cambiar precio mínimo |

## Ejemplo

```
/setprice 1.05
```

Establece el precio mínimo a $1.05. Solo se notificarán órdenes con precio >= $1.05.
