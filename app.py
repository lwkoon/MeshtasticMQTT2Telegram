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

# Get the token from the environment variable
TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Create a Telegram Bot object without custom request adapter
bot = Bot(token=TOKEN)

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
channel = os.getenv("MQTT_CHANNEL")
key = os.getenv("MQTT_MESH_KEY")

padded_key = key.ljust(len(key) + ((4 - (len(key) % 4)) % 4), '=')
replaced_key = padded_key.replace('-', '+').replace('_', '/')
key = replaced_key

# Convert hex to int and remove '!'
# node_number = int('abcd', 16)

async def send_telegram_message(bot, chat_id, text_payload):
    try:
        await bot.send_message(chat_id=chat_id, text=text_payload, parse_mode='MarkdownV2')
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

def process_message(mp, text_payload, is_encrypted):
    text = {
        "message": text_payload,
        "from": getattr(mp, "from"),
        "id": getattr(mp, "id"),
        "to": getattr(mp, "to")
    }
    print(text)

def decode_encrypted(message_packet):
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
        print('----------->service_envelope.packet.from', client_id)

        if message_packet.decoded.portnum == portnums_pb2.TEXT_MESSAGE_APP:
            text_payload = message_packet.decoded.payload.decode("utf-8")
            sendTelegramMsg(text_payload, client_id)

        elif message_packet.decoded.portnum == portnums_pb2.NODEINFO_APP:
            info = mesh_pb2.User()
            info.ParseFromString(message_packet.decoded.payload)

            if info.long_name is not None:
                print('----->nodeinfo', info)
                print(f'id:{info.id}, long_name:{info.long_name}')

                try:
                    conn = sqlite3.connect('meshtastic.db')
                    cursor = conn.cursor()

                    cursor.execute(f'SELECT * FROM {channel} WHERE client_id = ?', (client_id,))
                    existing_row = cursor.fetchone()

                    if existing_row:
                        cursor.execute(f'''UPDATE {channel} SET
                                        long_name = ?, short_name = ?
                                        WHERE client_id = ?''',
                                       (info.long_name, info.short_name, client_id))
                    else:
                        cursor.execute(f'''INSERT INTO {channel}
                                        (client_id, long_name, short_name)
                                        VALUES (?, ?, ?)''',
                                       (client_id, info.long_name, info.short_name))

                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"Update node Info failed: {e}")

        elif message_packet.decoded.portnum == portnums_pb2.POSITION_APP:
            pos = mesh_pb2.Position()
            pos.ParseFromString(message_packet.decoded.payload)

            if pos.latitude_i != 0:
                print('----->pos', pos)
                try:
                    conn = sqlite3.connect('meshtastic.db')
                    cursor = conn.cursor()

                    cursor.execute(f'SELECT * FROM {channel} WHERE client_id = ?', (client_id,))
                    existing_row = cursor.fetchone()

                    if existing_row:
                        cursor.execute(f'''UPDATE {channel} SET
                                        latitude_i = ?, longitude_i = ?, precision_bits = ?
                                        WHERE client_id = ?''',
                                       (pos.latitude_i, pos.longitude_i, pos.precision_bits, client_id))
                    else:
                        cursor.execute(f'''INSERT INTO {channel}
                                        (client_id, latitude_i, longitude_i, precision_bits)
                                        VALUES (?, ?, ?, ?)''',
                                       (client_id, pos.latitude_i, pos.longitude_i, pos.precision_bits))

                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"Update node Position failed: {e}")

        elif message_packet.decoded.portnum == portnums_pb2.TELEMETRY_APP:
            env = telemetry_pb2.Telemetry()
            env.ParseFromString(message_packet.decoded.payload)
            print('----->env', env)

    except Exception as e:
        print(f"Decryption failed: {e}")

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        client.subscribe(subscribe_topic, 0)
        print(f"Connected to {MQTT_BROKER} on topic:{subscribe_topic} id:{BROADCAST_NUM} send to telegram:{TELEGRAM_CHAT_ID}")
    else:
        print(f"Failed to connect to MQTT broker with result code {rc}")

def on_message(client, userdata, msg):
    service_envelope = mqtt_pb2.ServiceEnvelope()
    try:
        service_envelope.ParseFromString(msg.payload)
        message_packet = service_envelope.packet
    except Exception as e:
        print(f"Error parsing message_packet: {e}")
        return

    # Only process messages from a specific channel
    if message_packet.to == BROADCAST_NUM:
        if message_packet.HasField("encrypted") and not message_packet.HasField("decoded"):
            decode_encrypted(message_packet)
        else:
            print("----------------------------------*****no decode*****", mesh_pb2.Data())

def sendTelegramMsg(text_payload, client_id):
    try:
        print(f'client_id:{client_id} , text_payload: {text_payload}')
        if text_payload is not None:
            long_name = get_long_name(client_id)
            text_payload = escape_special_characters(text_payload)
            text_payload = '\n>' + '\n>'.join(text_payload.split('\n')) + '\r'
            client_id = escape_special_characters(client_id)
            if long_name is not None:
                long_name = escape_special_characters(long_name)
                print(f"The long name of client {client_id} is: {long_name}")
                msg_who = f"*{long_name} \\({client_id}\\)*:"
            else:
                print(f"No long name found for client {client_id}")
                msg_who = f"*{client_id}*:"

            # --- Instead of asyncio.run, schedule the coroutine on the persistent telegram_loop ---
            asyncio.run_coroutine_threadsafe(
                send_telegram_message(bot, TELEGRAM_CHAT_ID, msg_who + text_payload),
                telegram_loop
            )
        else:
            print("Received None payload. Ignoring...")
    except Exception as e:
        print(f"send message to telegram error: {e}")

def get_long_name(client_id):
    conn = sqlite3.connect('meshtastic.db')
    cursor = conn.cursor()
    cursor.execute("SELECT long_name FROM LongFast WHERE client_id = ?", (client_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        return row[0]
    else:
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

# Establish or connect to the database and create a data table
try:
    conn = sqlite3.connect('meshtastic.db')
    cursor = conn.cursor()

    create_table_query = f'''CREATE TABLE IF NOT EXISTS LongFast (
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
    conn.commit()
    conn.close()
except Exception as e:
    print(f"Database initialization failed: {e}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message
client.on_connect = on_connect
client.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)
client.connect(MQTT_BROKER, MQTT_PORT, 60)

subscribe_topic = f"{root_topic}{channel}/#"
client.subscribe(subscribe_topic, 0)

if __name__ == '__main__':
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("Program terminated manually.")
    except Exception as e:
        print(f"Unexpected error: {e}")

