# home-monitor

DIY 家庭监控系统：多路无线摄像头 + 门窗开关传感器 + 人形识别告警 + 外网实时监看。

## 仓库结构

```
firmware/
  camera-node/    # ESP32-S3 + OV2640 摄像头节点（PlatformIO）
  contact-node/      # ESP8266/ESP-01 + 干簧管 门窗传感器节点（PlatformIO）
server/
  docker-compose.yml   # mosquitto + app + cloudflared
  app/            # FastAPI：MQTT 消费/流转发/检测/告警/Web
docs/
  design.md       # 系统设计方案
```

## 文档

- 设计方案：[docs/design.md](docs/design.md)
- 协作约定：[CONTRIBUTING.md](CONTRIBUTING.md)

## 开发环境

- 固件：VSCode + PlatformIO（Arduino framework）
- 服务端：Docker Compose（Python FastAPI）
