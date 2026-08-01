# home-monitor 设计方案

DIY 家庭监控系统：多路无线摄像头 + 门窗开关传感器 + 人形识别告警 + 外网实时监看。

## 系统架构

```
【感知层 · 家中】                                【中心服务器 · 家中旧笔记本】
camera-node ×N                                  ┌─ Mosquitto (MQTT over TLS)
  ESP32-S3 + OV2640                             ├─ ingest     接收帧流/鉴权
  MJPEG 按需推流 ──────────┐                    ├─ detector   帧差移动侦测 → YOLOv8n 人形确认
                           │                    ├─ recorder   事件快照/短视频存储
door-node ×N               ├──局域网 WiFi─────▶ ├─ alerter    防抖聚合 → Server酱/企业微信
  ESP8266/ESP-01 + 干簧管   │  MQTT/HTTPS        ├─ web        实时监看 + 历史事件 + 布防撤防
  开门即唤醒上报 ──────────┘                    └─ cloudflared  Cloudflare Tunnel 出口
                                                【客户端】手机/电脑浏览器 → https://域名(Cloudflare)
```

所有服务跑在旧笔记本 WSL2 + Docker Compose 里，设备走局域网 WiFi 直连；外网访问经 **Cloudflare Tunnel**（免费、免备案、自带 HTTPS），无需公网 IP、无需 VPS。

## 组网设计

**单网段扁平组网，路由器零配置**——无端口转发、无 DDNS、无 VLAN，NAT 只走出站连接。

- **WiFi**：所有 ESP 节点连家里路由器的 **2.4GHz** WiFi（ESP8266/ESP32 不支持 5GHz；若路由器 2.4G/5G 合频 SSID 导致配网失败，需拆出独立 2.4G SSID），DHCP 自动分配 IP。
- **服务器地址固定**：路由器里给笔记本 MAC 做 DHCP 静态绑定（如 `192.168.1.10`），固件中 MQTT/HTTPS 地址统一指向它。
- **通信矩阵**：

| 链路 | 协议/端口 | 说明 |
|---|---|---|
| door-node → 笔记本 | MQTT `8883`（TLS + 账号密码） | 开门事件、心跳 |
| camera-node → 笔记本 | MQTT `8883` + HTTPS POST | 心跳/控制 + 事件帧、SD 分段上传 |
| 浏览器 → 笔记本（家中） | 局域网 HTTPS | 实时监看、历史回看 |
| 浏览器 → 笔记本（外网） | Cloudflare Tunnel | cloudflared 主动外连 443，无入站端口 |
| 笔记本 → Server酱 | HTTPS 外连 | 告警推送 |

- **断网降级**：宽带/Cloudflare 断了，局域网内监控、SD 卡录像、事件记录全部照常，仅微信推送和外网访问失效，恢复后补发缓存事件。

## 关键设计决策

- **中心服务器**：旧笔记本（房间常开）WSL2 + Docker Compose，¥0。
  - 电源设置为**合盖不睡眠、插电常开**；打游戏时服务照跑，人形识别限核/检测到游戏进程自动降级（SD 卡录像不受影响，可事后补检）。
  - 若带 NVIDIA 显卡，YOLOv8n 跑 GPU，识别速度远超 VPS 方案。
  - 服务端全套容器化，以后想迁 VPS 整套搬走即可，代码零改动。
- **相机节点**：ESP32-S3 + OV2640，MJPEG 推流。**按需拉流**（有人观看才传输），局域网内带宽充裕，可 `640×480@10fps`；经 Cloudflare 外网观看时自动降帧率/分辨率。
- **识别两级流水线**：camera-node 在边缘做帧差移动侦测，有动静才上传事件帧到服务器，服务器跑 YOLOv8n-INT8 人形确认，避免常跑模型。
- **存储策略（SD 卡边缘存储）**：全量录像存 camera-node 本地 SD 卡，服务器只存事件快照/短视频。零存储成本。
  - 平时 `320×240@5fps` 连续录（≈6-11GB/天），事件触发切 `640×480` 高清录事件段；32G 卡约覆盖一周。
  - 每分钟一个分段（AVI/MJPEG 或 jpg 序列目录），规避 FAT32 单文件 4G 限制，便于按时间段拉取。
  - 环形缓冲：剩余空间低于阈值（500MB）自动删最旧分段。
  - 断电健壮性：分段追加写，掉电最多损失当前一分钟；开机校验，文件系统损坏则格式化重来（SD 卡可替换，属耗材）。
  - 远程回看历史：浏览器 → 服务器 → MQTT 下发指令 → 节点上传指定分段 → 服务器缓存转发。
- **门窗传感器**：ESP-01 未引出 GPIO16，深度睡眠定时唤醒不可用。两个方案：
  - **干簧管串联供电**（常闭型）：门开 → 上电 → 开机即发 MQTT → 休眠。零待机功耗，电池节点首选。
  - 有插座的位置：ESP8266 开发板（NodeMCU/WeMos）常供电 + GPIO 中断，最简单。
- **告警防抖**：事件合并 + 冷却时间（同一传感器 60s 内只报一次）+ 布防/撤防开关（离家模式才告警，网页一键切换）。
- **外网访问**：Cloudflare Tunnel 命名隧道需要自己的域名，买个便宜后缀（.top/.xyz，~¥10/年）托管到 Cloudflare 即可，免备案。

## 技术选型

| 层 | 选型 |
|---|---|
| 固件 | PlatformIO + Arduino framework（VSCode，Windows 侧开发） |
| 通信 | MQTT over TLS（传感器/控制，局域网）+ HTTPS（帧流/Web） |
| 服务端 | Python FastAPI，Docker Compose（mosquitto + app + cloudflared），跑在旧笔记本 WSL2 |
| 识别 | 帧差法 + YOLOv8n-INT8（有 N 卡走 CUDA，否则 CPU/ONNX Runtime） |
| 告警 | Server酱 Turbo / 企业微信应用消息 |
| 存储 | camera-node SD 卡（全量录像，环形覆盖）+ 服务器 SQLite/文件（事件快照/短视频） |
| 外网 | Cloudflare Tunnel（免费，自带 HTTPS） |

## 硬件采购清单（约 ¥130 一次性 + ¥10/年域名）

- **ESP32-S3-CAM 集成板 × 2**（~¥45/个）：集成 OV2640 + microSD 卡槽，如 XIAO ESP32S3 Sense、Freenove ESP32-S3-WROOM（现有 S3 板无摄像头接口，留作他用）
- **干簧管门窗磁（常开型）× 1**（~¥3）：门开断电/上电方案
- **电池节点电源 × 1**（~¥15）：18650 + 电池盒 + 3.3V 稳压（AMS1117）
- 相机用 5V/2A USB 电源 × 2（~¥8/个，有闲置充电头可省）
- 便宜域名一个（.top/.xyz，~¥10/年，Cloudflare Tunnel 用）
- 服务器：旧笔记本复用，¥0（常开电费 ~¥5-10/月）

## 仓库结构

```
home-monitor/
├── firmware/
│   ├── camera-node/    # PlatformIO, ESP32-S3 + OV2640
│   └── door-node/      # PlatformIO, ESP8266/ESP-01 + 干簧管
├── server/
│   ├── docker-compose.yml   # mosquitto + app + cloudflared
│   └── app/            # FastAPI：MQTT 消费/流转发/检测/告警/Web
└── docs/
    └── design.md       # 本文件
```

## 分期实施

1. **Phase 1（最小可用）**：笔记本 WSL2 docker 环境 + MQTT + door-node 上报 + Server酱告警 → 开门手机收通知
2. **Phase 2**：camera-node 推流（含 SD 卡录像）+ 网页实时监看
3. **Phase 3**：移动侦测 + 人形确认 + 事件快照/录像
4. **Phase 4**：布防撤防、告警策略、多设备管理、Cloudflare Tunnel 外网访问

## 风险与对策

- 笔记本可用性（关机/睡眠/游戏） → 合盖不睡眠常开；识别限核、游戏时降级；SD 卡边缘录像保底
- 笔记本离线时门窗事件丢失 → door-node 固件本地缓存事件，重连后补发
- Cloudflare Tunnel 国内访问偶发不稳 → 可无缝迁移 VPS 方案（容器整套搬走），或加 ¥25/月 VPS 做 frp 中转
- SD 卡磨损/掉电损坏 → 分段追加写 + 开机校验，SD 卡作耗材轮换
- ESP-01 深度睡眠唤醒硬件限制 → 改用干簧管供电方案
- 相机供电不稳导致重启 → 5V/2A 电源
- 电池节点续航 → 干簧管供电方案待机零功耗，仅事件时耗电
