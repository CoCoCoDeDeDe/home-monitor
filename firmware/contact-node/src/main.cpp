#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>      // MQTT host 主机名解析：手写最小 mDNS A 查询（LEAmDNS 无主机查询 API）
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
// host 可填 IP 或 mDNS 主机名（<服务器主机名>.local，换网络免 IP 重配）
char mqttHost[40] = "home-monitor-server.local";
char mqttPort[6] = "1883";
char mqttUser[24] = "";
char mqttPass[24] = "";

char mqttTarget[40];    // 实际连接目标：mDNS 解析出的 IP 或原样 host
int resolveFailCount = 0;  // 主机名模式连续连接失败计数，触发重新解析

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

// ---- 最小 mDNS A 记录查询（组播 224.0.0.251:5353，DNS 报文格式） ----
// 跳过 DNS 名字段（标签序列或压缩指针），返回名字结束后的偏移
int mdnsSkipName(const uint8_t* b, int len, int i) {
  while (i < len) {
    uint8_t c = b[i];
    if (c == 0) return i + 1;
    if ((c & 0xC0) == 0xC0) return i + 2;  // 压缩指针占 2 字节
    i += c + 1;
  }
  return len;
}

// 比较 offset 处的 DNS 名（可为标签序列/压缩指针）与我们的 qname 是否一致（不区分大小写）
bool mdnsNameEq(const uint8_t* b, int len, int i, const uint8_t* qname, int qlen) {
  int qi = 0, hops = 0;
  while (i < len && qi < qlen && hops++ < 8) {
    uint8_t c = b[i];
    if ((c & 0xC0) == 0xC0) {  // 压缩指针：跳到目标继续比
      if (i + 1 >= len) return false;
      i = ((c & 0x3F) << 8) | b[i + 1];
      continue;
    }
    if (c != qname[qi]) return false;  // 标签长度必须一致（0 即双方同时结束）
    for (int k = 1; k <= c; k++) {
      if (i + k >= len || qi + k >= qlen) return false;
      if (tolower(b[i + k]) != tolower(qname[qi + k])) return false;
    }
    if (c == 0) return true;
    i += c + 1;
    qi += c + 1;
  }
  return false;
}

// 从 mDNS 应答里找 NAME 与查询匹配的 A 记录（TYPE=1, RDLEN=4）。
// 组播应答常不带 question 段，所以校验 answer 的名字，过滤网络上无关 mDNS 报文
IPAddress mdnsParseA(const uint8_t* b, int len, const uint8_t* qname, int qlen) {
  if (len < 12 || !(b[2] & 0x80)) return INADDR_NONE;  // 必须是应答报文
  uint16_t qd = (b[4] << 8) | b[5], an = (b[6] << 8) | b[7];
  int i = 12;
  for (int q = 0; q < qd && i < len; q++) {
    i = mdnsSkipName(b, len, i) + 4;  // 名字 + QTYPE/QCLASS
  }
  for (int a = 0; a < an && i < len; a++) {
    bool mine = mdnsNameEq(b, len, i, qname, qlen);
    i = mdnsSkipName(b, len, i);
    if (i + 10 > len) break;
    uint16_t type = (b[i] << 8) | b[i + 1];
    uint16_t rdlen = (b[i + 8] << 8) | b[i + 9];
    i += 10;  // TYPE/CLASS/TTL/RDLENGTH
    if (i + rdlen > len) break;
    if (mine && type == 1 && rdlen == 4) return IPAddress(b[i], b[i + 1], b[i + 2], b[i + 3]);
    i += rdlen;
  }
  return INADDR_NONE;
}

// 组播查询 hostname（不带 .local 后缀）的 A 记录，超时返回 INADDR_NONE
IPAddress mdnsQueryA(const char* hostname, uint32_t timeoutMs) {
  uint8_t pkt[128];
  memset(pkt, 0, 12);
  pkt[5] = 1;  // QDCOUNT=1
  size_t n = 12;
  const char* p = hostname;
  while (*p) {  // 按 '.' 切 label 写入 QNAME
    const char* next = strchr(p, '.');
    size_t l = next ? (size_t)(next - p) : strlen(p);
    if (l == 0 || l > 63 || n + l + 6 >= sizeof(pkt)) return INADDR_NONE;
    pkt[n++] = (uint8_t)l;
    memcpy(pkt + n, p, l);
    n += l;
    if (!next) break;
    p = next + 1;
  }
  memcpy(pkt + n, "\x05local", 6);  // mDNS 名字必须带 local 后缀
  n += 6;
  pkt[n++] = 0;
  pkt[n++] = 0; pkt[n++] = 1;  // QTYPE=A
  pkt[n++] = 0; pkt[n++] = 1;  // QCLASS=IN
  const uint8_t* qname = pkt + 12;  // label 序列（含结尾 0），用于校验应答归属
  int qlen = (int)n - 12 - 4;

  WiFiUDP udp;
  IPAddress multicast(224, 0, 0, 251);
  if (!udp.beginMulticast(WiFi.localIP(), multicast, 5353)) return INADDR_NONE;

  // 组播无线投递不可靠（无 ACK、可能被丢弃）：查询最多发 3 次，分段等待
  for (int attempt = 0; attempt < 3; attempt++) {
    udp.beginPacketMulticast(multicast, 5353, WiFi.localIP());
    udp.write(pkt, n);
    udp.endPacket();
    unsigned long start = millis();
    while (millis() - start < timeoutMs / 3) {
      int sz = udp.parsePacket();
      if (sz >= 12) {
        uint8_t buf[512];
        int r = udp.read(buf, sz > (int)sizeof(buf) ? (int)sizeof(buf) : sz);
        IPAddress ip = mdnsParseA(buf, r, qname, qlen);
        if (ip.isSet()) { udp.stop(); return ip; }
      }
      delay(10);
    }
  }
  udp.stop();
  return INADDR_NONE;
}

// MQTT host 解析：IP 原样用；主机名走 mDNS 查询，失败回退原样（交系统 DNS）
bool resolveMqttHost() {
  IPAddress ip;
  if (ip.fromString(mqttHost)) {
    strncpy(mqttTarget, mqttHost, sizeof(mqttTarget) - 1);
    return true;
  }
  String h = mqttHost;
  if (h.endsWith(".local")) h.remove(h.length() - 6);
  IPAddress rip = mdnsQueryA(h.c_str(), 2000);
  if (rip.isSet()) {
    snprintf(mqttTarget, sizeof(mqttTarget), "%s", rip.toString().c_str());
    Serial.printf("mdns %s -> %s\n", mqttHost, mqttTarget);
    return true;
  }
  strncpy(mqttTarget, mqttHost, sizeof(mqttTarget) - 1);
  Serial.printf("mdns resolve %s failed, use as-is\n", mqttHost);
  return false;
}

bool mqttConnect() {
  // clientId 用 nodeId；LWT：异常掉线时 broker 发布 status=offline（QoS1 retain）
  if (mqtt.connect(nodeId, mqttUser, mqttPass, topicStatus, 1, true, "offline")) {
    resolveFailCount = 0;
    Serial.println("mqtt connected");
    mqtt.publish(topicStatus, "online", true);  // 上线标记
    // 上线即上报当前状态，保证 broker 里 retain 的是最新值
    publishState(digitalRead(PIN_REED) == HIGH ? "closed" : "open");
    // 有缓存事件：订阅 app 就绪广播并请求同步，收到 contact/sync 才补发
    mqtt.subscribe(TOPIC_SYNC);
    if (eventsPending()) requestSync();
    return true;
  }
  Serial.printf("mqtt connect failed rc=%d (target %s:%s)\n", mqtt.state(), mqttTarget, mqttPort);
  // 主机名模式持续失败（>30s）：重新解析，服务器 IP 可能已变（换网络环境）
  IPAddress tmp;
  if (!tmp.fromString(mqttHost) && ++resolveFailCount >= 6) {
    resolveFailCount = 0;
    resolveMqttHost();
    mqtt.setServer(mqttTarget, atoi(mqttPort));
  }
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

  // host 是主机名则先 mDNS 解析出 IP 再连
  resolveMqttHost();
  mqtt.setServer(mqttTarget, atoi(mqttPort));
  mqtt.setCallback(onMqttMessage);
  Serial.printf("wifi ok, mqtt -> %s:%s\n", mqttTarget, mqttPort);
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
