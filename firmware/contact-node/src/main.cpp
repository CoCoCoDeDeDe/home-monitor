#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiManager.h>   // 配网门户：首次/配网失败时开热点 contact-node-setup
#include <PubSubClient.h>
#include <LittleFS.h>

// contact-node：ESP8266/ESP-01 + 干簧管 门窗传感器节点
// 开门（干簧管断开）→ 发布 contact/<chipid>/state {"state":"open"} → app 消费告警
// 设计依据：docs/design.md
//
// 注意：WiFiManager 只自动持久化 WiFi 凭据，自定义参数（MQTT 配置）必须
// 自己存 LittleFS，否则重启后回退默认值。开机后 1.5s 内按住 FLASH 键可
// 清空全部配置并重新进入配网门户。

static const uint8_t PIN_REED = 5;   // NodeMCU D1 / GPIO5，干簧管接此脚与 GND 之间
static const uint8_t PIN_FLASH = 0;  // NodeMCU FLASH 键 / GPIO0，按住为 LOW
static const unsigned long DEBOUNCE_MS = 50;
static const unsigned long MQTT_RETRY_MS = 5000;
static const char* MQTT_CFG_PATH = "/mqtt.cfg";

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

// MQTT 参数：默认值仅首次使用，配网后持久化到 LittleFS
char mqttHost[40] = "192.168.1.10";
char mqttPort[6] = "1883";
char mqttUser[24] = "";
char mqttPass[24] = "";

char nodeId[24];  // contact-<chipid>，每块板唯一

int lastStableState = -1;   // 去抖后的稳定状态：HIGH=关（闭合）/ LOW=开（断开）
int lastRawReading = -1;
unsigned long lastRawChangeMs = 0;
unsigned long lastMqttRetryMs = 0;

void loadMqttConfig() {
  File f = LittleFS.open(MQTT_CFG_PATH, "r");
  if (!f) return;
  String h = f.readStringUntil('\n'); h.trim();
  String p = f.readStringUntil('\n'); p.trim();
  String u = f.readStringUntil('\n'); u.trim();
  String w = f.readStringUntil('\n'); w.trim();
  f.close();
  if (h.length()) strncpy(mqttHost, h.c_str(), sizeof(mqttHost) - 1);
  if (p.length()) strncpy(mqttPort, p.c_str(), sizeof(mqttPort) - 1);
  if (u.length()) strncpy(mqttUser, u.c_str(), sizeof(mqttUser) - 1);
  strncpy(mqttPass, w.c_str(), sizeof(mqttPass) - 1);  // 密码允许为空
  Serial.printf("mqtt cfg loaded: %s:%s\n", mqttHost, mqttPort);
}

void saveMqttConfig() {
  File f = LittleFS.open(MQTT_CFG_PATH, "w");
  if (!f) return;
  f.printf("%s\n%s\n%s\n%s\n", mqttHost, mqttPort, mqttUser, mqttPass);
  f.close();
  Serial.println("mqtt cfg saved");
}

void publishState(const char* state) {
  char topic[40];
  char payload[80];
  snprintf(topic, sizeof(topic), "contact/%s/state", nodeId + 8);  // 去掉 "contact-" 前缀
  snprintf(payload, sizeof(payload), "{\"node\":\"%s\",\"state\":\"%s\"}", nodeId, state);
  mqtt.publish(topic, payload, true);  // retain：app 重启也能拿到最新状态
  Serial.printf("pub %s %s\n", topic, payload);
}

bool mqttConnect() {
  // clientId 用 nodeId，重连时 broker 能识别同一设备
  if (mqtt.connect(nodeId, mqttUser, mqttPass)) {
    Serial.println("mqtt connected");
    // 上线即上报当前状态，保证 broker 里 retain 的是最新值
    publishState(digitalRead(PIN_REED) == HIGH ? "closed" : "open");
    return true;
  }
  Serial.printf("mqtt connect failed rc=%d (target %s:%s)\n", mqtt.state(), mqttHost, mqttPort);
  return false;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_REED, INPUT_PULLUP);
  pinMode(PIN_FLASH, INPUT_PULLUP);
  snprintf(nodeId, sizeof(nodeId), "contact-%06x", ESP.getChipId());
  Serial.printf("%s boot\n", nodeId);

  LittleFS.begin();
  loadMqttConfig();

  WiFiManager wm;
  WiFiManagerParameter pHost("mqtt", "MQTT host", mqttHost, sizeof(mqttHost));
  WiFiManagerParameter pPort("port", "MQTT port", mqttPort, sizeof(mqttPort));
  WiFiManagerParameter pUser("user", "MQTT user", mqttUser, sizeof(mqttUser));
  WiFiManagerParameter pPass("pass", "MQTT pass", mqttPass, sizeof(mqttPass));
  wm.addParameter(&pHost);
  wm.addParameter(&pPort);
  wm.addParameter(&pUser);
  wm.addParameter(&pPass);
  // 配网保存后把 MQTT 参数落盘
  wm.setSaveConfigCallback([&]() {
    strncpy(mqttHost, pHost.getValue(), sizeof(mqttHost) - 1);
    strncpy(mqttPort, pPort.getValue(), sizeof(mqttPort) - 1);
    strncpy(mqttUser, pUser.getValue(), sizeof(mqttUser) - 1);
    strncpy(mqttPass, pPass.getValue(), sizeof(mqttPass) - 1);
    saveMqttConfig();
  });

  // 开机后 1.5s 内按住 FLASH 键 ≥300ms：清空 WiFi 凭据 + MQTT 配置，强制重新配网
  // （不能在上电瞬间采样 GPIO0——ROM 会进下载模式，所以改成开机后窗口检测）
  Serial.println("hold FLASH within 1.5s to reset settings");
  unsigned long winStart = millis(), lowSince = 0;
  bool resetReq = false;
  while (millis() - winStart < 1500) {
    if (digitalRead(PIN_FLASH) == LOW) {
      if (lowSince == 0) lowSince = millis();
      if (millis() - lowSince > 300) { resetReq = true; break; }
    } else {
      lowSince = 0;
    }
    delay(20);
  }
  if (resetReq) {
    Serial.println("FLASH held: reset all settings");
    wm.resetSettings();
    LittleFS.remove(MQTT_CFG_PATH);
  }

  // 已存凭据则直连；否则开热点 contact-node-setup（连上后 192.168.4.1 配网）
  if (!wm.autoConnect("contact-node-setup")) {
    Serial.println("config portal timeout, rebooting");
    ESP.restart();
  }
  // autoConnect 返回后参数对象里是最新值（配网保存时回调也已落盘）
  strncpy(mqttHost, pHost.getValue(), sizeof(mqttHost) - 1);
  strncpy(mqttPort, pPort.getValue(), sizeof(mqttPort) - 1);
  strncpy(mqttUser, pUser.getValue(), sizeof(mqttUser) - 1);
  strncpy(mqttPass, pPass.getValue(), sizeof(mqttPass) - 1);

  mqtt.setServer(mqttHost, atoi(mqttPort));
  Serial.printf("wifi ok, mqtt -> %s:%s\n", mqttHost, mqttPort);
}

void loop() {
  if (!mqtt.connected()) {
    unsigned long now = millis();
    if (now - lastMqttRetryMs > MQTT_RETRY_MS) {
      lastMqttRetryMs = now;
      mqttConnect();
    }
  } else {
    mqtt.loop();
  }

  // 干簧管去抖：电平稳定 DEBOUNCE_MS 后才认为状态变化
  int raw = digitalRead(PIN_REED);
  if (raw != lastRawReading) {
    lastRawReading = raw;
    lastRawChangeMs = millis();
  }
  if (lastRawReading != lastStableState && millis() - lastRawChangeMs > DEBOUNCE_MS) {
    lastStableState = lastRawReading;
    if (mqtt.connected()) {
      publishState(lastStableState == HIGH ? "closed" : "open");
    }
  }
}
