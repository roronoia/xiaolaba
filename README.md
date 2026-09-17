# 黑板小喇叭 (Blackboard Notifier) v3.5

> 专为智慧教室、希沃一体机及教学大屏打造的现代化无感远程叫号与语音播报系统。
> 全量基于 **Cloudflare Pages** 静态加速 + **EMQX Cloud** 高性能 MQTT 实时消息架构。

---

## 🌟 系统核心特性

1. **100% 纯云端原生架构**
   - **移动端**：由 Cloudflare Pages 全球 CDN 加速托管，毫秒级加载。
   - **双向消息信道**：基于公网 EMQX Cloud MQTT（WSS / TCP），教师在操场、走廊、办公室或校外使用手机 4G/5G/校园网均可秒级触达教室大屏。
   - **零本地端口监听**：彻底抛弃本地 HTTP 8088 端口与内网穿透工具，不占端口、免配置路由器、杜绝防火墙拦截。

2. **现代毛玻璃微质感 UI（Fluent / iOS 风格）**
   - 教学大屏通知采用半透明高斯模糊（Backdrop Blur）、1px 极细微高光描边、大圆角与多层弥散投影。
   - 专为远距离观察优化的超大字号排版（标准大字 34px / 教学大屏推荐 44px / 远距离特大 54px）。
   - 鼠标悬停智能暂停倒计时，集成“🔁 重读一遍”与“✕ 快速关闭”。

3. **三种播报模式自由切换**
   - 🔔🗣️ **叮咚+语音**：先播放清脆叮咚音效，再调用 Windows 原生 SAPI 朗读（支持设置朗读遍数）。
   - 🔔✨ **仅叮咚提示**：只播放提示音并弹出卡片，不朗读学生姓名，避免打断课堂演示。
   - 🔕🤫 **静音大屏弹窗**：无任何声响，仅屏幕置顶大字显示，适合自习或考试监考。

4. **双重防伪与安全防杀机制**
   - **双向校验**：班级编号（`room_id`）与教师防伪口令（`secret_token`）双重严格匹配，防乱播防误触。
   - **退出与设置密码锁**：进入系统高级设置或退出软件，必须输入管理员密码（初始默认 `888888`），防止学生误关闭。
   - **双进程防杀与自愈看护**：针对 Windows 任务管理器强杀保护，后台运行零开销内核级看护守护进程。若主程序在任务管理器中被恶意“结束任务”，守护进程将在 0.5 秒内自动复活主程序并弹出自愈警报！
   - **防误触最小化**：点击右上角关闭按钮默认平滑缩至系统托盘后台常驻，不中断服务。

---

## 📁 源码目录结构

```
classroom_notifier/
├── receiver.py                 # 电脑端/教室大屏 PyQt6 主程序源码
├── mobile.html                 # 手机端/移动控制台前端页面 (现代微质感 H5)
├── mqtt.min.js                 # 离线 MQTT 客户端库 (支持 WebSocket)
├── app_icon.ico                # 软件高清矢量拟物图标
├── chime.wav                   # 教学级清脆立体声叮咚音效
├── config.json                 # 运行时配置文件
├── requirements.txt            # Python 依赖清单
├── 黑板小喇叭.spec             # PyInstaller 打包构建脚本
├── 启动黑板小喇叭.bat          # 一键便捷启动脚本
├── 使用说明与快速配置.txt      # 详细用户使用与部署指南
└── cloudflare_pages_dist/      # Cloudflare Pages 静态部署目录
    ├── index.html              # 生产环境入口页面
    └── mqtt.min.js             # 离线库
```

---

## 🚀 开发者运行与编译指南

### 1. 安装依赖
运行环境推荐：Python 3.10 / 3.11 / 3.12 (64-bit Windows)
```bash
pip install -r requirements.txt
```

### 2. 本地直接运行
```bash
python receiver.py
```

### 3. 一键打包为独立可执行程序
```bash
pyinstaller --noconfirm 黑板小喇叭.spec
```
构建产物将输出至 `dist/黑板小喇叭/`。

---

## 📱 手机端 Cloudflare Pages 部署流程 (1 分钟)

1. 打开 [Cloudflare 控制台](https://dash.cloudflare.com/) 并登录。
2. 进入【Workers & Pages】->【Create application】->【Pages】->【Upload assets】。
3. 输入项目名称（如 `blackboard-notifier`），点击【Create project】。
4. 将 `cloudflare_pages_dist` 目录内的 `index.html` 和 `mqtt.min.js`（或直接使用 `cloudflare_pages_deploy.zip`）上传。
5. 点击【Deploy site】，获得您的专属访问域名（如 `https://blackboard-notifier.pages.dev`）。
6. 在电脑端【系统设置 🔒】中填入该域名，手机扫描设置界面生成的二维码即可即用即发！
