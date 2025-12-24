#!/usr/bin/env python3

"""
This program is adapted from https://github.com/Bamorph/Meshtastic_MQTT_Terminal/blob/main/main.py.

The copyright of the source code belongs to the original author.
"""

import logging
import os
import base64
import asyncio
import threading
import time
import paho.mqtt.client as mqtt

from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2, telemetry_pb2, BROADCAST_NUM

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# from plyer import notification

from dotenv import load_dotenv
from telegram import Bot

import sqlite3

# Load the .env file
load_dotenv()

# --- Logging Configuration: Logs to a file and the console ---
# Set the log file name from environment or default to "meshtastic.log"
log_file = os.getenv("LOG_FILE", "meshtastic.log")
logging.basicConfig(
    filename=log_file,            # Write logs to this file
    filemode='a',                 # Append mode (use 'w' to overwrite each time)
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Also add a console handler so logs appear in the terminal as well.
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)
logging.getLogger().addHandler(console_handler)

# Create a Telegram Bot object without custom request adapter
bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))

# --- Create a persistent asyncio event loop for sending Telegram messages ---
telegram_loop = asyncio.new_event_loop()

def start_telegram_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Start the telegram loop in a background thread
threading.Thread(target=start_telegram_loop, args=(telegram_loop,), daemon=True).start()

# Default settings
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

root_topic = os.getenv("MQTT_ROOT_TOPIC")

# Support multiple channels - LongFast and MediumFast
channels = {
    "LongFast": {
        "key": os.getenv("MQTT_MESH_KEY_LONGFAST", os.getenv("MQTT_MESH_KEY")),
        "broadcast_num": BROADCAST_NUM
    },
    "MediumFast": {
        "key": os.getenv("MQTT_MESH_KEY_MEDIUMFAST", os.getenv("MQTT_MESH_KEY")),
        "broadcast_num": BROADCAST_NUM
    }
}

# Process keys for both channels
for channel_name, channel_config in channels.items():
    key = channel_config["key"]
    padded_key = key.ljust(len(key) + ((4 - (len(key) % 4)) % 4), '=')
    replaced_key = padded_key.replace('-', '+').replace('_', '/')
    channel_config["key"] = replaced_key

# --- Message Deduplication ---
# Cache to track seen message IDs with timestamps to prevent duplicate Telegram forwards
# Duplicates occur when multiple relay nodes publish the same message to MQTT
DEDUP_WINDOW = int(os.getenv("DEDUP_WINDOW_SECONDS", "300"))  # Default 5 minutes
seen_messages = {}  # {packet_id: timestamp}
seen_messages_lock = threading.Lock()

def is_duplicate_message(packet_id):
    """Check if message was already seen. Returns True if duplicate."""
    current_time = time.time()

    with seen_messages_lock:
        # Clean up old entries (older than DEDUP_WINDOW)
        expired = [pid for pid, ts in seen_messages.items() if current_time - ts > DEDUP_WINDOW]
        for pid in expired:
            del seen_messages[pid]

        # Check if this message was already seen
        if packet_id in seen_messages:
            return True

        # Mark as seen
        seen_messages[packet_id] = current_time
        return False

# Convert hex to int and remove '!'
# node_number = int('abcd', 16)

async def send_telegram_message(bot, chat_id, text_payload):
    try:
        await bot.send_message(chat_id=chat_id, text=text_payload, parse_mode='MarkdownV2')
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

def process_message(mp, text_payload, is_encrypted):
    text = {
        "message": text_payload,
        "from": getattr(mp, "from"),
        "id": getattr(mp, "id"),
        "to": getattr(mp, "to")
    }
    logging.info(text)

def decode_encrypted(message_packet, channel_name, key):
    try:
        key_bytes = base64.b64decode(key.encode('ascii'))

        nonce_packet_id = getattr(message_packet, "id").to_bytes(8, "little")
        nonce_from_node = getattr(message_packet, "from").to_bytes(8, "little")
        nonce = nonce_packet_id + nonce_from_node

        cipher = Cipher(algorithms.AES(key_bytes), modes.CTR(nonce), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted_bytes = decryptor.update(getattr(message_packet, "encrypted")) + decryptor.finalize()

        data = mesh_pb2.Data()
        data.ParseFromString(decrypted_bytes)
        message_packet.decoded.CopyFrom(data)

        # Using getattr to dynamically retrieve the 'from' field
        client_id = create_node_id(getattr(message_packet, 'from', None))
        logging.info('----------->service_envelope.packet.from %s [%s]', client_id, channel_name)

        if message_packet.decoded.portnum == portnums_pb2.TEXT_MESSAGE_APP:
            text_payload = message_packet.decoded.payload.decode("utf-8")
            logging.info('[TEXT_MESSAGE] From %s [%s]: %s', client_id, channel_name, text_payload)
            sendTelegramMsg(text_payload, client_id, channel_name)

        elif message_packet.decoded.portnum == portnums_pb2.NODEINFO_APP:
            info = mesh_pb2.User()
            info.ParseFromString(message_packet.decoded.payload)

            if info.long_name is not None:
                logging.info('----->nodeinfo [%s] %s', channel_name, info)
                logging.info('id:%s, long_name:%s', info.id, info.long_name)

                try:
                    conn = sqlite3.connect('meshtastic.db')
                    cursor = conn.cursor()

                    cursor.execute(f'SELECT * FROM {channel_name} WHERE client_id = ?', (client_id,))
                    existing_row = cursor.fetchone()

                    if existing_row:
                        cursor.execute(f'''UPDATE {channel_name} SET
                                        long_name = ?, short_name = ?
                                        WHERE client_id = ?''',
                                       (info.long_name, info.short_name, client_id))
                    else:
                        cursor.execute(f'''INSERT INTO {channel_name}
                                        (client_id, long_name, short_name)
                                        VALUES (?, ?, ?)''',
                                       (client_id, info.long_name, info.short_name))

                    conn.commit()
                    conn.close()
                except Exception as e:
                    logging.error("Update node Info failed [%s]: %s", channel_name, e)

        elif message_packet.decoded.portnum == portnums_pb2.POSITION_APP:
            pos = mesh_pb2.Position()
            pos.ParseFromString(message_packet.decoded.payload)

            if pos.latitude_i != 0:
                logging.info('----->pos [%s] %s', channel_name, pos)
                try:
                    conn = sqlite3.connect('meshtastic.db')
                    cursor = conn.cursor()

                    cursor.execute(f'SELECT * FROM {channel_name} WHERE client_id = ?', (client_id,))
                    existing_row = cursor.fetchone()

                    if existing_row:
                        cursor.execute(f'''UPDATE {channel_name} SET
                                        latitude_i = ?, longitude_i = ?, precision_bits = ?
                                        WHERE client_id = ?''',
                                       (pos.latitude_i, pos.longitude_i, pos.precision_bits, client_id))
                    else:
                        cursor.execute(f'''INSERT INTO {channel_name}
                                        (client_id, latitude_i, longitude_i, precision_bits)
                                        VALUES (?, ?, ?, ?)''',
                                       (client_id, pos.latitude_i, pos.longitude_i, pos.precision_bits))

                    conn.commit()
                    conn.close()
                except Exception as e:
                    logging.error("Update node Position failed [%s]: %s", channel_name, e)

        elif message_packet.decoded.portnum == portnums_pb2.TELEMETRY_APP:
            env = telemetry_pb2.Telemetry()
            env.ParseFromString(message_packet.decoded.payload)
            logging.info('----->env [%s] %s', channel_name, env)

    except Exception as e:
        logging.error("Decryption failed [%s]: %s", channel_name, e)

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        # Subscribe to both LongFast and MediumFast channels
        for channel_name in channels.keys():
            subscribe_topic = f"{root_topic}{channel_name}/#"
            client.subscribe(subscribe_topic, 0)
            logging.info("Subscribed to %s on topic:%s id:%s", channel_name, subscribe_topic, BROADCAST_NUM)
        logging.info("Connected to %s, sending to telegram:%s", MQTT_BROKER, os.getenv("TELEGRAM_CHAT_ID"))
    else:
        logging.error("Failed to connect to MQTT broker with result code %s", rc)

def on_message(client, userdata, msg):
    service_envelope = mqtt_pb2.ServiceEnvelope()
    try:
        service_envelope.ParseFromString(msg.payload)
        message_packet = service_envelope.packet
    except Exception as e:
        # These are expected - MQTT publishes various non-protobuf messages (JSON status, stats, etc.)
        # Only log at DEBUG level to reduce noise
        logging.debug("Skipped non-protobuf message on topic %s: %s", msg.topic, e)
        return

    # Determine which channel this message is from
    channel_name = None
    for ch_name in channels.keys():
        if f"/{ch_name}/" in msg.topic:
            channel_name = ch_name
            break
    
    if channel_name is None:
        logging.debug("Message not from a monitored channel: %s", msg.topic)
        return

    # Deduplication check - skip if we've already processed this packet ID
    packet_id = getattr(message_packet, "id", None)
    if packet_id and is_duplicate_message(packet_id):
        logging.debug("Duplicate message (packet_id=%s) from %s, skipping", packet_id, msg.topic)
        return

    # Only process messages from the broadcast number
    if message_packet.to == channels[channel_name]["broadcast_num"]:
        if message_packet.HasField("encrypted") and not message_packet.HasField("decoded"):
            decode_encrypted(message_packet, channel_name, channels[channel_name]["key"])
        else:
            logging.debug("Received unencrypted message (skipped)")

def load_blacklist():
    """Load blacklisted node IDs from blacklist.txt"""
    blacklist = set()
    try:
        if os.path.exists('blacklist.txt'):
            with open('blacklist.txt', 'r') as f:
                for line in f:
                    line = line.strip()
                    # Skip empty lines and comments
                    if line and not line.startswith('#'):
                        blacklist.add(line)
            logging.info(f"Loaded {len(blacklist)} node IDs from blacklist")
        else:
            logging.info("No blacklist.txt found, all nodes will be published")
    except Exception as e:
        logging.error(f"Error loading blacklist: {e}")
    return blacklist

def sendTelegramMsg(text_payload, client_id, channel_name):
    try:
        logging.info('client_id:%s , channel:%s, text_payload: %s', client_id, channel_name, text_payload)

        # Check if node ID is blacklisted
        blacklist = load_blacklist()
        if client_id in blacklist:
            logging.info(f"Node {client_id} is blacklisted, skipping Telegram publish")
            return

        if text_payload is not None:
            long_name = get_long_name(client_id, channel_name)
            text_payload = escape_special_characters(text_payload)
            text_payload = '\n>' + '\n>'.join(text_payload.split('\n')) + '\r'
            client_id_escaped = escape_special_characters(client_id)
            channel_name_escaped = escape_special_characters(channel_name)
            
            if long_name is not None:
                long_name = escape_special_characters(long_name)
                logging.info("The long name of client %s [%s] is: %s", client_id, channel_name, long_name)
                msg_who = f"*{long_name} \\({client_id_escaped}\\)* \\[{channel_name_escaped}\\]:"
            else:
                logging.info("No long name found for client %s [%s]", client_id, channel_name)
                msg_who = f"*{client_id_escaped}* \\[{channel_name_escaped}\\]:"

            # --- Instead of asyncio.run, schedule the coroutine on the persistent telegram_loop ---
            asyncio.run_coroutine_threadsafe(
                send_telegram_message(bot, os.getenv("TELEGRAM_CHAT_ID"), msg_who + text_payload),
                telegram_loop
            )
        else:
            logging.info("Received None payload. Ignoring...")
    except Exception as e:
        logging.error("send message to telegram error: %s", e)

def get_long_name(client_id, channel_name):
    try:
        conn = sqlite3.connect('meshtastic.db')
        cursor = conn.cursor()
        cursor.execute(f"SELECT long_name FROM {channel_name} WHERE client_id = ?", (client_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return row[0]
        else:
            return None
    except Exception as e:
        logging.error(f"Error getting long_name from {channel_name}: {e}")
        return None

def escape_special_characters(text):
    special_characters = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    escaped_text = ''
    for char in text:
        if char in special_characters:
            escaped_text += '\\' + char
        else:
            escaped_text += char
    return escaped_text

def create_node_id(node_number):
    return f"!{hex(node_number)[2:]}"

# Establish or connect to the database and create data tables for both channels
try:
    conn = sqlite3.connect('meshtastic.db')
    cursor = conn.cursor()

    for channel_name in channels.keys():
        create_table_query = f'''CREATE TABLE IF NOT EXISTS {channel_name} (
                                    client_id TEXT PRIMARY KEY NOT NULL,
                                    long_name TEXT,
                                    short_name TEXT,
                                    macaddr TEXT,
                                    latitude_i TEXT,
                                    longitude_i TEXT,
                                    altitude TEXT,
                                    precision_bits TEXT,
                                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                )'''
        cursor.execute(create_table_query)
        logging.info(f"Table {channel_name} created or already exists")
    
    conn.commit()
    conn.close()
except Exception as e:
    logging.error("Database initialization failed: %s", e)

# Load and display blacklist status at startup
startup_blacklist = load_blacklist()
if startup_blacklist:
    logging.info("[BLACKLIST] Active: %d node(s) will be filtered: %s", len(startup_blacklist), ', '.join(sorted(startup_blacklist)))
else:
    logging.info("[BLACKLIST] Inactive - all nodes will be published to Telegram")

# Log deduplication status
logging.info("[DEDUP] Active with %d second window - duplicate messages will be filtered", DEDUP_WINDOW)

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message
client.on_connect = on_connect
client.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
client.connect(MQTT_BROKER, MQTT_PORT, 60)

if __name__ == '__main__':
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        logging.info("Program terminated manually.")
    except Exception as e:
        logging.error("Unexpected error: %s", e)
