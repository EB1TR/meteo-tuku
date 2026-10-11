from flask import Flask, request, jsonify
from datetime import datetime, timezone
from influxdb_client import InfluxDBClient, Point, WriteOptions
import logging
import os
import atexit
import json
import math
import paho.mqtt.client as mqtt

app = Flask(__name__)

# --------------------------------------------------------
# Función auxiliar para leer secretos desde fichero o env
# --------------------------------------------------------
def read_secret(name: str, default: str | None = None) -> str | None:
    """
    Prioridad:
    1) Variable NAME_FILE -> path a fichero con el valor
    2) Variable NAME       -> valor directo en entorno
    3) default             -> valor por defecto (si se pasa)
    """
    file_var = os.getenv(f"{name}_FILE")
    if file_var and os.path.isfile(file_var):
        try:
            with open(file_var, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            print(f"Error leyendo secreto {name} desde {file_var}: {e}")

    env_val = os.getenv(name)
    if env_val is not None:
        return env_val

    return default


# --------------------------------------------------------
# Configuración InfluxDB (URL no es secreta, token sí)
# --------------------------------------------------------
INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_ORG = read_secret("INFLUX_ORG", default="ed1b")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "weather")
INFLUX_TOKEN = read_secret("INFLUX_TOKEN")

# --------------------------------------------------------
# Cliente InfluxDB
# --------------------------------------------------------
client = InfluxDBClient(
    url=INFLUX_URL,
    token=INFLUX_TOKEN,
    org=INFLUX_ORG,
) if INFLUX_TOKEN else None

write_api = client.write_api(write_options=WriteOptions(batch_size=1)) if client else None

# --------------------------------------------------------
# Logging
# --------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logging.info(
    f"Arrancando weather-endpoint con Influx: url={INFLUX_URL}, org={INFLUX_ORG}, bucket={INFLUX_BUCKET}"
)


# --------------------------------------------------------
# Helpers de conversión
# --------------------------------------------------------
# MQTT se conecta en segundo plano; no bloquea la recepción ni InfluxDB.
MQTT_HOST = os.getenv("MQTT_HOST", "192.168.88.10")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "meteo/tuku")
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
mqtt_client.max_queued_messages_set(100)
mqtt_client.max_inflight_messages_set(20)
mqtt_user = read_secret("MQTT_USERNAME")
if mqtt_user:
    mqtt_client.username_pw_set(mqtt_user, read_secret("MQTT_PASSWORD"))
mqtt_client.enable_logger()
mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
mqtt_client.loop_start()
atexit.register(mqtt_client.loop_stop)
if write_api is None:
    logging.warning("InfluxDB deshabilitado: falta token; MQTT sigue activo.")


def publish_weather(fields, received_at):
    """Publica métricas únicamente, nunca PASSKEY ni el paquete crudo."""
    try:
        if not mqtt_client.is_connected():
            logging.warning("MQTT desconectado: medición no publicada.")
            return False
        payload = json.dumps(
            {"received_at": received_at.isoformat(), "fields": fields},
            separators=(",", ":"), allow_nan=False,
        )
        info = mqtt_client.publish(MQTT_TOPIC, payload, qos=1, retain=True)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logging.warning("MQTT no acepta la publicación: rc=%s", info.rc)
            return False
        return True  # Aceptado por Paho; la confirmación MQTT es asíncrona.
    except Exception:
        logging.exception("Error publicando medición MQTT")
        return False


def to_float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def f_to_c(temp_f):
    if temp_f is None:
        return None
    return (temp_f - 32.0) * 5.0 / 9.0


def mph_to_kmh(mph):
    if mph is None:
        return None
    return mph * 1.60934


def in_to_mm(inches):
    if inches is None:
        return None
    return inches * 25.4


def inhg_to_hpa(inhg):
    if inhg is None:
        return None
    return inhg * 33.8638866667


# --------------------------------------------------------
# Endpoint principal /data (GET/POST)
# --------------------------------------------------------
@app.route("/data", methods=["GET", "POST"])
def receive_data():
    # Ecowitt normalmente manda GET, pero soportamos ambos
    if request.method == "GET":
        data = request.args.to_dict()
    else:
        data = request.form.to_dict()

    # No registrar paquetes crudos: pueden contener PASSKEY.
    logging.info("Paquete meteorológico recibido de Ecowitt")

    # Parseo de valores crudos
    tempf_out = to_float(data.get("tempf"))
    tempf_in = to_float(data.get("tempinf"))
    humidity_out = to_float(data.get("humidity"))
    humidity_in = to_float(data.get("humidityin"))

    barom_rel_inhg = to_float(data.get("baromrelin"))
    barom_abs_inhg = to_float(data.get("baromabsin"))

    winddir_deg = to_float(data.get("winddir"))
    windspeed_mph = to_float(data.get("windspeedmph"))
    windgust_mph = to_float(data.get("windgustmph"))
    maxdailygust_mph = to_float(data.get("maxdailygust"))

    solarrad = to_float(data.get("solarradiation"))
    uv = to_float(data.get("uv"))

    rainrate_in = to_float(data.get("rainratein"))
    dailyrain_in = to_float(data.get("dailyrainin"))
    weeklyrain_in = to_float(data.get("weeklyrainin"))
    monthlyrain_in = to_float(data.get("monthlyrainin"))
    yearlyrain_in = to_float(data.get("yearlyrainin"))
    totalrain_in = to_float(data.get("totalrainin"))

    vpd_kpa = to_float(data.get("vpd"))
    wh65batt_raw = to_float(data.get("wh65batt"))

    # ----------------------------------------------------
    # Construimos un dict con campos MÉTRICOS y afinados
    # ----------------------------------------------------
    metric_fields: dict[str, float | int | str] = {}

    # Temperaturas
    temp_out_c = f_to_c(tempf_out)
    temp_in_c = f_to_c(tempf_in)
    if temp_out_c is not None:
        metric_fields["temp_out_c"] = temp_out_c
    if temp_in_c is not None:
        metric_fields["temp_in_c"] = temp_in_c

    # Humedad relativa
    if humidity_out is not None:
        metric_fields["humidity_out_pct"] = humidity_out
    if humidity_in is not None:
        metric_fields["humidity_in_pct"] = humidity_in

    # Presión (en hPa)
    barom_rel_hpa = inhg_to_hpa(barom_rel_inhg)
    barom_abs_hpa = inhg_to_hpa(barom_abs_inhg)
    if barom_rel_hpa is not None:
        metric_fields["pressure_rel_hpa"] = barom_rel_hpa
    if barom_abs_hpa is not None:
        metric_fields["pressure_abs_hpa"] = barom_abs_hpa

    # Viento
    if winddir_deg is not None:
        metric_fields["wind_dir_deg"] = winddir_deg

    wind_speed_kmh = mph_to_kmh(windspeed_mph)
    wind_gust_kmh = mph_to_kmh(windgust_mph)
    max_daily_gust_kmh = mph_to_kmh(maxdailygust_mph)

    if wind_speed_kmh is not None:
        metric_fields["wind_speed_kmh"] = wind_speed_kmh
    if wind_gust_kmh is not None:
        metric_fields["wind_gust_kmh"] = wind_gust_kmh
    if max_daily_gust_kmh is not None:
        metric_fields["max_daily_gust_kmh"] = max_daily_gust_kmh

    # Lluvia (mm)
    if rainrate_in is not None:
        metric_fields["rain_rate_mm_h"] = in_to_mm(rainrate_in)
    if dailyrain_in is not None:
        metric_fields["daily_rain_mm"] = in_to_mm(dailyrain_in)
    if weeklyrain_in is not None:
        metric_fields["weekly_rain_mm"] = in_to_mm(weeklyrain_in)
    if monthlyrain_in is not None:
        metric_fields["monthly_rain_mm"] = in_to_mm(monthlyrain_in)
    if yearlyrain_in is not None:
        metric_fields["yearly_rain_mm"] = in_to_mm(yearlyrain_in)
    if totalrain_in is not None:
        metric_fields["total_rain_mm"] = in_to_mm(totalrain_in)

    # Radiación solar y UV
    if solarrad is not None:
        metric_fields["solar_radiation_w_m2"] = solarrad
    if uv is not None:
        metric_fields["uv_index"] = uv

    # VPD (asumo kPa)
    if vpd_kpa is not None:
        metric_fields["vpd_kpa"] = vpd_kpa

    # Batería del sensor exterior WH65 (0=OK, 1=LOW en muchos firmwares)
    if wh65batt_raw is not None:
        metric_fields["wh65_batt_state"] = int(wh65batt_raw)

    # Si quieres, puedes usar PASSKEY como tag de estación
    station_id = data.get("PASSKEY", "unknown")

    # Si no hay ningún campo numérico útil, devolvemos error “amable”
    if not metric_fields:
        logging.warning("No se ha podido extraer ningún campo métrico de los datos recibidos.")
        return jsonify({"status": "error", "msg": "No se han podido extraer campos métricos"}), 400

    # ----------------------------------------------------
    # Crear punto para Influx
    # ----------------------------------------------------
    p = Point("weather").tag("station", station_id)

    for key, value in metric_fields.items():
        # Influx distingue numérico / string; aquí todos deberían ser numéricos salvo que metas algo extraño
        if isinstance(value, (int, float)):
            p = p.field(key, value)
        else:
            p = p.field(key, str(value))

    # Timestamp común para MQTT e InfluxDB, en UTC.
    received_at = datetime.now(timezone.utc)
    p = p.time(received_at)
    mqtt_queued = publish_weather(metric_fields, received_at)

    # Escribir en InfluxDB con manejo de errores
    try:
        if write_api is None:
            raise RuntimeError("InfluxDB deshabilitado: falta token")
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=p)
        logging.info("Medición enviada a InfluxDB; MQTT encolado=%s", mqtt_queued)
        return jsonify(
            {
                "status": "ok",
                "station": station_id,
                "fields_written": metric_fields,
                "mqtt_queued": mqtt_queued,
            }
        )
    except Exception as e:
        logging.exception(f"Error escribiendo en InfluxDB: {e}")
        return jsonify({"status": "error", "msg": "Error escribiendo en InfluxDB", "mqtt_queued": mqtt_queued}), 500


if __name__ == "__main__":
    # En desarrollo puedes arrancar así (en producción usarás gunicorn en el Dockerfile)
    app.run(host="0.0.0.0", port=8888, debug=True)
