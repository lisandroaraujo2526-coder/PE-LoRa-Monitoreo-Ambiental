#include <RadioLib.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <SPI.h>

// ========================= CONFIG =========================

const char* NODO = "tx1";

const uint8_t PIN_RX_SENSOR = 47; // YYS SC401
const uint8_t PIN_THS01 = 4;
const uint8_t PIN_VEXT = 36;

// OLED Heltec V3
const uint8_t PIN_OLED_SDA = 17;
const uint8_t PIN_OLED_SCL = 18;
const uint8_t PIN_OLED_RST = 21;

const uint32_t TX_PERIODICO = 10000;
const uint32_t TIMEOUT_GASES = 5000;
const uint32_t TIMEOUT_DHT = 15000;
const uint32_t DHT_INTERVALO = 3000;
const uint32_t REFRESCO_OLED = 500;

// ========================= DISPOSITIVOS ===================

SX1262 radio = new Module(8, 14, 12, 13);

U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(
  U8G2_R0,
  PIN_OLED_RST,
  PIN_OLED_SCL,
  PIN_OLED_SDA
);

// ========================= VARIABLES ======================

const uint8_t TRAMA_LEN = 12;
uint8_t buf[TRAMA_LEN];
uint8_t idx = 0;

struct Gases {
  float o2, h2s;
  int co, ch4;
  uint32_t t_ms;
  bool valida;
};

Gases G = {0, 0, 0, 0, 0, false};

struct Ambiente {
  float temp, hum;
  uint32_t t_ms;
  bool valida;
};

Ambiente A = {0, 0, 0, false};

uint32_t seq = 0;
uint32_t tx_ok = 0;
uint32_t tx_err = 0;

bool ultimo_tx_ok = false;

uint32_t t_ultimo_tx = 0;
uint32_t t_ultimo_dht = 0;
uint32_t t_ultimo_oled = 0;

// ========================= ESTADO LORA ====================

// RF = Radio lista
// TX = Transmitiendo
// OK = Transmision local completada
// ER = Error de transmision

enum EstadoRadio {
  RF_LISTO,
  RF_TRANSMITIENDO,
  RF_TX_OK,
  RF_TX_ERROR
};

EstadoRadio estadoRadio = RF_LISTO;

uint32_t t_fin_tx = 0;
const uint32_t TIEMPO_MOSTRAR_OK = 2500;

// Prototipos
bool gasesVivo();
bool dhtVivo();

// ========================= OLED ===========================

void encenderOLED() {

  pinMode(PIN_VEXT, OUTPUT);
  digitalWrite(PIN_VEXT, LOW);
  delay(50);

  pinMode(PIN_OLED_RST, OUTPUT);
  digitalWrite(PIN_OLED_RST, LOW);
  delay(20);
  digitalWrite(PIN_OLED_RST, HIGH);
  delay(20);

  oled.begin();
  oled.setBusClock(400000);
  oled.setFont(u8g2_font_6x10_tf);
}

// ========================= PANTALLA ARRANQUE ===============

void pantallaArranque() {

  oled.clearBuffer();
  oled.setFont(u8g2_font_6x10_tf);

  oled.drawStr(0, 10, "Heltec V3 encendido");
  oled.drawStr(0, 22, "OLED : OK");

  char l[24];

  snprintf(l, sizeof(l), "Nodo : %s", NODO);
  oled.drawStr(0, 34, l);

  oled.drawStr(0, 58, "Esperando sensores..");

  oled.sendBuffer();
}

// ========================= CELDAS OLED ====================

void celda(
  uint8_t x,
  uint8_t yTop,
  const char* etiqueta,
  const char* valor
) {

  oled.setFont(u8g2_font_5x8_tf);
  oled.drawStr(x, yTop + 8, etiqueta);

  oled.setFont(u8g2_font_7x13B_tf);
  oled.drawStr(x, yTop + 19, valor);
}

// ========================= PANTALLA PRINCIPAL ==============

void mostrarOLED() {

  oled.clearBuffer();

  const uint8_t X1 = 2;
  const uint8_t X2 = 67;

  const uint8_t F1 = 0;
  const uint8_t F2 = 22;
  const uint8_t F3 = 43;

  char v[12];

  // Cuadricula
  oled.drawVLine(63, 0, 64);
  oled.drawHLine(0, 21, 128);
  oled.drawHLine(0, 42, 128);

  bool g = gasesVivo();
  bool a = dhtVivo();

  // OXIGENO

  if (g)
    snprintf(v, sizeof(v), "%.1f", G.o2);
  else
    snprintf(v, sizeof(v), "--");

  celda(X1, F1, "O2 %vol", v);

  // METANO

  if (g)
    snprintf(v, sizeof(v), "%d", G.ch4);
  else
    snprintf(v, sizeof(v), "--");

  celda(X2, F1, "CH4 %LEL", v);

  // MONOXIDO DE CARBONO

  if (g)
    snprintf(v, sizeof(v), "%d", G.co);
  else
    snprintf(v, sizeof(v), "--");

  celda(X1, F2, "CO ppm", v);

  // SULFURO DE HIDROGENO

  if (g)
    snprintf(v, sizeof(v), "%.1f", G.h2s);
  else
    snprintf(v, sizeof(v), "--");

  celda(X2, F2, "H2S ppm", v);

  // TEMPERATURA

  if (a)
    snprintf(v, sizeof(v), "%.1f", A.temp);
  else
    snprintf(v, sizeof(v), "--");

  celda(X1, F3, "Temp C", v);

  // HUMEDAD

  if (a)
    snprintf(v, sizeof(v), "%.1f", A.hum);
  else
    snprintf(v, sizeof(v), "--");

  celda(X2, F3, "Hum %", v);

  // ================= INDICADOR LORA =================

  const char* estadoTexto = "RF";

  if (estadoRadio == RF_TRANSMITIENDO)
    estadoTexto = "TX";

  else if (estadoRadio == RF_TX_OK)
    estadoTexto = "OK";

  else if (estadoRadio == RF_TX_ERROR)
    estadoTexto = "ER";

  // Recuadro inferior derecho
  oled.drawRFrame(105, 53, 22, 11, 2);

  oled.setFont(u8g2_font_5x8_tf);
  oled.drawStr(111, 61, estadoTexto);

  oled.sendBuffer();
}

// ========================= SETUP ==========================

void setup() {

  Serial.begin(115200);
  delay(1000);

  Serial.printf("\n=== Nodo %s ===\n", NODO);

  // OLED
  encenderOLED();

  oled.clearBuffer();
  oled.drawStr(0, 10, "Heltec V3 encendido");
  oled.drawStr(0, 22, "Iniciando...");
  oled.sendBuffer();

  // Sensor YYS SC401
  Serial1.begin(
    9600,
    SERIAL_8N1,
    PIN_RX_SENSOR,
    -1
  );

  // Sensor Sonoff THS01
  pinMode(PIN_THS01, INPUT_PULLUP);

  // ========================= LORA =========================

  SPI.begin(9, 11, 10, 8);

  radio.tcxoVoltage =
    RadioLibTCXOVoltage_t::Voltage1V8;

  ConfigLoRa_t config;

  config.frequency =
    RADIOLIB_UNIT_MEGA(915);

  config.bandwidth =
    RADIOLIB_UNIT_KILO(125);

  config.spreadingFactor = 9;
  config.codingRate = 7;
  config.syncWord = 0x34;
  config.power = 22;
  config.preambleLength = 8;

  int st = radio.begin(config);

  if (st != RADIOLIB_ERR_NONE) {

    Serial.printf("ERROR radio: %d\n", st);

    oled.clearBuffer();
    oled.drawStr(0, 10, "ERROR radio");

    char e[24];

    snprintf(e, sizeof(e), "code: %d", st);

    oled.drawStr(0, 22, e);
    oled.sendBuffer();

    while (true)
      delay(1000);
  }

  radio.setDio2AsRfSwitch(true);
  radio.sleep();

  estadoRadio = RF_LISTO;

  Serial.println("SX1262 OK - LoRa SF9");

  pantallaArranque();
  delay(2500);
}

// ========================= YYS SC401 ======================

bool checksumOk(uint8_t* t) {

  uint16_t suma = 0;

  for (int i = 0; i <= 10; i++)
    suma += t[i];

  return (suma & 0xFF) == t[11];
}

void leerGases() {

  while (Serial1.available()) {

    uint8_t b = Serial1.read();

    if (idx == 0) {
      if (b != 0x3C)
        continue;
    }

    else if (idx == 1) {
      if (b != 0x04) {
        idx = 0;
        continue;
      }
    }

    buf[idx++] = b;

    if (idx < TRAMA_LEN)
      continue;

    idx = 0;

    if (!checksumOk(buf))
      continue;

    G.o2 =
      ((buf[2] << 8) | buf[3]) / 10.0;

    G.co =
      (buf[4] << 8) | buf[5];

    G.h2s =
      ((buf[6] << 8) | buf[7]) / 10.0;

    G.ch4 =
      (buf[8] << 8) | buf[9];

    G.t_ms = millis();
    G.valida = true;

    Serial.printf(
      "GAS O2=%.1f CO=%d H2S=%.1f CH4=%d\n",
      G.o2,
      G.co,
      G.h2s,
      G.ch4
    );
  }
}

bool gasesVivo() {

  return G.valida &&
    (millis() - G.t_ms < TIMEOUT_GASES);
}

// ========================= THS01 ==========================

uint32_t medir(int nivel, uint32_t timeout) {

  uint32_t t = micros();

  while (digitalRead(PIN_THS01) == nivel) {

    if (micros() - t > timeout)
      return 0;
  }

  return micros() - t;
}

// ========================= LECTURA THS01 ==================

int leerTHS01Crudo(uint8_t* d) {

  memset(d, 0, 5);

  pinMode(PIN_THS01, OUTPUT);
  digitalWrite(PIN_THS01, LOW);

  delayMicroseconds(500);

  pinMode(PIN_THS01, INPUT_PULLUP);

  int err = 0;

  noInterrupts();

  if (medir(HIGH, 200) == 0)
    err = 1;

  else if (medir(LOW, 150) == 0)
    err = 1;

  else if (medir(HIGH, 150) == 0)
    err = 1;

  else {

    for (int i = 0; i < 40; i++) {

      uint32_t bajo = medir(LOW, 120);
      uint32_t alto = medir(HIGH, 150);

      if (bajo == 0 || alto == 0) {
        err = 2;
        break;
      }

      d[i / 8] <<= 1;

      if (alto > bajo)
        d[i / 8] |= 1;
    }
  }

  interrupts();

  if (
    err == 0 &&
    ((d[0] + d[1] + d[2] + d[3]) & 0xFF) != d[4]
  ) {
    err = 3;
  }

  return err;
}

// ========================= TEMPERATURA Y HUMEDAD ===========

void leerDHT() {

  if (millis() - t_ultimo_dht < DHT_INTERVALO)
    return;

  t_ultimo_dht = millis();

  uint8_t d[5];

  int err = leerTHS01Crudo(d);

  if (err != 0)
    return;

  float h =
    ((d[0] << 8) | d[1]) / 10.0;

  float t =
    (((d[2] & 0x7F) << 8) | d[3]) / 10.0;

  if (d[2] & 0x80)
    t = -t;

  if (
    h < 0 || h > 100 ||
    t < -40 || t > 85
  ) {
    return;
  }

  A.temp = t;
  A.hum = h;
  A.t_ms = millis();
  A.valida = true;
}

bool dhtVivo() {

  return A.valida &&
    (millis() - A.t_ms < TIMEOUT_DHT);
}

// ========================= TRANSMISION LORA ================

void transmitir() {

  char trama[128];
  int n = 0;

  n += snprintf(
    trama + n,
    sizeof(trama) - n,
    "node=%s;seq=%lu;up=%lu",
    NODO,
    seq,
    millis() / 1000
  );

  if (gasesVivo()) {

    n += snprintf(
      trama + n,
      sizeof(trama) - n,
      ";gas=1;O2=%.1f;CO=%d;H2S=%.1f;CH4=%d",
      G.o2,
      G.co,
      G.h2s,
      G.ch4
    );
  }

  else {

    n += snprintf(
      trama + n,
      sizeof(trama) - n,
      ";gas=0"
    );
  }

  if (dhtVivo()) {

    n += snprintf(
      trama + n,
      sizeof(trama) - n,
      ";dht=1;T=%.1f;H=%.1f",
      A.temp,
      A.hum
    );
  }

  else {

    n += snprintf(
      trama + n,
      sizeof(trama) - n,
      ";dht=0"
    );
  }

  // Mostrar TX mientras se transmite
  estadoRadio = RF_TRANSMITIENDO;
  mostrarOLED();

  // Transmision
  int st = radio.transmit(trama);

  radio.sleep();

  ultimo_tx_ok = (st == RADIOLIB_ERR_NONE);

  if (ultimo_tx_ok)
    estadoRadio = RF_TX_OK;
  else
    estadoRadio = RF_TX_ERROR;

  t_fin_tx = millis();

  // Actualizar el estado inmediatamente
  mostrarOLED();

  Serial.printf(
    "TX %s (codigo %d) -> %s\n",
    ultimo_tx_ok ? "OK " : "ERR",
    st,
    trama
  );

  if (ultimo_tx_ok) {
    tx_ok++;
    seq++;
  }

  else {
    tx_err++;
  }

  t_ultimo_tx = millis();
}

// ========================= LOOP ===========================

void loop() {

  leerGases();
  leerDHT();

  // Volver a RF luego de mostrar OK
  if (
    estadoRadio == RF_TX_OK &&
    millis() - t_fin_tx >= TIEMPO_MOSTRAR_OK
  ) {
    estadoRadio = RF_LISTO;
  }

  // Actualizar OLED
  if (
    millis() - t_ultimo_oled >= REFRESCO_OLED
  ) {
    t_ultimo_oled = millis();
    mostrarOLED();
  }

  // Transmitir cada 10 segundos
  if (
    millis() - t_ultimo_tx >= TX_PERIODICO
  ) {
    transmitir();
  }
}
