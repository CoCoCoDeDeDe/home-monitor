# home-monitor 设计方案

DIY 家庭监控系统：多路无线摄像头 + 门窗开关传感器 + 人形识别告警 + 外网实时监看。

## 系统架构

```
【感知层 · 家中】                                【中心服务器 · 家中旧笔记本】
camera-node ×N                                  ┌─ Mosquitto (MQTT over TLS)
  ESP32-S3 + OV2640                             ├─ ingest     接收帧流/鉴权
  MJPEG 按需推流 ──────────┐                    ├─ detector   帧差移动侦测 → YOLOv8n 人形确认
                           │                    ├─ recorder   事件快照/短视频存储
collision-node ×N               ├──局域网 WiFi─────▶ ├─ alerter    防抖聚合 → Server酱
  ESP8266/ESP-01 + 干簧管   │  MQTT/HTTPS        ├─ web        监控看板（物模型渲染）+ 历史事件 + 布防撤防
  开门即唤醒上报 ──────────┘                    └─ Tailscale  外网组网出口
                                                【客户端】手机/电脑装 Tailscale → http://<tailscale-ip>:8000
```

所有服务跑在旧笔记本 WSL2 + Docker Compose 里，设备走局域网 WiFi 直连；外网访问经 **Tailscale 组网**（无公网 IP/CGNAT 场景零成本、WireGuard 加密、零端口开放），Cloudflare Tunnel 作为免客户端分享场景的备选。

## 组网设计

**单网段扁平组网，路由器零配置**——无端口转发、无 DDNS、无 VLAN，NAT 只走出站连接。

- **WiFi**：所有 ESP 节点连家里路由器的 **2.4GHz** WiFi（ESP8266/ESP32 不支持 5GHz；若路由器 2.4G/5G 合频 SSID 导致配网失败，需拆出独立 2.4G SSID），DHCP 自动分配 IP。
- **服务器地址**：固件中 MQTT/HTTPS 地址填 **mDNS 主机名**（`<服务器主机名>.local`，节点自解析），换网络环境 IP 变化免重配；Windows 服务器需网络配置为"专用"并放行 UDP 5353（见 deployment.md）。DHCP 静态绑定不再是必要条件。
- **通信矩阵**：

| 链路 | 协议/端口 | 说明 |
|---|---|---|
| collision-node → 笔记本 | MQTT `1883`（账号密码；TLS 8883 后续 ticket 补上） | 开门事件、心跳 |
| camera-node → 笔记本 | MQTT `8883` + HTTPS POST | 心跳/控制 + 事件帧、SD 分段上传 |
| 浏览器 → 笔记本（家中） | 局域网 HTTPS | 实时监看、历史回看 |
| 浏览器 → 笔记本（外网） | Tailscale 虚拟网 | 查看设备装客户端入 tailnet，WireGuard 加密，无入站端口 |
| 笔记本 → Server酱 | HTTPS 外连 | 告警推送 |

- **断网降级**：宽带/Tailscale 断了，局域网内监控、SD 卡录像、事件记录全部照常，仅微信推送和外网访问失效，恢复后补发缓存事件。

## 物模型设计（节点类型通用化）

目标：接入任意新类型 node，前后端代码零改动或极少改动。物模型基底采用 **Home Assistant MQTT Discovery 词汇**（binary_sensor/sensor/camera 等实体类型 + device_class + unit），扩展自有字段（告警级别等）——协议即 HA 方言，将来接入真 Home Assistant 零成本。

**三层约定**：

1. **基础协议**（所有节点类型必须实现，借用物模型"属性/事件"思想）：
   - `<type>/<id>/status`：LWT 在线状态（retain）
   - `<type>/<id>/health`：周期健康上报（rssi/uptime，非 retain）
   - `<type>/<id>/...`：类型专属主题（contact 的 state 即属性；事件带 `cached` 标记可补发）
   - 服务调用（物模型"服务"要素）预留 `<type>/<id>/svc/+`，摄像头来了再实现
2. **类型档案（Node Type Profile）**：类型知识全部收进 JSON（`server/app/nodetypes/<type>.json`），HA discovery config 兼容 + 扩展字段；一个 schema 同时驱动看板渲染、payload 校验、告警级别
3. **渲染**：前端通用渲染器按档案出卡片；未知类型兜底卡片（id/在线/信号/原始字段），新节点永远能显示；复杂类型（摄像头实时画面）才允许注册专用组件

**关键决策：HA discovery config 由服务端按档案代发（retain），不由固件发**——HA 不关心来源，固件保持轻量零改动；将来接真 HA 时 publisher 直接复用。

**关键决策：图引擎（Niagara 理念），固件上报硬件本质类型，部署语义是图中块的参数（Issue #27）**——同一硬件可服务不同语义场景（碰撞传感器装窗上是门窗，放货架上是在位）。分四层：

- **固件**：只上报硬件本质类型（collision）+ 原始布尔（`{"state":1}`=触发/`0`=释放），退化为诚实的二值上报器；线上与存储只跑布尔，文字只在图输出出现
- **图引擎**（`graph_engine/blocks/graph_store/graph_service`）：数据流图为唯一底座，每节点一张小图（`io_in → translate → display / alert`），存 SQLite `graphs` 表；显示文案与告警全部是图输出，无硬编码
- **表单即图的投影**：看板配置表单（`GET/PUT /api/nodes/{id}/config`）只是节点标准子图的读/写投影——用户直接定义 0/1 各对应什么文案与级别；contact/presence 降级为预填模板，与系统无持续关联
- **默认值策略**：无图节点按固件类型档案自动生成默认图（原始文案），渐进升级不破坏现有节点

硬件改用途 = 看板改配置，无需改固件；未来图联动（逻辑门/延时/设备互控）只是注册新块类型，不动架构。

**关键决策：原始值类型系统 + 大数据通道分离**——IoT ↔ 后端的原始数据只有基础类型（布尔/枚举/数字/字符串），语义全部后置到图配置：

- **小数据走类型系统**：bool=两行文案；enum=N 行文案（同一机制推广）；number=格式化（单位/精度）+ 阈值定级（threshold 块）；string=原样。翻译块按声明类型分派，events.payload 是 JSON 原生类型，DB 无感
- **大数据/流媒体不进 MQTT 值通道**：直播地址、点云、录像分段在 MQTT 里只传**指针**（URL/路径字符串——对系统就是个 string IO 点），内容走 HTTP/流按需拉取（SD 卡录像回看即此模式）；显示块按类型渲染（URL→`<img>` 直播，文字→文案），即"复杂类型注册专用组件"的落点
- **二进制不进库**，events 只存文件路径（events.py 既有约定）

**接入新节点成本**：简单传感器 = 固件遵守基础协议 + 丢一个档案 JSON；复杂节点 = 再加一个前端专用组件。

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
- **告警通道**：Server酱 Turbo（免费 5 条/天，防抖后够用）。
- **MQTT 安全**：先明文 + 账号密码（局域网风险低，快速跑通）；TLS 8883 作为独立 ticket 后续补上。
- **固件配网**：WiFiManager 配网门户（设备开热点手机配网，换网络免重烧）。
- **Web 前端**：服务端渲染 + 原生前端（FastAPI + Jinja2/原生 JS），单进程交付，不做 SPA。
- **物模型（节点类型通用化）**：见下节「物模型设计」。
- **事件存储**：事件落盘 **SQLite**（单文件、Python 内置、挂 volume 即可；事件量小 + 单写者，不上 PostgreSQL/MongoDB；标准 SQL 保留平迁路径）。图片/录像等二进制不进库，只存文件路径。
- **外网访问**：**Tailscale 组网**（宽带 CGNAT 无公网 IP 的现实选择：零成本、WireGuard 端到端加密、零端口开放；每个查看设备装客户端）。备选：Cloudflare Tunnel（浏览器免客户端，适合分享给家人，需要自有域名 ~¥10/年托管 Cloudflare，免备案）。

## 技术选型

| 层 | 选型 |
|---|---|
| 固件 | PlatformIO + Arduino framework（VSCode，Windows 侧开发） |
| 通信 | MQTT over TLS（传感器/控制，局域网）+ HTTPS（帧流/Web） |
| 服务端 | Python FastAPI，Docker Compose（mosquitto + app + cloudflared），跑在旧笔记本 WSL2 |
| 识别 | 帧差法 + YOLOv8n-INT8（有 N 卡走 CUDA，否则 CPU/ONNX Runtime） |
| 告警 | Server酱 Turbo |
| 前端 | 服务端渲染（Jinja2 + 原生 JS），随 app 同进程 |
| 存储 | camera-node SD 卡（全量录像，环形覆盖）+ 服务器 SQLite/文件（事件、快照/短视频） |
| 物模型 | Home Assistant MQTT Discovery 兼容（类型档案 JSON，服务端代发 config） |
| 外网 | Tailscale 组网（免费，WireGuard 加密；Cloudflare Tunnel 备选） |

## 硬件采购清单（约 ¥130 一次性 + ¥10/年域名）

- **ESP32-S3-CAM 集成板 × 2**（~¥45/个）：集成 OV2640 + microSD 卡槽，如 XIAO ESP32S3 Sense、Freenove ESP32-S3-WROOM（现有 S3 板无摄像头接口，留作他用）
- **干簧管门窗磁（常开型）× 1**（~¥3）：门开断电/上电方案
- **电池节点电源 × 1**（~¥15）：18650 + 电池盒 + 3.3V 稳压（AMS1117）
- 相机用 5V/2A USB 电源 × 2（~¥8/个，有闲置充电头可省）
- 便宜域名一个（.top/.xyz，~¥10/年，**备选** Cloudflare Tunnel 用；当前外网走 Tailscale 不需要）
- 服务器：旧笔记本复用，¥0（常开电费 ~¥5-10/月）

## 仓库结构

```
home-monitor/
├── firmware/
│   ├── camera-node/    # PlatformIO, ESP32-S3 + OV2640
│   └── collision-node/      # PlatformIO, ESP8266/ESP-01 + 干簧管
├── server/
│   ├── docker-compose.yml   # mosquitto + app + cloudflared
│   └── app/            # FastAPI：MQTT 消费/流转发/检测/告警/Web
└── docs/
    └── design.md       # 本文件
```

## 分期实施

1. **Phase 1（最小可用）**：docker 环境 + MQTT + collision-node 上报 + Server酱告警 ✅ → LWT/缓存可靠性 ✅ → 健康上报 ✅ → mDNS 主机名 ✅ → Web 监控看板（物模型）
2. **Phase 2（摄像头）**：camera-node 推流（含 SD 卡录像）+ 看板实时监看；后续移动侦测 + 人形确认 + 事件快照
3. **Phase 3（外网访问）**：Tailscale 组网 ✅；布防撤防、告警策略、多设备管理

## 风险与对策

- 笔记本可用性（关机/睡眠/游戏） → 合盖不睡眠常开；识别限核、游戏时降级；SD 卡边缘录像保底
- 笔记本离线时门窗事件丢失 → collision-node 固件本地缓存事件，重连后经 syncreq/sync 握手补发
- Tailscale 不可用或需免客户端分享 → 切换 Cloudflare Tunnel 备选方案（容器加一个 cloudflared 即可）
- SD 卡磨损/掉电损坏 → 分段追加写 + 开机校验，SD 卡作耗材轮换
- ESP-01 深度睡眠唤醒硬件限制 → 改用干簧管供电方案
- 相机供电不稳导致重启 → 5V/2A 电源
- 电池节点续航 → 干簧管供电方案待机零功耗，仅事件时耗电
