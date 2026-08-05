# 部署手册

服务端（Windows 主机 + Docker Desktop）部署要点与踩坑记录。目标机器：旧笔记本（WSL2 + Docker Desktop），开发本同样适用。

## 一键启动

```bash
cd server
cp .env.example .env   # 填入 MQTT_USER/MQTT_PASS/SCT_SENDKEY
# 生成 MQTT 密码文件（用户名密码与 .env 一致）
docker run --rm -v "$PWD/mosquitto:/mosquitto/config" eclipse-mosquitto:2 \
  mosquitto_passwd -b -c /mosquitto/config/password <user> <pass>
# 密码文件权限必须让 mosquitto 用户可读（mosquitto 启动后降权读 pwfile）
docker run --rm -v "$PWD/mosquitto:/mosquitto/config" eclipse-mosquitto:2 \
  chown mosquitto:mosquitto /mosquitto/config/password
docker compose up -d mosquitto app
```

验证：`curl http://localhost:8000/health` → `{"status":"ok","mqtt_connected":true}`

## Windows 主机网络（关键，缺一外部设备就进不来）

Docker Desktop（WSL2 后端）的端口发布**默认只在 localhost 可达**，局域网设备（传感器、手机）访问需三层放行：

1. **WSL 镜像网络**：`C:\Users\<user>\.wslconfig` 配置 `[wsl2] networkingMode=mirrored`，然后 `wsl --shutdown` 重启 WSL 生效
2. **Windows 防火墙**（管理员 PowerShell）：
   ```powershell
   New-NetFirewallRule -DisplayName 'home-monitor-mqtt-1883' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 1883 -Profile Any
   ```
3. **Hyper-V 入站策略**（管理员 PowerShell，默认是 Block）：
   ```powershell
   Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
   ```

验证方式：**用局域网内另一台设备**（手机浏览器开 `http://<主机IP>:8000/health`）。
注意：主机自己访问自己的 LAN IP 不通是 mirrored 模式的 hairpin 现象，属正常，**不是**故障；`netsh interface portproxy` 方案在此模式下无效，勿用。

## 外网访问（Tailscale 组网）

宽带无公网 IP（CGNAT），不暴露任何公网端口，用 Tailscale 把查看设备组进虚拟网：

1. 服务端主机装 Tailscale（`winget install Tailscale.Tailscale`），`tailscale up` 登录
2. 手机/笔记本装 Tailscale App，**同一账号**登录并连接
3. 外网访问 `http://<服务端 Tailscale IP>:8000/...` 即可；Tailscale IP 查 `tailscale status` 或管理后台

说明：

- 传输由 WireGuard 端到端加密，MQTT/Web 均不暴露公网，板子零改动
- mirrored 网络下 Docker 发布端口对 Tailscale 接口同样可达，无需额外防火墙规则
- 主机自访自己的 Tailscale IP 不通也是 hairpin 现象，用另一台设备验证
- 局限：每个查看设备都要装客户端；如需免客户端分享给他人，再评估 Cloudflare Tunnel

## contact-node 固件

- 首次/重配：开机后 1.5s 内按住 FLASH 键 ≥300ms → 清空配置 → 热点 `contact-node-setup` → `192.168.4.1` 配网（WiFi + MQTT 四项）
  - 注意按键时序：先按 RST 松手、**再**在 1.5s 窗口内按住 FLASH；RST 之前就按住 FLASH 会进 ROM 下载模式
- MQTT 参数经 LittleFS 持久化，重启不丢（WiFiManager 自定义参数不落盘，必须自己存）
- MQTT host 可填 IP 或 **mDNS 主机名**（`<服务器主机名>.local`）：主机名由固件手写最小 mDNS A 查询解析（LEAmDNS 无主机查询 API），解析失败回退原样；持续连接失败 >30s 自动重新解析。**换网络环境（IP 变化）免重配**，建议直接填主机名
- 干簧管接 D1(GPIO5) 与 GND：断开=开门（`open`），闭合=关门（`closed`）

### mDNS 主机名对服务器的要求（Windows）

- 网络配置文件须为**专用（Private）**：`Set-NetConnectionProfile -InterfaceAlias WLAN -NetworkCategory Private`（公用配置下 Windows 不应答外部 mDNS 查询，仅对可信网络开启）
- 防火墙放行 mDNS（管理员 PowerShell）：
  ```powershell
  New-NetFirewallRule -DisplayName 'home-monitor-mdns-5353' -Direction Inbound -Action Allow -Protocol UDP -LocalPort 5353 -Profile Any
  ```

## contact-node 现场部署 checklist（#9 实测）

门窗传感器节点从桌面到实体安装的完整流程与坑。

### 接线（碰撞传感器模组 → NodeMCU）

| 模组 | NodeMCU | 线色 |
|------|---------|------|
| VCC  | 3V3     | 红   |
| GND  | GND     | 黑   |
| OUT  | D1(GPIO5) | 黄 |

### 步骤

1. **桌面极性验证**：手动压开关，确认看板事件极性与语义吻合（碰撞模组：杠杆压下=HIGH=closed，翘起=LOW=open），再上墙
2. **固定**：确定安装压点与走线，3M 胶固定模组与板子；开关窗各测一次确认事件上报
3. **供电**：见下节决策
4. **长稳观察**：连续运行 ≥3 天，每日检查 uptime（掉线会清零）、rssi、离线/补发事件

### 供电决策（实测结论）

- **充电宝不可用作常供电**：低负载（ESP8266 常连 ~70mA）触发自动断电，上线 38~80s 即断，稳定复现。除非充电宝支持关闭自动断电（小电流模式），否则勿用
- **墙插可行**：5V USB 充电头直供。实测连续运行 ~36h（rssi -55~-60，期间仅人为拔电中断），上报零丢失零误报
- 无插座位置（如本案面向平台的窗户）：等电池低功耗方案（#11，deep sleep + 组网）

### 长稳验证记录（2026-08-02 ~ 08-05）

- 连续在线 ~36h，rssi -55~-60（信号良好），状态上报准确、零误报
- 期间所有离线均为**人为拔电**（插座紧张），非链路故障；遇到秒级/分钟级瞬断先排除人为与路由器因素再排查固件

## 串口驱动

CH340 芯片（VID_1A86&PID_7523）：官网 https://www.wch.cn/downloads/CH341SER_EXE.html 下载 CH341SER.EXE 安装（有反爬，需浏览器下载）。
