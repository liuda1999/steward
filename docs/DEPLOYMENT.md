# 部署与启动

这份文档回答一个问题：**拿到这份代码之后，怎么把它变成一个跑着的服务。**

分四段：本地启动（最常见）→ 局域网共享 → 开机自启 / 后台常驻 → 升级与排错。
只想跑起来的话，读第一节就够了。

---

## 一、本地启动

### 1.1 一条命令

```bash
git clone https://github.com/liuda1999/steward.git
cd steward
./scripts/start.sh
```

Windows 用 `scripts\start.bat`。

**之后每次启动都是同一条命令**——配置已经在 `config.yaml` 里，不会覆盖。

这条命令会依次做完四件事，并把每一步的结果打出来：

```
  steward 1.6.9  启动检查

  [1/4] Python 3.13.14  OK
  [2/4] 依赖齐全  OK
  [3/4] 首次启动，已生成配置：/path/to/steward/config.yaml
  [4/4] 配置校验通过  OK
```

然后服务起来了，终端里会打印：

```
  端点 http://127.0.0.1:27418/v1
  key  /path/to/steward/state/keys.json
  控制台 http://127.0.0.1:27418/  （填槽位、改配额、签发 key）
  口令   xxxxxxxxxxxxxxxx
```

**口令只显示这一次**，关掉终端就看不到了。它也存在 `state/console.token` 里，
但那是文件权限保护下的明文，不是给你复制到别处用的。

### 1.2 然后打开控制台

浏览器打开 <http://127.0.0.1:27418/>，输入上面那串口令。

控制台里要做的事只有一件：**把两个槽位填上**。

| 槽位 | 填什么 | 常见值 |
|---|---|---|
| 基础模型 | 日常主力的地址 + 模型标识 | Ollama `http://127.0.0.1:11434/v1` · LM Studio `http://127.0.0.1:1234/v1` · llama.cpp `http://127.0.0.1:8080/v1` |
| 进阶模型 | 云端外援的地址 + 模型标识 | 留空 = 纯本地模式（推荐先用这个跑通） |

填完点「测试连接」确认通，再点「保存并重启」。

> **进阶槽位留空是合法配置，而且是最省的一种。** 空着就不注入 `consult_cloud`
> 工具，每轮少发约 500 tokens 的工具定义，而且完全不会误调云端。
> 先用纯本地跑通，再决定要不要接云端 —— 顺序反了容易在还没搞懂配额机制时
> 就把额度花掉。

### 1.3 接进客户端

```
base_url = http://127.0.0.1:27418/v1
api_key  = st-xxxxxxxx-...
model    = auto
```

Claude Code、Cline、Continue、opencode，或者自己写的脚本，都是这三行。
`model` 填什么都不影响路由 —— 网关不按它分发。

**key 只在签发时显示一次。** 丢了就重新签发一把、吊销旧的：

```bash
python -m steward key new --label my-agent
python -m steward key list
python -m steward key revoke <id>
```

一个智能体一把 key。共用的话，某个 agent 失控烧配额时你没法定位是谁。

---

## 二、装成命令（可选）

不装也能跑，但装完之后 `steward` 就是一条全局命令，
不用每次 `python -m steward`、也不用记住自己在哪个目录。

```bash
pip install -e .
steward serve
```

`-e` 是可编辑安装：改了源码直接生效，不用重装。发行名在 PyPI 上叫
`steward-gateway`（`steward` 这个名字被别人占了），但**命令行入口仍然叫
`steward`**。

### 2.1 用虚拟环境（推荐）

装到全局会污染系统的 Python 环境，而且和别的项目抢同一批依赖版本。

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -e .
steward serve
```

`scripts/start.sh` 和 `scripts/start.bat` 会**自动优先用 `.venv`**，
所以建好虚拟环境之后，启动脚本不用改。

---

## 三、局域网共享

给家里另外几台设备用（手机、平板、另一台电脑）。

### 3.1 改监听地址

```yaml
server:
  host: 0.0.0.0        # 从 127.0.0.1 改成这个
```

命令行临时改也行：`steward serve --host 0.0.0.0`。

> **非回环地址 + `auth.mode: off` 会被拒绝启动。** 这是故意的：
> 局域网里任何人都能用掉你的云端额度。`auth.mode` 保持 `required` 就行。

### 3.2 每台设备一把 key

```bash
python -m steward key new --label 客厅主机
python -m steward key new --label 笔记本
```

配额按 key 归因。吊销其中一把不影响其他设备。

### 3.3 找到本机的局域网地址

启动时终端会直接打印：

```
  局域网 http://192.168.1.23:27418/v1
```

别的设备用这个地址。如果没打印出来，用 `ipconfig`（Windows）或
`ip addr`（Linux）查。

### 3.4 防火墙

Windows 上第一次监听非回环地址，防火墙会弹窗询问。**要允许专用网络**，
否则别的设备连不上，而且报错在对方设备上（超时），很容易以为是网关的问题。

Linux 上：

```bash
sudo ufw allow 27418/tcp
```

---

## 四、开机自启 / 后台常驻

### 4.1 Linux：systemd

写一个 unit 文件：

```ini
# /etc/systemd/system/steward.service
[Unit]
Description=steward - local model gateway
After=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/steward
ExecStart=/path/to/steward/.venv/bin/python -m steward serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now steward
sudo systemctl status steward
journalctl -u steward -f      # 看日志
```

`Restart=on-failure` 值得留着：网关挂了自动拉起来。
但**别配 `Restart=always`** —— 你按 Ctrl+C 想停它，它会立刻自己爬起来。

### 4.2 Windows：计划任务

最省事的方式是 NSSM（把程序注册成 Windows 服务）：

```bash
nssm install steward "C:\path\to\steward\.venv\Scripts\python.exe" "-m steward serve"
nssm set steward AppDirectory C:\path\to\steward
nssm set steward Start SERVICE_AUTO_START
nssm start steward
```

不想装第三方软件的话，用任务计划程序：触发器选「计算机启动时」，
操作选「启动程序」，程序填 `python.exe`，参数填 `-m steward serve`，
「起始于」填 steward 的目录。

### 4.3 macOS：launchd

```xml
<!-- ~/Library/LaunchAgents/com.steward.gateway.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.steward.gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/steward/.venv/bin/python</string>
    <string>-m</string><string>steward</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/steward</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.steward.gateway.plist
```

---

## 五、升级

```bash
git pull
pip install -e .          # 依赖有变动时必须重装
```

**要升级时先看一眼配置还在不在**：

```bash
python -m steward serve --check
```

`config.yaml` 不在版本控制里（它进 `.gitignore`），所以 `git pull` 不会动它。
但如果新版本改了配置项的默认值，旧的 `config.yaml` 里没有这些新项 ——
网关会用代码里的默认值。**默认值变了不会自动写进你的文件**，这是有意的：
不然一次 `git pull` 就能改掉你手改过的配置。

启动日志里的 `[配置] 注意` 会告诉你哪些项需要手动补。

### 5.1 存档目录会跟着版本走

交付物存档在 `.steward/artifacts/`，按目录名里的日期分目录。
升级不会动它。旧版本的存档新版本照样读。

---

## 六、排错

### 6.1 启动就报错

| 报错 | 原因 | 怎么办 |
|---|---|---|
| `base.base_url 未配置` | 配置文件不存在或没填 | `python -m steward init`，或开控制台填 |
| `base.model 还是模板占位值` | 还是刚生成的模板 | 控制台里填真实模型标识 |
| `无法连到本地模型` | 上游没起 | 先确认 Ollama / llama.cpp 在跑，`python -m steward test` 能测 |
| `[端口] ... 已被占用` | 别的进程占了这个端口 | `steward serve --port 27xxx` 换一个 |

### 6.2 端口冲突

默认端口 **27418**。选它是有原因的：不是 80/8080 这类几乎必被占的端口，
也不是 12345 这种连续数字（撞车时一眼看不出是谁占的）。

真撞上了终端会直接告诉你占用进程的 PID：

```
[端口] 127.0.0.1:27418 已被占用（PID 13028，tasklist /FI "PID eq 13028" 可查进程名）。
```

### 6.3 浏览器打不开控制台

两种情况，症状不一样，处理方式也不一样。

**端口上根本没有进程。** 直连会立刻被拒绝：

```bash
curl --noproxy '*' -m 3 http://127.0.0.1:27418/health
# curl: (7) Failed to connect —— 网关没在跑，先 steward start
```

默认端口是 27418，但改过 `server.port` 或用过 `--port` 就不是它了。
要看启动横幅里那行 `控制台 http://127.0.0.1:xxxxx/`。

**浏览器一直转圈，最后显示「未响应」。** 端口是通的，但请求被代理接走了：
浏览器或系统代理没有把 `127.0.0.1` / `localhost` 放进直连例外。把这两个
加进代理的 bypass 列表，或临时关掉代理再刷新。

判断是不是代理的锅，加 `--noproxy` 绕开它直连：

```bash
curl --noproxy '*' -m 3 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:27418/
```

返回 200 说明服务没问题，卡在浏览器到本机这一段。

### 6.4 客户端连不上

按这个顺序查：

```bash
curl http://127.0.0.1:27418/health          # 服务活着吗
curl http://127.0.0.1:27418/v1/models        # 鉴权过了吗（没带 key 应该 401）
python -m steward test                      # 两个槽位通吗
python -m steward ctx                       # 本地模型的真实窗口是多少
```

`/health` 返回 200 但 `/v1/models` 返回 401 —— 这是对的，鉴权在起作用。

### 6.5 模型忽然变笨

跑 `python -m steward ctx`。它会打出真实窗口、输入预算、计数方式三个数字。
大部分「忽然变笨」的问题，从这三个数字就能看出来 —— 比如上游换了个更小的模型，
但预算还是按老窗口算的。

### 6.6 本地窗口上限怎么填

`context.ctx_limit` 是**任意整数，不设档位**。64K / 128K / 256K 只是说明里举的常见例子，
100000、1048576（1M）、甚至更大的值都照样能用；控制台的「窗口上限」是普通数字输入框，
没有下拉枚举。

三种情况：

| 你填的值 | 行为 |
|---|---|
| `0`（默认） | 启动时向模型探测：`/props` → `/v1/models` → `/api/v0/models`，拿到**每槽**真实窗口 |
| 任意正整数，探测失败 | 直接采用这个值 |
| 任意正整数，探测成功 | 取 `min(探测值, 你填的值)` —— 它是**上限**，只能压低，不能抬高真实窗口 |

探测全部失败且留 `0` 时，保守按 `32768` 处理，并在 `steward ctx` 里标 `source: default`。

所以**不要为了「想要更大窗口」而填一个大数**：真实窗口由模型服务端决定，填大了不会让它变大，
反而可能让网关按错误预算塞满、触发上游静默截断。这个字段的用途是：探测不到真实值时手动指定，
或者主动把预算压得比真实窗口更保守。

### 6.7 完整对照表

`steward/README.md` 末尾有一份更细的排错对照表，按「症状 → 可能原因 → 怎么查」
三列排的。

---

## 七、命令行速查

| 命令 | 用途 |
|---|---|
| `steward start` | **正式启动入口**：检查环境 → 备配置 → 起服务 |
| `steward serve` | 直接起网关（不检查环境） |
| `steward init` | 交互式配置两个槽位并签发首把 key |
| `steward serve --check` | 只校验配置，不启动 |
| `steward test` | 测两个槽位的连通性（探模型列表免费） |
| `steward ctx` | 看真实窗口 / 输入预算 / 计数方式 |
| `steward key new --label <名字>` | 签发新 key |
| `steward key list` / `revoke` | 列出 / 吊销 |
| `steward refs <路径…>` | 查某个路径会不会被 refs 放行 |
| `steward artifacts` | 看存档占用，`--clean` 回收 |

---

## 八、相关文档

| 文档 | 内容 |
|---|---|
| [`README.md`](../README.md) | 项目总览：解决什么问题、三个关键设计 |
| [`steward/README.md`](../steward/README.md) | 实现细节与完整排错对照表 |
| [`ARCHITECTURE.md`](../ARCHITECTURE.md) | 为什么这样设计：决策过程与被否掉的方案 |
| [`CONTEXT-DESIGN.md`](../CONTEXT-DESIGN.md) | 上下文治理的设计论证 |
| [`README.en.md`](../README.en.md) | 英文版总览 |
