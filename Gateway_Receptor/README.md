# Gateway receptor LoRa–MQTT

Gateway Python para Raspberry Pi con Waveshare SX1262 LoRa HAT. Recibe tramas ASCII LoRa de los nodos, las valida y las convierte a JSON; el envío MQTT sobre WebSockets seguros usa una cola SQLite persistente para que una interrupción de red no bloquee la recepción por radio.

## Archivos

- `lora.py`: recepción LoRa y publicación en cola MQTT.
- `protocolo.py`: parseo y normalización de tramas del nodo.
- `mqtt_gateway.py`: conexión MQTT v5/WSS, cola SQLite y estado del gateway.
- `simulador.py`: generación de nodos de prueba sin hardware.

## Requisitos

- Raspberry Pi OS de 64 bits, Python 3.10 o posterior y SPI habilitado.
- Waveshare SX1262 LoRa HAT conectado a SPI0: CS0, RESET GPIO 18, BUSY GPIO 20 y TXEN GPIO 6.
- Biblioteca Python [LoRaRF](https://github.com/chandrawi/LoRaRF-Python) para el HAT.
- Parámetros de radio iguales a los del transmisor: 915 MHz, SF9, BW 125 kHz, CR 4/7 y palabra de sincronización `0x3444`.

## Instalación

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Instala `LoRaRF` según las instrucciones de su repositorio. Completa en `.env` el usuario y la contraseña MQTT entregados para el broker; no guardes credenciales reales en Git.

## Ejecución

```sh
.venv/bin/python lora.py
```

Para probar el formato del protocolo sin publicar ni usar el HAT:

```sh
.venv/bin/python simulador.py --sin-mqtt --cantidad 2
```

Para publicar datos simulados en MQTT, configura las credenciales en `.env` y ejecuta `simulador.py`. El simulador usa un identificador de cliente y una cola SQLite propios.

El gateway publica lecturas en `sensores/<node>` y, por defecto, el estado retenido en `sensores/gateway/estado`. La cola local `mqtt_cola.db` y el archivo `.env` están excluidos de Git.
