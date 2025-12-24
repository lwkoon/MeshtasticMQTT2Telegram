# MeshtasticMQTT2Telegram

Relays messages from Meshtastic mesh network channels via MQTT to Telegram for storage and monitoring.

## Features

- Connects to public Meshtastic MQTT broker
- Decrypts AES-CTR encrypted mesh messages
- Forwards text messages to Telegram with sender info
- Stores node metadata (name, position) in SQLite database
- Supports multiple channels (LongFast, MediumFast)
- Message deduplication to prevent duplicate forwards
- Node blacklist filtering

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and configure:
```bash
cp .env.example .env
```

3. Run the application:
```bash
python app3.py
```

## Configuration

Edit `.env` file:

| Variable | Description |
|----------|-------------|
| `MQTT_BROKER` | MQTT broker hostname |
| `MQTT_PORT` | MQTT broker port |
| `MQTT_USERNAME` | MQTT username |
| `MQTT_PASSWORD` | MQTT password |
| `MQTT_ROOT_TOPIC` | Base topic path (e.g., `msh/US/`) |
| `MQTT_MESH_KEY_LONGFAST` | Base64 encryption key for LongFast channel |
| `MQTT_MESH_KEY_MEDIUMFAST` | Base64 encryption key for MediumFast channel |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID to send messages |
| `LOG_FILE` | Log file path (default: `meshtastic.log`) |
| `DEDUP_WINDOW_SECONDS` | Deduplication window in seconds (default: `300`) |

## Deduplication

Messages are deduplicated by packet ID to prevent the same message from being forwarded multiple times. This is necessary because:
- Multiple relay nodes publish the same message to MQTT
- Messages may arrive via different frequency bands (e.g., 919MHz and 433MHz)

The dedup window is configurable via `DEDUP_WINDOW_SECONDS` (default: 5 minutes).

## Filtering

Create a `blacklist.txt` file to filter specific nodes (one node ID per line):
```
# Comments start with #
!abcd1234
!efgh5678
```

## Export Dependencies

```bash
pipenv run pip freeze > requirements.txt
```
