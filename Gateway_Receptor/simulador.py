"""
Simulador de nodos LoRa -> MQTT.

Genera tramas con el MISMO formato que transmiten los ESP32, las procesa con
el mismo codigo del gateway (protocolo.py) y las publica en el broker en
sensores/<node>, igual que lora.py. Sirve para probar el lado consumidor
(dashboard, base de datos, alarmas) sin hardware.

Ejemplos:
    .venv/bin/python simulador.py                      # 3 nodos, cada 10 s
    .venv/bin/python simulador.py --nodos sim1 sim2 --intervalo 5
    .venv/bin/python simulador.py --cantidad 20        # 20 rondas y termina
    .venv/bin/python simulador.py --evento-gas 0.05    # 5% de fugas de gas
    .venv/bin/python simulador.py --sin-mqtt           # solo imprime, no publica

Los mensajes llevan "simulado": true para distinguirlos de los reales.
"""
import argparse
import json
import os
import random
import time
from pathlib import Path

from mqtt_gateway import PublicadorMQTT, id_gateway
from protocolo import (
    armar_payload,
    cargar_env,
    extraer_campos,
    parsear_mensaje,
    topic_seguro,
)

cargar_env(Path(__file__).resolve().parent / ".env")


def acotar(v, lo, hi):
    return max(lo, min(hi, v))


class NodoSimulado:
    """Estado de un nodo: los valores derivan suavemente entre lecturas."""

    def __init__(self, nombre: str, prob_falla: float, prob_evento: float):
        self.nombre = nombre
        self.prob_falla = prob_falla
        self.prob_evento = prob_evento
        self.seq = 0
        self.arranque = time.time() - random.randint(0, 3600)
        # Cada nodo con su propia "ubicacion": distinta distancia al gateway
        # y distinto ambiente de base.
        self.rssi_base = random.uniform(-115, -75)
        self.temp = random.uniform(18, 28)
        self.hum = random.uniform(45, 75)
        self.o2 = 20.9
        self.co = 0.0
        self.h2s = 0.0
        self.ch4 = 0.0
        self.evento_restante = 0

    def _avanzar(self):
        self.temp = acotar(self.temp + random.gauss(0, 0.15), -10, 50)
        self.hum = acotar(self.hum + random.gauss(0, 0.5), 5, 99)

        # Inicio de un evento de gas (fuga) que dura varias lecturas.
        if self.evento_restante == 0 and random.random() < self.prob_evento:
            self.evento_restante = random.randint(3, 8)

        if self.evento_restante > 0:
            self.evento_restante -= 1
            self.co = acotar(self.co + random.uniform(5, 25), 0, 500)
            self.h2s = acotar(self.h2s + random.uniform(1, 5), 0, 100)
            self.ch4 = acotar(self.ch4 + random.uniform(2, 10), 0, 100)
            self.o2 = acotar(self.o2 - random.uniform(0.1, 0.5), 15, 21)
        else:
            # Vuelta gradual a valores normales.
            self.co = max(0.0, self.co * 0.6 + random.choice([0, 0, 0, 1]))
            self.h2s = max(0.0, self.h2s * 0.6)
            self.ch4 = max(0.0, self.ch4 * 0.6)
            self.o2 = acotar(self.o2 + (20.9 - self.o2) * 0.4
                             + random.gauss(0, 0.05), 15, 21)

    def trama(self) -> str:
        """Trama ASCII identica a la que arma transmitir() en el ESP32."""
        self._avanzar()
        up = int(time.time() - self.arranque)
        t = f"node={self.nombre};seq={self.seq};up={up}"

        if random.random() < self.prob_falla:
            t += ";gas=0"
        else:
            # Mismos formatos que el firmware: O2/H2S %.1f, CO/CH4 enteros.
            t += (f";gas=1;O2={self.o2:.1f};CO={int(self.co)}"
                  f";H2S={self.h2s:.1f};CH4={int(self.ch4)}")

        if random.random() < self.prob_falla:
            t += ";dht=0"
        else:
            t += f";dht=1;T={self.temp:.1f};H={self.hum:.1f}"

        self.seq += 1
        return t

    def enlace(self):
        """RSSI/SNR simulados para la calidad de enlace."""
        rssi = self.rssi_base + random.gauss(0, 2)
        # Con SF9 el SX1262 decodifica hasta ~-12.5 dB de SNR.
        snr = acotar((rssi + 120) / 4 + random.gauss(0, 1), -12.5, 12)
        return round(rssi, 1), round(snr, 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodos", nargs="+", default=["sim1", "sim2", "sim3"],
                    help="nombres de los nodos simulados")
    ap.add_argument("--intervalo", type=float, default=10,
                    help="segundos entre rondas (el ESP32 usa 10)")
    ap.add_argument("--cantidad", type=int, default=0,
                    help="rondas a enviar (0 = infinito)")
    ap.add_argument("--falla-sensor", type=float, default=0.05,
                    help="probabilidad de que un sensor no responda")
    ap.add_argument("--evento-gas", type=float, default=0.02,
                    help="probabilidad por lectura de iniciar una fuga de gas")
    ap.add_argument("--perdida", type=float, default=0.03,
                    help="probabilidad de que un paquete se pierda en el aire")
    ap.add_argument("--sin-mqtt", action="store_true",
                    help="no publicar, solo mostrar lo que se enviaria")
    args = ap.parse_args()

    base = os.getenv("MQTT_TOPIC_BASE", "sensores")
    gateway_id = id_gateway()
    nodos = [NodoSimulado(n, args.falla_sensor, args.evento_gas)
             for n in args.nodos]
    cliente = None
    if not args.sin_mqtt:
        # client_id distinto al del gateway real para no desconectarlo, y
        # cola propia en disco.
        cliente = PublicadorMQTT.desde_entorno(
            client_id=f"sim-{gateway_id}-{os.getpid()}",
            db_path=Path(__file__).resolve().parent / ".simulador_cola.db",
            log_cada=60,
            status_topic="",
        )
        cliente.iniciar()

    print(f"Simulando {len(nodos)} nodos -> {base}/<node> "
          f"cada {args.intervalo}s (Ctrl+C para salir)")
    ronda = 0
    try:
        while args.cantidad == 0 or ronda < args.cantidad:
            ronda += 1
            for nodo in nodos:
                trama = nodo.trama()
                if random.random() < args.perdida:
                    print(f"[{nodo.nombre}] paquete perdido (seq salteada)")
                    continue

                # Mismo camino que lora.py: parsear -> campos -> payload.
                valores = parsear_mensaje(trama)
                rssi, snr = nodo.enlace()
                campos = extraer_campos(valores, rssi, snr)
                payload = armar_payload(valores["node"], gateway_id, campos)
                payload["simulado"] = True

                topic = f"{base}/{topic_seguro(valores['node'])}"
                print(f"{topic}  <- {trama}")
                if cliente:
                    cliente.publicar(topic, json.dumps(payload))
                else:
                    print(f"   {json.dumps(payload)}")

                # Separar los nodos dentro de la ronda, como en el aire.
                time.sleep(random.uniform(0.1, 0.5))
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nSimulacion detenida")
    finally:
        if cliente:
            cliente.cerrar()


if __name__ == "__main__":
    main()
