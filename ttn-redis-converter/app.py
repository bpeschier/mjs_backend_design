#!/usr/bin/env python3
# vim:fileencoding=utf8
# pylint: disable=missing-docstring

from datetime import datetime, timezone
import logging
import os
import json
import base64
import traceback
from urllib.parse import urlparse

import redis

import paho.mqtt.client as mqtt
import bitstring
import cbor2

CONFIG_PORT = 1
DATA_PORT = 2

# Copied from ttn-redis-decoder, do not modify here
CONFIG_PACKET_KEYS = {
    1: "channel_id",
    2: "quantity",
    3: "unit",
    4: "sensor",
    5: "item_type",
    6: "measured",
    7: "divider",
}

# Copied from ttn-redis-decoder, do not modify here
CONFIG_PACKET_VALUES = {
    "quantity": {
        1: "temperature",
        2: "humidity",
        3: "voltage",
        4: "ambient_light",
        5: "particulate_matter",
        6: "position",
    },
    "unit": {
        # TODO: How to note these? Perhaps just '°C'?
        1: "degrees_celcius",
        2: "percent_rh",
        3: "volt",
        4: "ug_per_cubic_meter",
        5: "lux",
        6: "degrees",
    },
    "sensor": {1: "Si2701"},
    "item_type": {1: "node", 2: "channel"},
}

CONFIG_PACKET_KEYS_INVERTED = {v: k for k, v in CONFIG_PACKET_KEYS.items()}
CONFIG_PACKET_VALUES_INVERTED = {
    outer_k: {v: k for k, v in outer_v.items()}
    for outer_k, outer_v in CONFIG_PACKET_VALUES.items()
}


def encode_cbor_obj(obj, keys, values):
    if not isinstance(obj, dict):
        logging.warning("Element to encode is not object: %s", obj)
        return obj

    out = {}
    for key, value in obj.items():
        if isinstance(value, str):
            values_for_this_key = values.get(key, False)
            if values_for_this_key:
                try:
                    value = values_for_this_key[value]
                except KeyError:
                    pass
        if isinstance(key, str):
            try:
                key = keys[key]
            except KeyError:
                pass
        out[key] = value
    return out


# Copied from ttn-redis-decoder
def make_ttn_node_id(msg):
    return "ttn/{}/{}".format(msg["app_id"], msg["dev_id"])


# Maps station ids to the last frame counter seen
last_counter_seen = {}


def process_data(msg_obj, payload):
    stream = bitstring.ConstBitStream(bytes=payload)

    node_config = {"item_type": "node"}
    config = [
        node_config,
        {
            "item_type": "channel",
            "channel_id": 0,
            "quantity": "position",
            "unit": "degrees",
            "divider": 32768,
        },
        {
            "item_type": "channel",
            "channel_id": 1,
            "quantity": "temperature",
            "unit": "degrees_celsius",
            "divider": 16,
        },
        {
            "item_type": "channel",
            "channel_id": 2,
            "quantity": "humidity",
            "unit": "percent_rh",
            "divider": 16,
        },
    ]
    vcc_config = {
        "item_type": "channel",
        "channel_id": 3,
        "quantity": "voltage",
        "unit": "volt",
        "measured": "supply",
        "divider": 100,
        "offset": 1,
    }
    battery_config = {
        "item_type": "channel",
        "channel_id": 4,
        "quantity": "voltage",
        "unit": "volt",
        "measured": "battery",
        "divider": 50,
        "offset": 1,
    }
    lux_config = {
        "item_type": "channel",
        "channel_id": 5,
        "quantity": "ambient_light",
        "unit": "lux",
    }
    pm25_config = {
        "item_type": "channel",
        "channel_id": 6,
        "quantity": "particulate_matter",
        "unit": "ug_per_cubic_meter",
        "measured:size": 2.5,
    }
    pm10_config = {
        "item_type": "channel",
        "channel_id": 7,
        "quantity": "particulate_matter",
        "unit": "ug_per_cubic_meter",
        "measured:size": 10,
    }

    extra_config = {
        "item_type": "channel",
        "channel_id": 8,
    }

    data = []

    port = msg_obj["port"]
    length = len(payload)
    have_supply = False
    have_battery = False
    have_firmware = False
    have_lux = False
    have_pm = False
    have_extra = False
    if port == 10:
        # Legacy packet without firmware_version, with or without supply
        # and battery
        if length == 9:
            pass
        elif length == 10:
            have_supply = True
        elif length == 11:
            have_supply = True
            have_battery = True
        else:
            logging.warning(
                "Invalid packet received on port {} with length {}".format(port, length)
            )
            return
    elif port == 11:
        # Packet without lux, with or without 1 byte battery measurement, with
        # or without 4-byte particulate matter
        have_firmware = True
        have_supply = True
        if length == 11:
            pass
        elif length == 12:
            have_battery = True
        elif length == 15:
            have_pm = True
        elif length == 16:
            have_battery = True
            have_pm = True
        else:
            logging.warning(
                "Invalid packet received on port {} with length {}".format(port, length)
            )
            return
    elif port == 12:
        # Packet with 2-byte lux, with or without 1 byte battery measurement, with or
        # without 4-byte particulate matter
        have_firmware = True
        have_supply = True
        have_lux = True
        if length == 13:
            pass
        elif length == 14:
            have_battery = True
        elif length == 17:
            have_pm = True
        elif length == 18:
            have_battery = True
            have_pm = True
        else:
            logging.warning(
                "Invalid packet received on port {} with length {}".format(port, length)
            )
            return
    elif port == 13:
        # Packet starting with a flag byte that indicates which of the
        # optional values are present.
        have_firmware = True
        have_supply = True
        have_lux = True
        have_lux = stream.read("bool")
        have_pm = stream.read("bool")
        have_battery = stream.read("bool")
        # 4 bits unused
        stream.read("uint:4")
        have_extra = stream.read("bool")
        # In this packet, the lux is scaled to allow larger values
        lux_config["divider"] = 4
    else:
        logging.warning("Ignoring message with unknown port: {}".format(port))
        return

    if have_firmware:
        node_config["firmware_version"] = stream.read("uint:8")

    # Position
    data.append(
        {"channel_id": 0, "value": [stream.read("int:24"), stream.read("int:24")]}
    )

    # Temperature
    data.append({"channel_id": 1, "value": stream.read("int:12")})

    # Humidity
    data.append({"channel_id": 2, "value": stream.read("int:12")})

    if have_supply:
        config.append(vcc_config)
        data.append({"channel_id": 3, "value": stream.read("uint:8")})

    if have_lux:
        config.append(lux_config)
        data.append({"channel_id": 5, "value": stream.read("uint:16")})

    if have_pm:
        config.append(pm25_config)
        data.append({"channel_id": 6, "value": stream.read("uint:16")})
        config.append(pm10_config)
        data.append({"channel_id": 7, "value": stream.read("uint:16")})

    if have_battery:
        config.append(battery_config)
        data.append({"channel_id": 4, "value": stream.read("uint:8")})

    if have_extra:
        # Extra values are encoded as pairs of size and value, where size
        # is always 6 bits and the value is size+1 bits long.
        extra_value = []
        while stream.bitpos < len(stream):
            if len(stream) - stream.bitpos < 5:
                # This can happen due to rounding to whole bytes
                break
            # Add 1 to allow 1-32 bits rather than 0-31
            bits = stream.read("uint:5") + 1
            if len(stream) - stream.bitpos < bits:
                # This can happen due to rounding to whole bytes, in
                # which case the bits should be all-ones
                break
            value = stream.read(bits).uint
            # Just store extra values in the list
            extra_value.append(value)

        config.append(extra_config)
        data.append({"channel_id": 8, "value": extra_value})

    node_id = make_ttn_node_id(msg_obj)
    msg_counter = msg_obj["counter"]

    generate_config = False
    try:
        last_counter = last_counter_seen[node_id]
        # If the node was rebooted, simulate a new config
        if last_counter > msg_counter:
            generate_config = True
    except KeyError:
        generate_config = True

    last_counter_seen[node_id] = msg_counter

    if generate_config:
        logging.debug("Generated config payload (before shortening): %s", config)

        def encode(item):
            return encode_cbor_obj(
                item, CONFIG_PACKET_KEYS_INVERTED, CONFIG_PACKET_VALUES_INVERTED
            )

        config = list(map(encode, config))
        logging.debug("Generated config payload (after shortening): %s", config)
        yield produce_message(msg_obj, config, CONFIG_PORT)

    logging.debug("Generated data payload: %s", data)
    yield produce_message(msg_obj, data, DATA_PORT)


def produce_message(msg_obj, payload, port):
    msg_obj["port"] = port
    payload_cbor = cbor2.dumps(payload)
    msg_obj["payload_raw"] = base64.b64encode(payload_cbor).decode("utf8")

    msg_as_string = json.dumps(msg_obj)
    msg_as_bytes = msg_as_string.encode("utf8")

    return msg_as_bytes


def get_env_or_file(name, default=None):
    try:
        return os.environ[name]
    except KeyError:
        try:
            filename = os.environ[name + "_FILE"]
            with open(filename) as env_file:
                return env_file.read()
        except KeyError:
            if default is None:
                raise KeyError(name)
            return default


def main():
    def on_connect(client, userdata, flags, rc):
        logging.info("Connected to host, subscribing to uplink messages")
        client.subscribe("+/devices/+/up")

    def on_message(client, userdata, msg):
        logging.debug("Received message %s", str(msg.payload))

        try:
            msg_as_string = msg.payload.decode("utf8")
            msg_obj = json.loads(msg_as_string)
            payload = base64.b64decode(msg_obj.get("payload_raw", ""))
        # python2 uses ValueError and perhaps others, python3 uses JSONDecodeError
        # pylint: disable=broad-except
        except Exception as ex:
            logging.warning("Error parsing JSON payload")
            logging.warning(ex)
            return

        try:
            for message_bytes in process_data(msg_obj, payload):
                logging.debug("Producing new message: %s", message_bytes)
                redis_server.xadd(redis_stream, {
                    "payload": message_bytes,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        # pylint: disable=broad-except
        except Exception as ex:
            logging.warning("Error processing packet")
            logging.warning(ex)
            traceback.print_tb(ex.__traceback__)

    logging.basicConfig(level=logging.DEBUG)

    redis_stream = os.environ["REDIS_STREAM"]
    app_id = os.environ.get("TTN_CONVERT_APP_ID")
    access_key = get_env_or_file("TTN_CONVERT_ACCESS_KEY")
    ttn_host = os.environ.get("TTN_HOST", "eu.thethings.network")
    ca_cert_path = os.environ.get("TTN_CA_CERT_PATH", "mqtt-ca.pem")
    ttn_port = 8883

    redis_url = urlparse(os.environ["REDIS_URL"])
    logging.info(
        "Connecting Redis to {} on port {}".format(redis_url.hostname, redis_url.port)
    )
    redis_server = redis.Redis(
        host=redis_url.hostname, port=redis_url.port, db=int(redis_url.path[1:] or 0)
    )

    logging.info("Connecting MQTT to %s on port %s", ttn_host, ttn_port)
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.username_pw_set(app_id, password=access_key)
    mqtt_client.tls_set(ca_cert_path)
    mqtt_client.connect(ttn_host, port=ttn_port)
    mqtt_client.loop_forever()


main()

# vim: set sw=4 sts=4 expandtab:
