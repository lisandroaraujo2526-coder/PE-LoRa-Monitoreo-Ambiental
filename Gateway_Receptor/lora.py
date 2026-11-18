from LoRaRF import SX126x
import time
import os
import json

import signal
import threading
from pathlib import Path


from mqtt_gateway import PublicadorMQTT, id_gateway
from protocolo import (
    armar_payload,
    cargar_env,
    extraer_campos,
    parsear_mensaje,
    topic_seguro,
)


# Carga del .env junto a este script (el entorno tiene prioridad).
cargar_env(Path(__file__).resolve().parent / ".env")


# systemd detiene el servicio con SIGTERM: se trata igual que Ctrl+C para
# cerrar ordenadamente la radio y MQTT.
def _sigterm(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _sigterm)


# ---------------------------------------------------------------------------
# MQTT sobre WebSocket seguro (WSS, detras de Cloudflare).
# Los mensajes se guardan primero en una cola en disco (mqtt_cola.db) y un
# hilo aparte los envia: si se cae internet o el broker, el bucle LoRa sigue
# recibiendo y nada se pierde. Ver mqtt_gateway.py.
# ---------------------------------------------------------------------------
# Cada nodo publica en <MQTT_TOPIC_BASE>/<node>, p. ej. sensores/tx1.
# Del otro lado: sensores/# recibe todos, sensores/tx1 solo ese nodo.
MQTT_TOPIC_BASE = os.getenv("MQTT_TOPIC_BASE", "sensores")
GATEWAY_ID = id_gateway()
# Estado del gateway (JSON retenido): online + estadisticas cada
# ESTADO_CADA_S; "offline" lo publica el broker si el gateway se cae.
# Vacio = desactivado.
MQTT_STATUS_TOPIC = os.getenv("MQTT_STATUS_TOPIC",
                              f"{MQTT_TOPIC_BASE}/gateway/estado")
ESTADO_CADA_S = float(os.getenv("ESTADO_CADA_S", "60"))
# Sin paquetes durante este tiempo -> "radio": "sin_paquetes" (alerta).
ALERTA_SIN_PAQUETES_S = float(os.getenv("ALERTA_SIN_PAQUETES_S", "300"))
# Un nodo se considera inactivo si no transmite en este tiempo.
NODO_INACTIVO_S = float(os.getenv("NODO_INACTIVO_S", "60"))


class ActividadRadio:
    """Lleva la cuenta de los paquetes validos recibidos, por nodo."""

    def __init__(self):
        self._lock = threading.Lock()
        self.inicio = time.time()
        self.paquetes = 0
        self.ultimo = None
        self.nodos: dict[str, dict] = {}
        self._alerta = False

    def registrar(self, node: str, campos: dict) -> None:
        ahora = time.time()
        with self._lock:
            self.paquetes += 1
            self.ultimo = ahora
            n = self.nodos.setdefault(node, {"paquetes": 0})
            n["paquetes"] += 1
            n["ultimo"] = ahora
            n["seq"] = campos.get("seq")
            n["rssi"] = campos.get("rssi")
            n["snr"] = campos.get("snr")

    def resumen(self) -> dict:
        """Datos para el mensaje de estado (se llama desde el hilo MQTT)."""
        ahora = time.time()
        with self._lock:
            referencia = self.ultimo or self.inicio
            silencio = ahora - referencia
            radio = "sin_paquetes" if silencio > ALERTA_SIN_PAQUETES_S else "ok"
            if radio == "sin_paquetes" and not self._alerta:
                print(f"ALERTA: sin paquetes LoRa hace {silencio:.0f} s")
            elif radio == "ok" and self._alerta:
                print("Recepcion LoRa normalizada")
            self._alerta = radio == "sin_paquetes"

            nodos = {}
            for nombre, n in self.nodos.items():
                hace = ahora - n["ultimo"]
                nodos[nombre] = {
                    "activo": hace <= NODO_INACTIVO_S,
                    "ultimo_hace_s": int(hace),
                    "paquetes": n["paquetes"],
                    "seq": n["seq"],
                    "rssi": n["rssi"],
                    "snr": n["snr"],
                }
            return {
                "uptime_s": int(ahora - self.inicio),
                "radio": radio,
                "paquetes_recibidos": self.paquetes,
                "ultimo_paquete_hace_s": (int(ahora - self.ultimo)
                                          if self.ultimo else None),
                "nodos_activos": sum(n["activo"] for n in nodos.values()),
                "nodos": nodos,
            }


actividad = ActividadRadio()

mqtt_pub = PublicadorMQTT.desde_entorno(
    client_id=f"gw-{GATEWAY_ID}",
    db_path=Path(__file__).resolve().parent / "mqtt_cola.db",
    status_topic=MQTT_STATUS_TOPIC,
    estado_base={"gateway": GATEWAY_ID},
    estado_fn=actividad.resumen,
    estado_cada=ESTADO_CADA_S,
)
mqtt_pub.iniciar()


def publicar_mqtt(node: str, campos: dict) -> None:
    """Encola la lectura como JSON para <MQTT_TOPIC_BASE>/<node> (QoS 1)."""
    actividad.registrar(node, campos)
    payload = armar_payload(node, GATEWAY_ID, campos)
    mqtt_pub.publicar(f"{MQTT_TOPIC_BASE}/{topic_seguro(node)}",
                      json.dumps(payload))


LoRa = SX126x()

# IMPORTANTE:
# irq=-1 desactiva el callback GPIO de la librería original.
# La recepción se controla mediante polling dentro de LoRa.wait().
if not LoRa.begin(
    bus=0,
    cs=0,
    reset=18,
    busy=20,
    irq=-1,
    txen=6,
    rxen=-1,
    wake=-1
):
    raise RuntimeError("Error iniciando SX1262")

# Configuración compatible con el Waveshare HAT.
# No activar DIO2 ni DIO3 porque este HAT está funcionando con TXEN GPIO 6.
#
# LoRa.setDio2RfSwitch(True)
# LoRa.setDio3TcxoCtrl(...)

LoRa.setFrequency(915000000)

LoRa.setLoRaModulation(
    sf=9,
    bw=125000,
    cr=7,
    ldro=False
)

LoRa.setLoRaPacket(
    headerType=LoRa.HEADER_EXPLICIT,
    preambleLength=8,
    payloadLength=240,
    crcType=True,
    invertIq=False
)

LoRa.setSyncWord(0x3444)
LoRa.setRxGain(LoRa.RX_GAIN_BOOSTED)

errores = LoRa.getError()
if errores:
    # Bit 0x20 = XOSC_START_ERR: el oscilador/TCXO no arrancó bien.
    print(f"ADVERTENCIA: errores del SX1262 = 0x{errores:04X}")

print("SX1262 Waveshare iniciado")
print("Recepción por polling, sin callback GPIO")
print(f"Publicando en MQTT: {MQTT_TOPIC_BASE}/<node>")
print(f"Estado del gateway en: {MQTT_STATUS_TOPIC or '(desactivado)'}")
print("Escuchando paquetes LoRa en 915 MHz...")


while True:
    try:
        # Iniciar una nueva recepción.
        # request() devuelve False si el chip sigue en modo RX (p. ej. tras
        # un error de cabecera, el SX126x NO sale de RX por sí solo).
        # Se fuerza standby y se reintenta.
        if not LoRa.request():
            print("Chip seguía en RX: forzando standby y reintentando")
            LoRa.standby()
            continue

        # Al tener irq=-1, wait() consulta el IRQ por SPI.
        LoRa.wait()

        estado = LoRa.status()

        if estado == LoRa.STATUS_CRC_ERR:
            print("Paquete descartado: error CRC")
            continue

        if estado == LoRa.STATUS_HEADER_ERR:
            # Tras HEADER_ERR el chip permanece en RX; hay que sacarlo.
            print("Paquete descartado: error de cabecera")
            LoRa.standby()
            continue

        if estado == LoRa.STATUS_RX_TIMEOUT:
            print("Timeout de recepción")
            continue

        if estado != LoRa.STATUS_RX_DONE:
            print(f"Estado RX inesperado: {estado}")
            continue

        # wait() ya consultó GetRxBufferStatus y guardó:
        # - cantidad recibida
        # - offset inicial del FIFO
        payload_length = LoRa.available()

        if payload_length <= 0:
            print("RX_DONE sin datos disponibles")
            continue

        if payload_length > 240:
            print(
                f"Longitud inválida: {payload_length} bytes. "
                "Paquete descartado."
            )
            LoRa.purge()
            continue

        # Leer todo el paquete en una única operación SPI.
        datos_rx = LoRa.get(payload_length)

        print("-" * 65)
        print(f"Payload: {payload_length} bytes")
        print(f"HEX: {datos_rx.hex()}")

        try:
            mensaje = datos_rx.decode("ascii")
        except UnicodeDecodeError as error:
            print(f"Paquete no ASCII: {error}")
            continue

        valores = parsear_mensaje(mensaje)

        # Validación básica de estructura.
        if "node" not in valores:
            print(f"Paquete sin campo node: {mensaje}")
            continue

        rssi = LoRa.packetRssi()
        snr = LoRa.snr()

        print(f"Recibido: {mensaje}")
        print(f"RSSI: {rssi:.1f} dBm | SNR: {snr:.2f} dB")

        print(f"Nodo: {valores.get('node', 'N/D')}")
        print(f"Secuencia: {valores.get('seq', 'N/D')}")
        print(f"Uptime: {valores.get('up', 'N/D')} segundos")

        if valores.get("gas") == "1":
            print(
                "Gases: "
                f"O2={valores.get('O2', 'N/D')} %, "
                f"CO={valores.get('CO', 'N/D')} ppm, "
                f"H2S={valores.get('H2S', 'N/D')} ppm, "
                f"CH4={valores.get('CH4', 'N/D')}"
            )
        else:
            print("Sensor de gases sin datos válidos")

        if valores.get("dht") == "1":
            print(
                f"Temperatura: {valores.get('T', 'N/D')} °C | "
                f"Humedad: {valores.get('H', 'N/D')} %"
            )
        else:
            print("DHT22 sin datos válidos")

        # -------------------------------------------------------------------
        # Envio al broker MQTT: solo encola en disco (no bloquea la radio).
        # -------------------------------------------------------------------
        node = valores.get("node", "desconocido")
        campos = extraer_campos(valores, rssi, snr)

        try:
            publicar_mqtt(node, campos)
        except Exception as error:
            print(f"Error publicando en MQTT: {error}")

    except KeyboardInterrupt:
        print("\nRecepción detenida por el usuario")
        break

    except Exception as error:
        print(f"Error durante la recepción: {error}")
        time.sleep(1)


LoRa.end()
mqtt_pub.cerrar()
