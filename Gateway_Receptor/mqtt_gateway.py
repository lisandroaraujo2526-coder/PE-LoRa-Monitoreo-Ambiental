"""
Publicador MQTT robusto para el gateway (WSS + cola persistente).

Garantias:
- publicar() NUNCA bloquea ni lanza excepciones: solo guarda el mensaje en
  una cola SQLite en disco (~1 ms). El bucle LoRa no se ve afectado por el
  estado de la red o del broker.
- Un hilo propio envia la cola cuando hay conexion. Un mensaje se borra del
  disco recien cuando el broker confirma (PUBACK). Si se corta internet, se
  reinicia el servicio o se apaga la Raspberry, los datos pendientes se
  envian al volver.
- MQTT v5: el broker informa si rechaza un mensaje (p. ej. falta de permiso
  en el topic), cosa que en v3.1.1 se descarta en silencio.
- Reconexion automatica con backoff; las excepciones en callbacks no matan el
  hilo de red de paho.
- La cola tiene un tope (MQTT_COLA_MAX); si se llena se descartan los mas
  viejos para no llenar la tarjeta SD.
- Estado del gateway (opcional, status_topic): JSON retenido con
  "estado": "online" + estadisticas cada `estado_cada` segundos, y
  "estado": "offline" publicado por el broker (Last Will) si el gateway se
  cae o pierde la conexion sin despedirse.
"""
import json
import os
import queue
import socket
import sqlite3
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

_ACK = "ack"
_DESCONECTADO = "desc"


class PublicadorMQTT:
    def __init__(
        self,
        host: str,
        port: int,
        ws_path: str,
        usuario: str | None,
        clave: str | None,
        client_id: str,
        db_path: Path,
        status_topic: str = "",
        estado_base: dict | None = None,
        estado_fn=None,
        estado_cada: float = 60,
        keepalive: int = 30,
        max_inflight: int = 20,
        cola_max: int = 100_000,
        log_cada: float = 300,
        log=print,
    ):
        self.url = f"wss://{host}:{port}{ws_path}"
        self._host, self._port = host, port
        self._keepalive = keepalive
        self._status_topic = status_topic
        self._estado_base = {"tipo": "estado_gateway", **(estado_base or {})}
        self._estado_fn = estado_fn  # () -> dict con datos extra (radio, nodos)
        self._estado_cada = estado_cada
        self._estado_ya = threading.Event()
        self._max_inflight = max_inflight
        self._cola_max = cola_max
        self._log_cada = log_cada
        self._log = log

        self._db_path = str(db_path)
        self._db_lock = threading.Lock()  # solo para el hilo que llama publicar()
        self._db = self._abrir_db()

        self._conectado = threading.Event()
        self._hay_datos = threading.Event()
        self._parar = threading.Event()
        self._eventos: queue.Queue = queue.Queue()  # acks desde el hilo de paho

        self.enviados = 0
        self.rechazados = 0
        self.descartados = 0

        c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            transport="websockets",
            protocol=mqtt.MQTTv5,
        )
        c.ws_set_options(path=ws_path)
        c.tls_set()
        if usuario:
            c.username_pw_set(usuario, clave)
        if status_topic:
            # Si el gateway se cae, el broker publica "offline" solo.
            c.will_set(status_topic, self._json_estado("offline"),
                       qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        # El limite de mensajes en vuelo lo maneja nuestro hilo; paho sin cola.
        c.max_inflight_messages_set(max_inflight)
        c.suppress_exceptions = True
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_publish = self._on_publish
        self._client = c

        self._hilo = threading.Thread(target=self._bucle_envio,
                                      name="mqtt-envio", daemon=True)

    # ------------------------------------------------------------------ API
    @classmethod
    def desde_entorno(cls, client_id: str, db_path: Path, **kw):
        """Crea el publicador con la configuracion de las variables MQTT_*."""
        kw.setdefault("status_topic", os.getenv("MQTT_STATUS_TOPIC", ""))
        return cls(
            host=os.getenv("MQTT_HOST", "mqtt.ralcivardev.com"),
            port=int(os.getenv("MQTT_PORT", "443")),
            ws_path=os.getenv("MQTT_WS_PATH", "/mqtt"),
            usuario=os.getenv("MQTT_USER"),
            clave=os.getenv("MQTT_PASS") or os.getenv("MQTT_PAS"),
            client_id=client_id,
            db_path=db_path,
            cola_max=int(os.getenv("MQTT_COLA_MAX", "100000")),
            **kw,
        )

    def iniciar(self) -> None:
        pendientes = self.pendientes()
        if pendientes:
            self._log(f"MQTT: {pendientes} mensajes pendientes en disco, "
                      "se enviaran al conectar")
        # connect_async + loop_start: no bloquea y reintenta aunque no haya
        # red/DNS al arrancar.
        self._client.connect_async(self._host, self._port,
                                   keepalive=self._keepalive)
        self._client.loop_start()
        self._hilo.start()

    def publicar(self, topic: str, payload: str) -> bool:
        """Encola un mensaje (QoS 1). No bloquea; devuelve False si fallo."""
        try:
            with self._db_lock:
                self._db.execute(
                    "INSERT INTO cola (topic, payload, creado) VALUES (?,?,?)",
                    (topic, payload, time.time()),
                )
                self._db.commit()
            self._hay_datos.set()
            return True
        except Exception as error:
            self._log(f"MQTT: no se pudo encolar el mensaje: {error}")
            return False

    def pendientes(self) -> int:
        try:
            with self._db_lock:
                return self._db.execute("SELECT COUNT(*) FROM cola").fetchone()[0]
        except Exception:
            return -1

    @property
    def conectado(self) -> bool:
        return self._conectado.is_set()

    def cerrar(self, espera: float = 5) -> None:
        """Intenta vaciar la cola unos segundos y desconecta ordenadamente."""
        limite = time.time() + espera
        while self.conectado and self.pendientes() > 0 and time.time() < limite:
            time.sleep(0.2)
        self._parar.set()
        self._hay_datos.set()
        self._hilo.join(timeout=3)
        try:
            if self._status_topic and self.conectado:
                self._client.publish(self._status_topic,
                                     self._json_estado("offline"), qos=1,
                                     retain=True).wait_for_publish(timeout=2)
            self._client.disconnect()
            self._client.loop_stop()
        except Exception as error:
            self._log(f"MQTT: error al cerrar: {error}")
        restantes = self.pendientes()
        if restantes:
            self._log(f"MQTT: {restantes} mensajes quedan en disco para el "
                      "proximo arranque")
        self._db.close()

    # ---------------------------------------------------- callbacks de paho
    # Corren en el hilo de red de paho: no tocan la DB ni bloquean, solo
    # avisan al hilo de envio.
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._log(f"MQTT: conexion rechazada por el broker ({reason_code})")
            return
        self._log(f"MQTT: conectado a {self.url}")
        self._conectado.set()
        self._estado_ya.set()  # el hilo de envio publica "online" enseguida
        self._hay_datos.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._conectado.clear()
        self._eventos.put((_DESCONECTADO, None, None))
        if self._parar.is_set():
            self._log("MQTT: desconectado")
        else:
            self._log(f"MQTT: conexion perdida ({reason_code}), reintentando...")

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        self._eventos.put((_ACK, mid, reason_code))
        self._hay_datos.set()

    # ------------------------------------------------------- hilo de envio
    def _abrir_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")  # menos escrituras en la SD
        db.execute("""CREATE TABLE IF NOT EXISTS cola (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          topic TEXT NOT NULL,
                          payload TEXT NOT NULL,
                          creado REAL NOT NULL)""")
        db.commit()
        return db

    def _bucle_envio(self) -> None:
        db = self._abrir_db()  # conexion propia de este hilo
        en_vuelo: dict[int, int] = {}  # mid de paho -> id en la DB
        ultimo_log = ultimo_recorte = ultimo_estado = time.time()

        while not self._parar.is_set():
            try:
                self._hay_datos.wait(timeout=1)
                self._hay_datos.clear()

                self._procesar_eventos(db, en_vuelo)
                if time.time() - ultimo_recorte >= 60:
                    ultimo_recorte = time.time()
                    self._recortar_cola(db)

                if self.conectado and len(en_vuelo) < self._max_inflight:
                    self._enviar_lote(db, en_vuelo)

                if self._status_topic and self.conectado and (
                        self._estado_ya.is_set()
                        or time.time() - ultimo_estado >= self._estado_cada):
                    self._estado_ya.clear()
                    ultimo_estado = time.time()
                    self._publicar_estado(db)

                if time.time() - ultimo_log >= self._log_cada:
                    ultimo_log = time.time()
                    total = db.execute("SELECT COUNT(*) FROM cola").fetchone()[0]
                    self._log(
                        f"MQTT estado: {'conectado' if self.conectado else 'SIN CONEXION'}"
                        f" | enviados={self.enviados} pendientes={total}"
                        f" rechazados={self.rechazados} descartados={self.descartados}"
                    )
            except Exception as error:
                # Nunca dejar morir el hilo de envio.
                self._log(f"MQTT: error en hilo de envio: {error}")
                time.sleep(1)
        db.close()

    def _json_estado(self, estado: str, extra: dict | None = None) -> str:
        return json.dumps({**self._estado_base, "estado": estado,
                           "ts": int(time.time()), **(extra or {})})

    def _publicar_estado(self, db) -> None:
        extra = {}
        if self._estado_fn:
            try:
                extra = self._estado_fn()
            except Exception as error:
                self._log(f"MQTT: error armando estado: {error}")
        extra["mqtt"] = {
            "pendientes": db.execute("SELECT COUNT(*) FROM cola").fetchone()[0],
            "enviados": self.enviados,
            "rechazados": self.rechazados,
            "descartados": self.descartados,
        }
        info = self._client.publish(self._status_topic,
                                    self._json_estado("online", extra),
                                    qos=1, retain=True)
        self._mid_estado = info.mid

    def _procesar_eventos(self, db, en_vuelo) -> None:
        while True:
            try:
                tipo, mid, rc = self._eventos.get_nowait()
            except queue.Empty:
                return
            if tipo == _DESCONECTADO:
                # paho reenvia por su cuenta lo que tenia en vuelo al
                # reconectar (mismo mid), asi que el mapa se conserva.
                continue
            fila = en_vuelo.pop(mid, None)
            if fila is None:
                # Mensaje de estado (no esta en la cola de disco).
                if (mid == getattr(self, "_mid_estado", None)
                        and rc is not None and rc.is_failure):
                    self._log(f"MQTT: el broker rechazo el mensaje de estado "
                              f"({rc}); revisar MQTT_STATUS_TOPIC / ACL")
                continue
            if rc is not None and rc.is_failure:
                self.rechazados += 1
                self._log(f"MQTT: el broker rechazo un mensaje ({rc}); "
                          "revisar permisos (ACL) del topic")
            else:
                self.enviados += 1
            db.execute("DELETE FROM cola WHERE id=?", (fila,))
            db.commit()

    def _enviar_lote(self, db, en_vuelo) -> None:
        libres = self._max_inflight - len(en_vuelo)
        ids_en_vuelo = set(en_vuelo.values())
        filas = db.execute(
            "SELECT id, topic, payload FROM cola ORDER BY id LIMIT ?",
            (libres + len(ids_en_vuelo),),
        ).fetchall()
        for fila_id, topic, payload in filas:
            if libres <= 0 or not self.conectado:
                break
            if fila_id in ids_en_vuelo:
                continue
            info = self._client.publish(topic, payload, qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                # Se perdio la conexion justo ahora: paho lo guarda y lo
                # reenvia al reconectar, asi que igual se registra.
                if info.rc != mqtt.MQTT_ERR_NO_CONN:
                    self._log(f"MQTT: error al publicar: "
                              f"{mqtt.error_string(info.rc)}")
                    continue
            en_vuelo[info.mid] = fila_id
            libres -= 1

    def _recortar_cola(self, db) -> None:
        total = db.execute("SELECT COUNT(*) FROM cola").fetchone()[0]
        exceso = total - self._cola_max
        if exceso > 0:
            db.execute("DELETE FROM cola WHERE id IN "
                       "(SELECT id FROM cola ORDER BY id LIMIT ?)", (exceso,))
            db.commit()
            self.descartados += exceso
            self._log(f"MQTT: cola llena, se descartaron {exceso} mensajes viejos")


def id_gateway() -> str:
    return os.getenv("GATEWAY_ID", socket.gethostname())
