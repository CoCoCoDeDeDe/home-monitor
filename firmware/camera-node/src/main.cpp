#include <Arduino.h>

// camera-node：ESP32-S3 + OV2640 摄像头节点
// 功能规划见 docs/design.md：MJPEG 推流、SD 卡分段录像、边缘帧差移动侦测

void setup() {
  Serial.begin(115200);
  Serial.println("camera-node boot");
}

void loop() {
}
