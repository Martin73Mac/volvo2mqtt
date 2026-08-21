import logging
import re
import time
import paho.mqtt.client as mqtt
import json
import volvo
import util
import os
import requests
import threading
from threading import Thread, Timer
from datetime import datetime
from babel.dates import format_datetime
from config import settings
from const import CLIMATE_START_URL, CLIMATE_STOP_URL, CAR_LOCK_URL, \
    CAR_UNLOCK_URL, availability_topic, icon_states, old_entity_ids, otp_mqtt_topic

mqtt_client: mqtt.Client
# Car images are the only large payloads this addon publishes. Ask the CDN for a
# narrow render and refuse to publish anything oversized: a multi-MB message takes
# the broker connection down mid-discovery (rc=7) and costs us the entity configs.
IMAGE_WIDTH = 600
IMAGE_MAX_BYTES = 256 * 1024
# How often to retry endpoints that returned no data at startup
ENDPOINT_RECHECK_SECONDS = 6 * 3600
# update_car_data runs from the update loop, from MQTT command callbacks and from
# climate/lock threads. Reentrant because api_call can call back into it on a 503.
update_lock = threading.RLock()
subscribed_topics = []
assumed_climate_state = {}
last_data_update = None
climate_timer = {}
engine_status = {}
devices = {}
active_schedules = {}
otp_code = None

def connect():
    client_id = "volvoAAOS2mqtt" if os.environ.get("IS_HA_ADDON") \
        else "volvoAAOS2mqtt_" + settings.volvoData["username"].replace("+", "")
    try:
        # clean_session=False keeps queued qos 1 messages across a reconnect, so
        # discovery configs are not lost when the broker drops the connection
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id, clean_session=False)
    except AttributeError:
        # paho-mqtt < 2.0 does not have CallbackAPIVersion
        client = mqtt.Client(client_id, clean_session=False)

    if "logging" in settings["mqtt"] and settings["mqtt"]["logging"]:
        mqtt_logger = logging.getLogger("mqtt")
        client.enable_logger(mqtt_logger)
    
    client.on_message = safe_on_message
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    client.will_set(availability_topic, "offline", 0, False)
    if settings["mqtt"]["username"] and settings["mqtt"]["password"]:
        client.username_pw_set(settings["mqtt"]["username"], settings["mqtt"]["password"])
    port = 1883
    if util.keys_exists(settings["mqtt"], "port"):
        conf_port = settings["mqtt"]["port"]
        if isinstance(conf_port, int):
            if conf_port > 0:
                port = settings["mqtt"]["port"]
    # 120s keepalive, the OTP wait can block the main thread for a long time
    client.connect(settings["mqtt"]["broker"], port, 120)

    client.loop_start()

    global mqtt_client
    mqtt_client = client


def create_otp_input():
    state_topic = otp_mqtt_topic + "/state"
    config = {
        "name": "Volvo OTP",
        "default_entity_id": "text.volvo_otp",
        "schema": "state",
        "command_topic": otp_mqtt_topic,
        "state_topic": state_topic,
        "unique_id": "volvoAAOS2mqtt_otp",
        "pattern": r"\d{6}",
        "icon": "mdi:two-factor-authentication",
        "mode": "text"
    }

    mqtt_client.publish(
        "homeassistant/text/volvoAAOS2mqtt/volvo_otp/config",
        json.dumps(config),
        qos=1,
        retain=True
    )

    mqtt_client.publish(
        state_topic,
        "000000",
        retain=True
    )

def set_otp_state():
    mqtt_client.publish(
        otp_mqtt_topic + "/state",
        otp_code,
        retain=True
    )


def send_car_images(vin, data, device):
    if util.keys_exists(data, "images"):
        for entity in [{"name": "Exterior Image", "id": "exterior_image"},
                       {"name": "Interior Image", "id": "interior_image"}]:
            image_topic = f"homeassistant/image/{vin}_{entity['id']}/image_topic"
            config = {
                "name": entity["name"],
                "default_entity_id": f"image.volvo_{vin}_{entity['id']}",
                "schema": "state",
                "icon": "mdi:image-area",
                "content_type": "image/png",
                "image_topic": image_topic,
                "device": device,
                "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
                "availability_topic": availability_topic
            }

            mqtt_client.publish(
                f"homeassistant/image/volvoAAOS2mqtt/{vin}_{entity['id']}/config",
                json.dumps(config),
                qos=1,
                retain=True
            )

            headers = {"User-Agent": "(Windows NT 10.0; Win64; x64)",
                       "sec-ch-ua-platform": "\"Windows\"", "sec-fetch-dest": "document", "Accept-Encoding": "gzip, deflate, br, zstd",
                       "sec-fetch-mode": "navigate", "sec-fetch-site": "none", "sec-fetch-user": "?1", "upgrade-insecure-requests": "1"}

            # Post Images. The width is part of the file name so a cached full size
            # image from an older version is not reused.
            image_file = f'{entity["id"]}_{vin}_w{IMAGE_WIDTH}.png'
            if not os.path.exists(image_file):
                data_path = "exteriorImageUrl" if entity["id"] == "exterior_image" else "internalImageUrl"
                image_url = re.sub(r"([?&]w=)\d+", r"\g<1>" + str(IMAGE_WIDTH), data["images"][data_path])
                response = requests.get(image_url, headers=headers)
                if response.status_code == 200:
                    with open(image_file, 'wb') as image:
                        image.write(response.content)
                else:
                    logging.warning("Error getting car images: " + str(response.status_code) + " Message: " + response.text)

            if os.path.exists(image_file):
                ext_image = open(image_file, mode="rb").read()
                if len(ext_image) > IMAGE_MAX_BYTES:
                    logging.warning(f"Not publishing {entity['id']} for {vin}: {len(ext_image) // 1024} KB exceeds "
                                    f"the {IMAGE_MAX_BYTES // 1024} KB limit, a payload this large drops the "
                                    f"broker connection and costs us the entity configs")
                    continue
                # qos 0 on purpose. A missing car picture is not worth retrying a large
                # payload forever if the broker refuses it.
                mqtt_client.publish(
                    image_topic,
                    ext_image,
                    retain=True
                )


def on_connect(client, userdata, flags, rc):
    # set a better name for the mqtt_loop Thread
    threading.current_thread().name = 'mqtt_thread'
    logging.info("MQTT connected")
    
    send_heartbeat()
    # Subscribe here, not right after connect(), so the subscriptions survive a reconnect
    mqtt_client.subscribe(otp_mqtt_topic)
    if len(subscribed_topics) > 0:
        for topic in subscribed_topics:
            mqtt_client.subscribe(topic)


def on_disconnect(client, userdata, rc):
    logging.warning(f"MQTT disconnected, reconnecting automatically rc={rc}")


def safe_on_message(client, userdata, msg):
    try:
        on_message(client, userdata, msg)
    except Exception as error:
        logging.error(f"Exception {error} processing received message on topic {msg.topic}")
        return None
        
def on_message(client, userdata, msg):
    payload = msg.payload.decode("UTF-8")
    if msg.topic == otp_mqtt_topic:
        if msg.retain == 0:
            global otp_code
            otp_code = payload
            set_otp_state()
        else:
            logging.warning("Found retained OTP, this can't work! Please clean retained messages!")
        return None
    else:
        try:
            vin = msg.topic.split('/')[2].split('_')[0]
        except IndexError:
            logging.error(f"Error - Cannot get vin from MQTT topic : {msg.topic}!")
            return None

    if "climate_status" in msg.topic:
        if payload == "ON":
            start_climate(vin)
            return None
        elif payload == "OFF":
            stop_climate(vin)
            return None
        return None
    elif "lock_status" in msg.topic:
        if payload == "LOCK":
            lock_car(vin)
            return None
        elif payload == "UNLOCK":
            unlock_car(vin)
            return None
        return None
    elif "update_data" in msg.topic:
        if payload == "PRESS":
            update_car_data(True)
            return None
        return None
    elif "update_interval" in msg.topic:
        try:
            update_interval = int(payload)
            if update_interval >= 60 or update_interval == -1:
               settings.update({"updateInterval": int(update_interval)})
            else:
                logging.warning(f"Interval {update_interval} seconds is to low. Doing nothing!")
            update_car_data()
            return None
        except ValueError as error:
            logging.error(f"Unable to change update_interval {error} payload={payload}")
            return None
    elif "schedule" in msg.topic:
        try:
            d = json.loads(payload)
        except ValueError as e:
            logging.error(f"Can't set timer. Error: {e}, payload={payload}")
            return None

        if d["mode"] == "timer":
            start_climate_timer(d, vin)
            return None
        else:
            logging.warning("No schedule mode found, doing nothing")
            return None
    return None


def start_climate_timer(d, vin):
    global active_schedules
    try:
        minute = int(d["start_time"].split(":")[1])
        hour = int(d["start_time"].split(":")[0])
        local_datetime = datetime.now(util.TZ)
        start_datetime = local_datetime.replace(hour=hour, minute=minute, second=0)
        timer_seconds = (start_datetime - local_datetime).total_seconds()
    except Exception as e:
        logging.error(f"Error creating climate timer: {e}")
        return None

    if timer_seconds > 0:
        Timer(timer_seconds, activate_climate_timer, (vin, start_datetime.isoformat(),)).start()
        active_schedules[vin]["timers"].append(start_datetime.isoformat())
        logging.debug(f"Climate timer set to {start_datetime}")
        update_car_data()
    else:
        logging.warning("Timer can not be set. Unusable start time entered")


def unlock_car(vin):
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CAR_UNLOCK_URL, "POST", vin), name="unlock_car_thread").start()

    # Force set unlocking state
    update_car_data(False, {"entity_id": "lock_status", "vin": vin, "state": "UNLOCKING"})
    # Fetch API lock state until unlocking finished
    Thread(target=volvo.check_lock_status, args=(vin, "LOCKED"), name="check_lock_status_thread").start()


def lock_car(vin):
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CAR_LOCK_URL, "POST", vin), name="lock_car_thread").start()

    # Force set locking state
    update_car_data(False, {"entity_id": "lock_status", "vin": vin, "state": "LOCKING"})
    # Fetch API lock state until locking finished
    Thread(target=volvo.check_lock_status, args=(vin, "UNLOCKED"), name="check_lock_status_thread").start()


def stop_climate(vin):
    global assumed_climate_state, climate_timer, engine_status
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CLIMATE_STOP_URL, "POST", vin), name="stop_climate_thread").start()

    # Stop door check thread if running
    if engine_status[vin].is_alive():
        engine_status[vin].do_run = False

    # Stop climate timer if active
    if climate_timer[vin].is_alive():
        climate_timer[vin].cancel()

    # Set and update switch status
    assumed_climate_state[vin] = "OFF"
    update_car_data()


def activate_climate_timer(vin, start_time):
    start_climate(vin)
    active_schedules[vin]["timers"].remove(start_time)
    update_car_data()


def start_climate(vin):
    global assumed_climate_state, climate_timer, engine_status
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CLIMATE_START_URL, "POST", vin), name="start_climate_thread").start()

    # Start door check thread to turn off climate if driver door is opened
    check_engine_thread = Thread(target=volvo.check_engine_status, args=(vin,), name="check_engine_status_thread")
    check_engine_thread.start()
    engine_status[vin] = check_engine_thread

    # Starting timer to disable climate after 30 mins
    climate_timer[vin] = Timer(30 * 60, volvo.disable_climate, (vin,))
    climate_timer[vin].start()

    # Set and update switch status
    assumed_climate_state[vin] = "ON"
    update_car_data()


def update_loop():
    # Keep retrying, a failure here used to propagate out of update_loop and stop
    # the addon with no entity ever published
    while True:
        try:
            create_ha_devices()
            break
        except Exception as error:
            logging.error(f"Exception {error} while creating HA devices. Retrying in 60 seconds.")
            time.sleep(60)

    last_endpoint_recheck = datetime.now(util.TZ)
    while True:
        if settings["updateInterval"] > 0:
            logging.info("Sending mqtt update...")
            try:
                send_heartbeat()
                update_car_data()
                if (datetime.now(util.TZ) - last_endpoint_recheck).total_seconds() >= ENDPOINT_RECHECK_SECONDS:
                    last_endpoint_recheck = datetime.now(util.TZ)
                    recheck_unsupported_endpoints()
                logging.info(f"Mqtt update done. Next run in {settings['updateInterval']} seconds.")
            except Exception as error:
                logging.info(f"Exception {error} in Mqtt update. Next run in {settings['updateInterval']} seconds.")
            time.sleep(settings["updateInterval"])
        else:
            logging.info("Data update is disabled, doing nothing for 30 seconds")
            time.sleep(30)


def update_car_data(force_update=False, overwrite={}):
    with update_lock:
        publish_car_data(force_update, overwrite)


def publish_car_data(force_update=False, overwrite={}):
    global last_data_update
    last_data_update = format_datetime(datetime.now(util.TZ), format="medium", locale=settings["babelLocale"])
    for vin in volvo.vins:
        for entity in volvo.supported_endpoints[vin]:
            if entity["domain"] in ["button"]:
                continue

            ov_entity_id = ""
            ov_vin = ""
            ov_state = ""
            if bool(overwrite):
                ov_entity_id = overwrite["entity_id"]
                ov_vin = overwrite["vin"]
                ov_state = overwrite["state"]

            if entity["id"] == "climate_status":
                state = assumed_climate_state[vin]
            elif entity["id"] == "last_data_update":
                state = last_data_update
            elif entity["id"] == "active_schedules":
                state = active_schedules[vin]
            elif entity["id"] == "update_interval":
                state = settings["updateInterval"]
            elif entity["id"] == "api_backend_status":
                if force_update:
                    state = volvo.get_backend_status()
                else:
                    state = volvo.backend_status
            else:
                if entity["id"] == ov_entity_id and vin == ov_vin:
                    state = ov_state
                else:
                    state = volvo.api_call(entity["url"], "GET", vin, entity["id"], force_update)

            if entity["domain"] == "device_tracker" or entity["id"] == "active_schedules":
                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"
            elif entity["id"] == "warnings":
                if state is not None:
                    # Only report a count when the call actually returned something,
                    # publishing 0 on a failed call reads as "no warnings"
                    mqtt_client.publish(
                        f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes",
                        json.dumps(state),
                        retain=True
                    )
                    state = sum(value == "FAILURE" for value in state.values()) if state else 0

                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state"
            else:
                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state"

            logging.info(f"update_car_data {topic} {state}")
            # None means the call failed, keep whatever HA already has. Anything else
            # is published, including "" and 0, so a value that goes away clears the
            # retained message instead of leaving a stale reading behind forever.
            if state is not None:
                mqtt_client.publish(
                    topic,
                    json.dumps(state) if isinstance(state, dict) or isinstance(state, list) else state,
                    retain=True
                )
                update_ha_device(entity, vin, state)


def update_ha_device(entity, vin, state):
    icon_config = icon_states.get(entity["id"])
    try:
        if icon_config and state:
            logging.debug("Entity " + entity["id"] + " state " + str(state))
            if isinstance(state, float):
                icon = util.get_icon_between(icon_config, state)
            elif state.replace(".", "").isnumeric():
                state = float(state)
                icon = util.get_icon_between(icon_config, state)
            else:
                icon = icon_config[state]
        else:
            return None
    except:
        return None

    logging.debug("Updating icon to " + icon + " for " + entity["id"])
    publish_entity_config(vin, entity, devices[vin], icon)


def build_entity_config(vin, entity, device, icon=None):
    """Build the MQTT discovery payload for one entity.

    Shared by create_ha_devices() and update_ha_device(). They used to build this
    separately and had drifted: the update path dropped min/max/mode for numbers
    and the attributes topic for warnings and active_schedules, so republishing a
    config quietly downgraded the entity.
    """
    config = {
        "name": entity['name'],
        "default_entity_id": f"{entity['domain']}.volvo_{vin}_{entity['id']}",
        "schema": "state",
        "icon": f"mdi:{icon}" if icon else f"mdi:{entity['icon']}",
        "state_topic": f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state",
        "device": device,
        "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
        "availability_topic": availability_topic
    }

    if entity.get("device_class"):
        config["device_class"] = entity["device_class"]

    if entity.get("unit"):
        config["unit_of_measurement"] = entity["unit"]

    if entity.get("state_class"):
        config["state_class"] = entity["state_class"]

    if (entity.get("domain") == "device_tracker" or entity.get("id") == "active_schedules"
            or entity.get("id") == "warnings"):
        config["json_attributes_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"

    if entity.get("domain") in ["switch", "lock", "button", "number"]:
        command_topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/command"
        config["command_topic"] = command_topic
        # Guard against duplicates, this used to append on every single update and
        # grow subscribed_topics without bound
        if command_topic not in subscribed_topics:
            subscribed_topics.append(command_topic)
        mqtt_client.subscribe(command_topic)

        if entity["domain"] == "number":
            config["min"] = entity["min"]
            config["max"] = entity["max"]
            config["mode"] = entity["mode"]
    elif entity.get("domain") == "image":
        config["url_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/image_url"

    return config


def publish_entity_config(vin, entity, device, icon=None):
    mqtt_client.publish(
        f"homeassistant/{entity['domain']}/volvoAAOS2mqtt/{vin}_{entity['id']}/config",
        json.dumps(build_entity_config(vin, entity, device, icon)),
        qos=1,
        retain=True
    )


def recheck_unsupported_endpoints():
    # Under the same lock as update_car_data: this appends to the very list that
    # publish_car_data iterates over
    with update_lock:
        recovered = volvo.recheck_unsupported_endpoints()
    for vin, entity in recovered:
        publish_entity_config(vin, entity, devices[vin])
        time.sleep(0.05)
    if recovered:
        logging.info(f"Added {len(recovered)} endpoints that returned data on recheck")


def create_ha_devices():
    global subscribed_topics, devices
    for vin in volvo.vins:
        device = volvo.get_vehicle_details(vin)
        devices[vin] = device
        logging.info(f"Publishing {len(volvo.supported_endpoints[vin])} entity configs for {vin}")
        for entity in volvo.supported_endpoints[vin]:
            publish_entity_config(vin, entity, device)
            # Pace the burst. Publishing ~50 configs per car back to back overruns the
            # socket buffer, and everything still queued is lost when the broker drops us.
            time.sleep(0.05)
    time.sleep(2)
    send_heartbeat()


def send_heartbeat():
    mqtt_client.publish(availability_topic, "online", retain=True)


def send_offline():
    mqtt_client.publish(availability_topic, "offline", retain=True)


def delete_old_entities():
    for vin in volvo.vins:
        for entity in old_entity_ids:
            # Clear the config so HA drops the entity, and the state and attributes
            # topics too, which used to be left behind as orphaned retained messages
            for topic in [f"homeassistant/sensor/volvoAAOS2mqtt/{vin}_{entity}/config",
                          f"homeassistant/sensor/{vin}_{entity}/state",
                          f"homeassistant/sensor/{vin}_{entity}/attributes"]:
                mqtt_client.publish(topic, payload="", retain=True)
