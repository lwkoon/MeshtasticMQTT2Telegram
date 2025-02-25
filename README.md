# MeshtasticMQTT2Telegram

This program is mainly used to relay conversations from a specific channel in Meshtastic's MQTT host to Telegram for storage.

# Export the required libraries

pipenv run pip freeze > requirements.txt

#
The following are the modifications I made based on several possible issues, including:

Telegram transmission part
Calling asyncio in a synchronous environment may cause event loop conflicts. Instead, use asyncio.run() to call asynchronous functions.

MQTT main loop
Changed the original while client.loop() == 0: pass to client.loop_forever() to avoid excessive CPU usage in infinite loops and abnormal program termination when the connection is disconnected.

Add exception capture
Added exception capture to some blocks that may throw exceptions (such as Telegram message sending, database operations) to avoid interrupting the application due to unhandled exceptions.
