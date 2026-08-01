#include <Arduino.h>

// contact-node：ESP8266/ESP-01 + 干簧管 门窗传感器节点
// 功能规划见 docs/design.md：干簧管门控供电，开门上电即发 MQTT 后休眠

void setup() {
  Serial.begin(115200);
  Serial.println("contact-node boot");
}

void loop() {
}
