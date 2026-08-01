#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiManager.h>   // 配网门户：首次/配网失败时开热点 contact-node-setup
#include <PubSubClient.h>
#include <LittleFS.h>

// contact-node：ESP8266/ESP-01 + 干簧管 门窗传感器节点
// 开门（干簧管断开）→ 发布 contact/<chipid>/state {"state":"open"} → app 消费告警
// 设计依据：docs/design.md
//
// 可靠性设计（Issue #6）：
// - LWT：异常掉线 broker 自动广播 contact/<id>/status = offline（retain）
// - MQTT 断开期间的状态变化缓存到 LittleFS；重连后不立即补发——app 可能尚未
//   订阅，先发 contact/syncreq，等 app 广播 contact/sync 后再补发（标记 cached）
// 注：PubSubClient 发布仅支持 QoS 0，QoS 1 由 LWT(willQoS=1) 和缓存补发兜住。
//
// 其他：WiFiManager 自定义参数（MQTT 配置）不落盘，需自己存 LittleFS；
// 开机后 1.5s 内按住 FLASH 键 ≥300ms 清空全部配置并重开配网门户。

static const uint8_t PIN_REED = 5;   // NodeMCU D1 / GPIO5，干簧管接此脚与 GND 之间
static const uint8_t PIN_FLASH = 0;  // NodeMCU FLASH 键 / GPIO0，按住为 LOW
static const unsigned long DEBOUNCE_MS = 50;
static const unsigned long MQTT_RETRY_MS = 5000;
static const char* MQTT_CFG_PATH = "/mqtt.cfg";
static const char* EVENTS_PATH = "/events.log";
static const size_t EVENTS_MAX_BYTES = 400;  // 缓存上限，约 10 条事件
// 补发握手：板子发 contact/syncreq，app 广播 contact/sync，板子收到才补发
static const char* TOPIC_SYNC = "contact/sync";
static const char* TOPIC_SYNCREQ = "contact/syncreq";
static const unsigned long SYNCREQ_RETRY_MS = 30000;  // 没等到 sync 则重发请求

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

// MQTT 参数：默认值仅首次使用，配网后持久化到 LittleFS
char mqttHost[40] = "192.168.1.10";
char mqttPort[6] = "1883";
char mqttUser[24] = "";
char mqttPass[24] = "";

char nodeId[24];      // contact-<chipid>，每块板唯一
char topicState[40];  // contact/<chipid>/state
char topicStatus[40]; // contact/<chipid>/status

int lastStableState = -1;   // 去抖后的稳定状态：HIGH=关（闭合）/ LOW=开（断开）
int lastRawReading = -1;
unsigned long lastRawChangeMs = 0;
unsigned long lastMqttRetryMs = 0;
unsigned long lastSyncReqMs = 0;

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

void publishState(const char* state, bool cached = false) {
  char payload[100];
  snprintf(payload, sizeof(payload), "{\"node\":\"%s\",\"state\":\"%s\"%s}",
           nodeId, state, cached ? ",\"cached\":true" : "");
  mqtt.publish(topicState, payload, true);  // retain：app 重启也能拿到最新状态
  Serial.printf("pub %s %s\n", topicState, payload);
}

// 断线期间的事件写入 LittleFS 缓存
void cacheEvent(const char* state) {
  File f = LittleFS.open(EVENTS_PATH, "r");
  size_t sz = f ? f.size() : 0;
  if (f) f.close();
  if (sz > EVENTS_MAX_BYTES) {
    Serial.println("event cache full, drop");
    return;
  }
  f = LittleFS.open(EVENTS_PATH, "a");
  if (f) {
    f.println(state);
    f.close();
    Serial.printf("cached event: %s\n", state);
  }
}

// 重连成功后补发缓存事件并清空
void flushEvents() {
  File f = LittleFS.open(EVENTS_PATH, "r");
  if (!f) return;
  while (f.available()) {
    String s = f.readStringUntil('\n');
    s.trim();
    if (s == "open" || s == "closed") {
      publishState(s.c_str(), true);
    }
  }
  f.close();
  LittleFS.remove(EVENTS_PATH);
  Serial.println("cached events flushed");
}

bool eventsPending() {
  File f = LittleFS.open(EVENTS_PATH, "r");
  if (!f) return false;
  size_t sz = f.size();
  f.close();
  return sz > 0;
}

void requestSync() {
  mqtt.publish(TOPIC_SYNCREQ, nodeId);
  lastSyncReqMs = millis();
  Serial.println("sync requested");
}

void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  // app 就绪广播：有缓存事件才补发
  if (strcmp(topic, TOPIC_SYNC) == 0 && eventsPending()) {
    flushEvents();
  }
}

bool mqttConnect() {
  // clientId 用 nodeId；LWT：异常掉线时 broker 发布 status=offline（QoS1 retain）
  if (mqtt.connect(nodeId, mqttUser, mqttPass, topicStatus, 1, true, "offline")) {
    Serial.println("mqtt connected");
    mqtt.publish(topicStatus, "online", true);  // 上线标记
    // 上线即上报当前状态，保证 broker 里 retain 的是最新值
    publishState(digitalRead(PIN_REED) == HIGH ? "closed" : "open");
    // 有缓存事件：订阅 app 就绪广播并请求同步，收到 contact/sync 才补发
    mqtt.subscribe(TOPIC_SYNC);
    if (eventsPending()) requestSync();
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
  snprintf(topicState, sizeof(topicState), "contact/%s/state", nodeId + 8);
  snprintf(topicStatus, sizeof(topicStatus), "contact/%s/status", nodeId + 8);
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
  mqtt.setCallback(onMqttMessage);
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
    // 缓存事件未补发（sync 未到达/丢失）：周期性重发请求
    if (eventsPending() && millis() - lastSyncReqMs > SYNCREQ_RETRY_MS) {
      requestSync();
    }
  }

  // 干簧管去抖：电平稳定 DEBOUNCE_MS 后才认为状态变化
  int raw = digitalRead(PIN_REED);
  if (raw != lastRawReading) {
    lastRawReading = raw;
    lastRawChangeMs = millis();
  }
  if (lastRawReading != lastStableState && millis() - lastRawChangeMs > DEBOUNCE_MS) {
    lastStableState = lastRawReading;
    const char* state = lastStableState == HIGH ? "closed" : "open";
    if (mqtt.connected()) {
      publishState(state);
    } else {
      cacheEvent(state);  // 断线：缓存，重连后补发
    }
  }
}
