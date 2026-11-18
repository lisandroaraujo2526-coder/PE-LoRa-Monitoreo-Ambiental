"""
Protocolo de las tramas LoRa de los nodos (comun al gateway y al simulador).

Trama que envia cada ESP32 (ver codigo_esp32.txt):
    node=tx1;seq=10;up=300;gas=1;O2=20.9;CO=0;H2S=0.0;CH4=0;dht=1;T=21.5;H=60.2
Si un sensor no responde, manda gas=0 o dht=0 sin sus valores.
"""
import os
import re
import time
from pathlib import Path


def cargar_env(ruta: Path) -> None:
    if not ruta.is_file():
        return
    for linea in ruta.read_text().splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        clave = clave.removeprefix("export ").strip()
        valor = valor.strip().strip('"').strip("'")
        os.environ.setdefault(clave, valor)


def a_float(valor):
    """Convierte a float o devuelve None si no es numerico."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def a_int(valor):
    """Convierte a int o devuelve None si no es entero."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def parsear_mensaje(mensaje: str) -> dict[str, str]:
    """
    Convierte:
    node=tx1;seq=10;T=21.5;H=60.2

    En:
    {
        "node": "tx1",
        "seq": "10",
        "T": "21.5",
        "H": "60.2"
    }
    """
    resultado = {}

    for campo in mensaje.split(";"):
        if "=" not in campo:
            continue

        clave, valor = campo.split("=", 1)
        resultado[clave] = valor

    return resultado


def extraer_campos(valores: dict, rssi: float, snr: float) -> dict:
    """
    Normaliza el mensaje parseado a campos tipados (comun a Influx y MQTT).

    - rssi / snr -> siempre presentes (calidad de enlace)
    - resto de campos -> solo si vienen y son numericos validos
    """
    campos = {"rssi": float(rssi), "snr": float(snr)}

    seq = a_int(valores.get("seq"))
    if seq is not None:
        campos["seq"] = seq

    up = a_int(valores.get("up"))
    if up is not None:
        campos["uptime"] = up

    # Ambiente (DHT22)
    if valores.get("dht") == "1":
        for clave, campo in (("T", "temperatura"), ("H", "humedad")):
            v = a_float(valores.get(clave))
            if v is not None:
                campos[campo] = v

    # Gases (ZCE04B)
    if valores.get("gas") == "1":
        for clave, campo in (
            ("O2", "o2"),
            ("CO", "co"),
            ("H2S", "h2s"),
            ("CH4", "ch4"),
        ):
            v = a_float(valores.get(clave))
            if v is not None:
                campos[campo] = v

    return campos


def topic_seguro(node: str) -> str:
    """Evita que un node con '/', '+' o '#' rompa la jerarquia de topics."""
    limpio = re.sub(r"[^A-Za-z0-9_-]", "_", node)
    return limpio or "desconocido"


def armar_payload(node: str, gateway: str, campos: dict) -> dict:
    """JSON que se publica en MQTT para una lectura."""
    return {
        "node": node,
        "gateway": gateway,
        "ts": int(time.time()),
        **campos,
    }
