# Meteo Tuku

Recepción de datos de una estación Ecowitt/FineOffset, conversión a unidades
métricas y envío a InfluxDB y MQTT. Grafana permite consultar el histórico.

El servicio HTTP utiliza Flask y Gunicorn sobre Python 3.11. El proyecto incluye
InfluxDB 2.7 y Grafana mediante Docker Compose; el broker MQTT es externo.

## Estructura

```text
app.py                  Recepción, conversión y publicación de mediciones
Dockerfile              Imagen del servicio weather-endpoint
requirements.txt        Dependencias Python
docker-compose.yaml     Servicios, puertos y volúmenes
dashboard               Recurso local de dashboard, sin importación automática
.gitignore              Exclusión de secretos y archivos locales
env/                    Configuración local, excluida de Git
  influxdb.env
  weather-endpoint.env
```

## Configuración

Docker Compose espera dos archivos locales: `env/influxdb.env` y
`env/weather-endpoint.env`. Deben existir antes de arrancar y no deben
subirse al repositorio. Los ejemplos siguientes son plantillas, no valores
reales ni credenciales predeterminadas del proyecto.

Para inicializar una instalación nueva de InfluxDB, el archivo
`env/influxdb.env` puede contener:

```dotenv
DOCKER_INFLUXDB_INIT_MODE=setup
DOCKER_INFLUXDB_INIT_USERNAME=admin
DOCKER_INFLUXDB_INIT_PASSWORD=REEMPLAZAR_POR_UNA_CONTRASENA_SEGURA
DOCKER_INFLUXDB_INIT_ORG=ed1b
DOCKER_INFLUXDB_INIT_BUCKET=weather
DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=REEMPLAZAR_POR_UN_TOKEN_SEGURO
```

Estas opciones de inicialización no reconfiguran una base de datos ya existente.
No borres sus volúmenes para actualizar el servicio.

Configuración de `env/weather-endpoint.env`:

```dotenv
INFLUX_URL=http://influxdb:8086
INFLUX_ORG=ed1b
INFLUX_BUCKET=weather
INFLUX_TOKEN=REEMPLAZAR_POR_UN_TOKEN_CON_PERMISO_DE_ESCRITURA
```

El token debe tener permiso de escritura en el bucket indicado. Los valores
predeterminados del código son `http://influxdb:8086`, `ed1b` y `weather`;
el token no tiene valor predeterminado.

El Compose configura directamente:

```yaml
MQTT_HOST: "192.168.88.10"
MQTT_PORT: "1883"
MQTT_TOPIC: "meteo/tuku"
```

Modifica esos valores en `docker-compose.yaml` si cambia el broker. Las variables
de `environment` en Compose prevalecen sobre las de `env_file`.
Opcionalmente, el endpoint admite `MQTT_USERNAME` y `MQTT_PASSWORD`.

El código también permite `INFLUX_TOKEN_FILE`, `INFLUX_ORG_FILE`,
`MQTT_USERNAME_FILE` y `MQTT_PASSWORD_FILE`. Para utilizarlas hay que montar
los archivos dentro del contenedor. El Compose actual **no configura Docker
Secrets ni esos montajes**. Si el archivo indicado existe y se puede leer,
su contenido tiene prioridad sobre la variable directa.

## Arranque y actualización

Desde el directorio del proyecto, con Docker y Docker Compose disponibles:

```bash
docker compose up -d --build
```

Para reconstruir únicamente el receptor meteorológico:

```bash
docker compose up -d --build --no-deps weather-endpoint
```

Para comprobar su estado y registros:

```bash
docker compose ps
docker compose logs --tail=100 weather-endpoint
```

`depends_on` establece el orden de arranque, pero no comprueba que InfluxDB
esté listo. El receptor utiliza reconexión automática para MQTT.

Los puertos publicados son 8888 para el receptor y 3000 para Grafana.
InfluxDB utiliza `http://influxdb:8086` dentro de la red de Compose y no publica
un puerto al host. Sus datos y los de Grafana persisten en los volúmenes
`influxdb_data` y `grafana_data`.

## Recepción de datos

Configura el servidor personalizado de la estación para enviar sus mediciones
a `http://TU_SERVIDOR:8888/data`. El endpoint admite:

- GET con parámetros de consulta.
- POST con formulario; no admite un cuerpo JSON como entrada.

Solo se incluyen campos numéricos válidos. Valores ausentes, no numéricos o
no finitos se omiten. Si no queda ninguna métrica, devuelve HTTP 400.

Cada medición utiliza la hora de recepción del servidor en UTC, no la hora
de la estación. InfluxDB recibe la medición `weather` con la etiqueta
`station`, cuyo valor es PASSKEY o `unknown` si no está presente.

### Prueba manual

Esta petición inserta una medición de prueba y también intenta publicarla por
MQTT; no es una comprobación de salud sin efectos:

```bash
curl "http://localhost:8888/data?tempf=68&humidity=56"
```

Ejemplo de respuesta si el envío a InfluxDB se acepta y MQTT está conectado:

```json
{
  "status": "ok",
  "station": "unknown",
  "fields_written": {
    "temp_out_c": 20.0,
    "humidity_out_pct": 56.0
  },
  "mqtt_queued": true
}
```

La escritura en InfluxDB es asíncrona: `fields_written` indica los campos
enviados al cliente, no una confirmación de almacenamiento. Del mismo modo,
`mqtt_queued` confirma que Paho aceptó la publicación, no su entrega al broker.

Si la llamada de escritura a InfluxDB falla, devuelve HTTP 500, incluyendo
`mqtt_queued`. Sin token InfluxDB, el servicio puede arrancar y publicar por
MQTT, pero las peticiones válidas a `/data` devolverán HTTP 500.

## Publicación MQTT

Cada paquete con métricas válidas intenta publicar un JSON en `meteo/tuku`,
con QoS 1 y mensaje retenido. Ejemplo:

```json
{
  "received_at": "2026-10-11T18:00:00+00:00",
  "fields": {
    "temp_out_c": 20.0,
    "humidity_out_pct": 56.0
  }
}
```

El mensaje contiene solo la fecha de recepción y los campos métricos
disponibles. No incluye PASSKEY, credenciales ni el paquete crudo.

MQTT se conecta en segundo plano, con reintentos entre 1 y 30 segundos.
Si está desconectado, se omite esa publicación y se sigue intentando escribir
en InfluxDB; no se guarda un histórico de paquetes durante la desconexión.
La siguiente medición se publica tras reconectar. Si falla InfluxDB, MQTT
se intenta antes y no depende del resultado de esa escritura.

La cola de publicaciones está limitada a 100 mensajes, con un máximo de
20 en vuelo. QoS 1 permite duplicados; no garantiza entrega exactamente una vez.

El mensaje retenido permite obtener la última medición al suscribirse,
pero no asegura que sea reciente. Los consumidores deben comprobar
`received_at`. La vista `stack-v2.html` del proyecto `py-antena-ctrl`
se suscribe a este topic y muestra `--` si la temperatura supera cinco minutos
de antigüedad.

## Campos métricos

Los mismos campos disponibles se envían a InfluxDB y MQTT:

| Campo | Unidad | Campo recibido |
| --- | --- | --- |
| temp_out_c | °C | tempf |
| temp_in_c | °C | tempinf |
| humidity_out_pct | % | humidity |
| humidity_in_pct | % | humidityin |
| pressure_rel_hpa | hPa | baromrelin |
| pressure_abs_hpa | hPa | baromabsin |
| wind_dir_deg | grados | winddir |
| wind_speed_kmh | km/h | windspeedmph |
| wind_gust_kmh | km/h | windgustmph |
| max_daily_gust_kmh | km/h | maxdailygust |
| solar_radiation_w_m2 | W/m² | solarradiation |
| uv_index | índice | uv |
| rain_rate_mm_h | mm/h | rainratein |
| daily_rain_mm | mm | dailyrainin |
| weekly_rain_mm | mm | weeklyrainin |
| monthly_rain_mm | mm | monthlyrainin |
| yearly_rain_mm | mm | yearlyrainin |
| total_rain_mm | mm | totalrainin |
| vpd_kpa | kPa, según el campo recibido | vpd |
| wh65_batt_state | estado entero del sensor | wh65batt |

Temperaturas: Fahrenheit a Celsius; viento: mph a km/h; lluvia: pulgadas a
milímetros; presión: inHg a hPa. La interpretación de VPD y batería debe
confirmarse con el modelo y firmware de la estación.

## Grafana

Accede a `http://TU_SERVIDOR:3000`. El Compose habilita lectura anónima
con rol Viewer, mantiene el formulario de login y deshabilita el registro
de usuarios. Los usuarios autenticados con permisos suficientes pueden editar.

Configura manualmente una fuente de datos InfluxDB con Flux:

- URL: `http://influxdb:8086`.
- Organización: la configurada en InfluxDB.
- Bucket: el configurado para las mediciones.
- Token: uno con permiso de lectura para ese bucket.

El Compose no aprovisiona automáticamente la fuente de datos ni dashboards.

## Seguridad y limitaciones

- `/data` no autentica peticiones. PASSKEY se usa como etiqueta y aparece
  en la respuesta HTTP; no se valida como contraseña.
- No publiques el receptor ni Grafana en Internet sin protección adicional.
  El Compose no configura TLS; MQTT utiliza TCP sin cifrar en el puerto 1883.
- Los paquetes crudos y PASSKEY no se registran en los mensajes de recepción
  ni se publican por MQTT; PASSKEY sí se almacena como etiqueta en InfluxDB.
- `env/`, `secrets/`, archivos de entorno y entornos virtuales están
  excluidos por `.gitignore`. No incluyas valores reales en la documentación.
- `.gitignore` no excluye archivos del contexto de construcción Docker.
  El Dockerfile solo copia código y dependencias, pero conviene añadir un
  `.dockerignore` antes de enviar el contexto a un constructor remoto.
- El código no valida los rangos físicos de las mediciones ni separa estaciones
  en topics MQTT diferentes. Si varias estaciones usan el endpoint, compartirán
  el mensaje retenido de `meteo/tuku`.
- No ejecutes `docker compose down -v` para una actualización: elimina los
  volúmenes y puede borrar el histórico y la configuración.
