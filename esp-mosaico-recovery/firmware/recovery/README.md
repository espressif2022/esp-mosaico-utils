# ESP-Mosaico Vibe Mode

`factory` 是 `esp-mosaico-recovery` 内置的 ESP-Mosaico 保留 Recovery 固件，
其源码和评审 bundle 与 `mosaico.py recover` 一同维护。普通应用从宿主
workspace 的 `projects/hello_world` 创建，不应将本工程作为应用安装到 `ota_0`。

当前源码构建的 Recovery 固件版本为 `0.1.2`，由
`sdkconfig.recovery.defaults` 中的 `CONFIG_APP_PROJECT_VER` 定义。
`prebuilt/recovery` 基础包也使用 `0.1.2`，其 manifest 记录各镜像的大小与 SHA-256。
2026-09-22 更新纳入 ESP-61 页面、OTA 优化及允许缺省数据分区镜像的修复。
本次整包通过构建、布局、哈希及宿主测试；维护者于 2026-09-22 确认新版预置包
已完成真机验收。
源码来源与 dirty 状态以 `prebuilt/recovery/manifest.json` 为准。
本次使用 ESP-IDF `v6.2-dev-2991-g0f1b3e3ca392` 和仓库默认配置完整构建：
`factory.bin` 为 1,823,936 字节，固定槽位剩余 11,072 字节；INFO 日志
`bootloader.bin` 为 24,560 字节，距离分区表仅余 16 字节，后续改动须特别关注容量。
默认 `recover` 使用该包，`recover --source current` 使用当前源码重新构建。
Recovery ABI 与分区布局由工程配置和包 manifest 约束。

System Update 版本检查将 `0.1.2` 解析为同一语义版本线，只接受同一主版本线、且
最低版本要求不高于当前 Recovery 的更新包。Recovery 自更新拒绝降级或
跨主版本线的镜像。

## 静态开机 Logo

保留 Recovery 的二级 bootloader 在选择应用分区前，通过 SPI2 QSPI 初始化
CO5300，绘制黑底橙色点阵 `mosaico`。Logo 使用紧凑的字形数据逐像素生成，
不依赖 LVGL、GSP、PSRAM 或外部 UI 资源。支持 eFuse 标识的 v1.0、v1.1 和
v1.2 板；未知板型或 SPI 传输失败时跳过 Logo，继续正常启动与 Recovery。

确认硬件版本后，bootloader 会先打开 VCC_3V3 并触发约 60 ms 的 GPIO8 短震，
再继续屏幕初始化和 Logo 绘制。短震复用了原有屏幕复位等待时间，没有额外延长
启动流程；失败路径会再次确保马达关闭，未知板型不会驱动马达。

Logo 完整写入后通过 LP STORE15 发布一次性屏幕交接标记。支持该协议的 BSP
会在应用启动时消费标记，跳过重复面板 reset、Sleep Out 和 Display On 延迟，
保留 Logo 直至应用首帧覆盖。Logo 初始化或传输失败时不发布标记，BSP 自动
回到原来的完整初始化流程。

bootloader 默认启用 INFO 串口日志，输出启动信息、分区选择、Logo 成功信息及
警告/错误；使用默认 UART0、115200 波特率。此阶段 ESP-Iris 尚未启动，
`iris logs` 不会回放这些早期串口日志。

Logo 的 SPI2 总线固定为 40 MHz、半双工只写，通过 ESP-IDF LL 配置发送，
不引入通用 SPI HAL 的 RX、DMA 与动态事务配置。板型读取使用 ESP32-S31
USER_DATA 的低 16 位只读寄存器，虚拟 eFuse 构建保留原字段读取 API。
单字节屏幕初始化指令使用紧凑表；字模、像素、短震、失败回退和屏幕交接保持不变。
ESP-IDF `7b9cc1ac79f8` 实测 INFO bootloader 为 24,432 字节，比优化前同级
日志的 26,720 字节缩小 2,288 字节，在 `0x2000` 到 `0x8000` 的 24 KiB
固定空间内剩余 144 字节。后续修改仍须通过 bootloader 容量检查。

分区布局、OTA 选择、Recovery Boot 按键和恢复协议不变。维护者必须
在更新源码后重新生成并校验 `prebuilt/recovery` 的完整包，不能只替换其中一个
镜像；普通应用更新仍使用宿主 workspace 的 `mosaico.py iris app-update`。

## 用户命令

从源码构建前，在已激活的 ESP-IDF Python 环境中安装资源编码依赖：

```sh
python -m pip install -r firmware/recovery/tools/requirements.txt
```

该工具依赖 Python 3.10+，仅用于离线 Zopfli 压缩；设备继续使用 ROM 的 zlib/Deflate
解码，场景、字号和字形内容不变。CI 在构建 Python 环境中安装同一固定版本。

在仓库根目录运行：

```sh
python mosaico.py iris list
python mosaico.py recover
python mosaico.py iris app-update --project projects/<project>
python mosaico.py iris logs
```

- `iris list` 实时发现设备，并区分在线连接与离线缓存。
- `recover` 初始化或恢复设备，默认使用仓库内经过评审的基础包；实时显示基础包
  校验、设备检测、ESP-IDF 构建/烧录、镜像哈希校验、重连和 Recovery 就绪验证。
- `iris app-update` 构建并通过 ESP-Iris 安装普通应用；不会自动执行 `recover`。
- `iris app-update` 默认实时显示构建、Recovery 切换、传输进度、重连和固件校验阶段；
  `--json` 模式保持稳定机器输出，详细过程仍保存在运行日志中。
- `iris logs` 先显示保留日志，再持续跟随；按 `Ctrl+C` 正常结束。

Recovery 屏幕在 OTA 更新期间同时显示当前阶段、已传输/总容量、百分比与速率。
Bridge 使用 `Download`，USB/TCP 使用 `Receive`，NAND 使用 `Read`；速率单位
为 KiB/s 或 MiB/s，按约一秒内写入后端接受的有效负载计算，不包含传输协议开销。
首次采样、组件切换、重试及非传输阶段显示 `--`，停滞一个完整窗口后显示零速率。

System Update 的主进度按整包字节数加权，并另列当前组件进度及已校验组件数。
总大小未知时百分比显示 `--`。传输达到 100% 后仍需完成校验与提交；普通 OTA
成功后提示重启，失败页显示错误和最后传输进度，不能把传输完成当成应用健康。

### Download Ideas 的后台配对码

本次 Recovery 启动首次连接 Wi-Fi 并取得 IP 后，会后台注册一个 Bridge 会话。
首页保持可操作，后台只获取/缓存配对码，不轮询、上报 inventory 或执行下载更新。
打开 Download Ideas 后复用有效码并启用下载轮询；返回首页暂停轮询并保留会话，
再次进入继续使用同一码。明确点击 Cancel 或忘记 Wi-Fi 时会取消会话。

后台未配对的码按现有约 10 分钟期限失效后结束，不无限注册续期；之后点击下载页
重新申请。下载页仍打开时会自动续期未配对的码。注册失败最多尝试三次，保留现有
退避和服务端 Retry-After；失败后可退出页面再进入重试。自动预取每次启动只触发一次，
忘记 Wi-Fi 后重新配置可再次触发。

### Recovery 自更新（接受 ROM 兜底）

维护者可从当前 Recovery 构建生成只含一个 `recovery` 组件的专用包：

```sh
idf.py -C firmware/recovery build recovery-self-update-bundle
python mosaico.py iris system-update \
  --bundle firmware/recovery/build/factory-recovery-update.irisfw
```

制包器把 `factory.bin` 以 `0xff` 补齐至整个 1.75 MiB `factory` 槽。分区表不作为
组件进入包；制包时只把它的 SHA-256 写入顶层 `target_layout_sha256`，作为明确的
设备布局前置条件。设备先确认当前分区表哈希满足该条件，再在 PSRAM 中分配连续的
完整槽位、接收并校验 SHA-256 和 ESP32-S31 镜像。只有全部预检通过后，才会临时
关闭 dangerous write protection，原地擦写 `factory`，恢复保护，再执行整槽读回
SHA-256 和 `esp_image_verify()`。成功后将下一次启动明确指向 `factory`，保存
operation receipt，并延迟重启；Gateway 必须观察到相同 Device ID、新 Boot ID、
`recovery` mode、目标 project/ELF SHA-256 和 HEALTHY 才报告成功。

该路径不改 bootloader 或 Flash 布局，也不与 normal application/data 更新混包。
它仍是单副本原地更新：从开始擦除到完成校验之间掉电，可能导致 Recovery 无法启动，
此时需按仓库规定进入 ROM download mode；开发期间运行
`python mosaico.py recover --source current` 恢复本分支构建，发布后则使用已评审的
Recovery 包。不具备自更新能力的设备必须走 ROM/`recover` 路径。

### Recovery HTTPS Bridge

Recovery 主动通过 HTTPS 连接 Bridge 服务，支持 `partitions`、`layout`、
`factory`，并可在构建时启用 `system_update`。本仓库的 Recovery 源码构建默认连接
`https://iris-bridge.esp-claw.com`，并使用 `esp-mosaico` board ID。部署到其他
环境时，通过 `CONFIG_IRIS_FACTORY_BRIDGE_SERVER_URL`（无末尾斜杠的 HTTPS
Origin）和 `CONFIG_IRIS_FACTORY_BRIDGE_BOARD_ID` 覆盖这两个值；任一值为空都不注册。

首页主按钮为 **Download Ideas**，引导用户访问
<https://mosaico-ideas.espressif.com/> 并选择适合设备的应用。没有保存 Wi-Fi
配置时，点击主按钮先进入配网页；连接取得 IP 后自动继续下载流程。返回首页
会取消这次续接。普通 **Wi-Fi** 入口不会自动开启下载。

进入下载页面后，设备等待 Wi-Fi IP、注册并显示服务器配对码。
扫描页面二维码打开上述网站，在网页输入二维码下方的配对码。网址使用小字，
常规配对码使用 40px 大字；较长或包含其他 ASCII 字符的码使用 24px 完整显示。
网站可能要求在浏览器完成人机验证后才能配对。
网页配对并上传后，设备拉取任务、验证并写入；一次会话仅烧录一次。
退出会请求异步安全停止，完成或失败后重新进入页面才能再次配对。

Bridge 注册响应的 `control_protocol: 2` 启用独立授权：设备向 `/authorize`
提交 `PRECHECK` 获取计划及 `write_authorization_ttl_ms`，向同一接口提交
`COMMITTING` 获取明确的 `authorized: true` 后才提交。设备使用单调时钟检查
写入授权期限；普通 `/progress` 上报不授予写权限。授权响应不确定时不会自动重放。
旧服务继续使用原有 `/progress` 协议，在最终提交边界补齐必要阶段确认。

下载期间，PSRAM 栈后台任务合并组件进度，最多每秒发送一次；没有新进度时每
5 秒查询取消状态。暂时性网络/5xx 失败仅退避重试，不中断组件接收；明确取消、
401/404/410 或授权过期仍停止更新。重试从请求结束时计时并遵守 `Retry-After`。
退出或提交前会等待后台任务释放连接，避免旧请求跨越提交边界。
控制主任务、后台任务和文件接收各自拥有 HTTP 客户端；更换路径时更新 URL，完整
消费成功响应后复用 HTTPS 连接，错误、不完整响应或服务端关闭连接时丢弃句柄。

更新后端先将操作 ID 和结果持久化到 `sysmeta`，再保存待启动目标。最终结果由
只做网络操作的 PSRAM 栈任务回报，主任务最多等待 **2 秒**便重启；DNS/TLS 或
响应体阻塞不会延长等待，不强删持有 TLS 锁的任务。启动意图写入失败会更新本地
失败结果。下次 inventory 仍能提供 `last_operation_id/last_operation_state`；
云端最终响应丢失不触发自动重烧，也不等同于目标应用已经健康启动。

应用镜像按实际长度向上对齐 4 KiB 扇区后擦除，不再擦除整个应用分区；其余尾部
字节保留，镜像及读回校验不变。提供镜像的数据分区仍完整擦除，保持文件系统尾部语义。

远程布局更新允许省略数据分区镜像（包括 SPIFFS/FAT/LittleFS 等），适用于未使用
或由应用自行初始化的分区；可变应用分区仍必须提供并校验镜像。省略的数据分区
不会新增擦除或写入操作，但布局改变或其他镜像复用原地址时，旧数据不保证有效或
保留。网页上传端应列出缺失的非 NVS 数据镜像，并由作者确认后继续。
该行为需要 Recovery `0.1.2` 与配套 Bridge；旧设备固件仍可能拒绝更新。
Recovery 的 TLS 动态分配使用 PSRAM，证书校验保持启用。批量下载期间临时关闭
Wi-Fi 省电，退出或完成时恢复原模式；TCP 接收窗口为 65,535 字节，接收邮箱为
48 项，避免小窗口限制公网 HTTPS 吞吐。实际速率仍包含同步 Flash 写入耗时。

Recovery 的普通 `malloc` 优先使用 PSRAM，并保留 64 KiB 片内分配池。Wi-Fi/lwIP
可迁移的动态缓冲及 BSS、TLS、JSON、4 KiB 下载缓冲、旧布局模式的分区表缓冲、
更新组件计划、UI/NAND 快照与 ESP-Iris 日志环均使用 PSRAM；mDNS 和只读取消查询
任务也使用 PSRAM 栈。DMA 缓冲、RTOS 控制块及 Flash/NVS 写入任务栈仍留在片内，
不启用全局任务栈外移。Bridge 写入任务保留 20 KiB 片内栈：实测 16 KiB 栈仅剩
868 字节余量，因此增加 4 KiB 给 TLS 和异常路径。

日志记录各组件实际接收字节数、总耗时、HTTP 读取与后端写入累计耗时、最长单次
读取耗时及写入任务栈水位；总耗时还包括打开连接、擦除和校验，不能当作纯网络
速率。任务内存观察接口保持启用，可结合日志区分网络等待、Flash 写入和内存压力。

Recovery 支持 Gateway Web 工作台截图，以及 USB 会话下的交互输入。
触摸输入使用 Gateway 的 `0x1001/1` pointer RPC，交由 GSP 输入处理按下、移动和抬起；TCP 会话不能通过此接口操作 Recovery 界面。

```sh
python mosaico.py iris test recovery-wifi --ssid SSID
python mosaico.py iris test bridge-code --device-id DEVICE_ID --timeout 60
```

USB 命令打开同一个页面并等待配对码；重复打开不换码。控制服务 `0x1202` 的
方法 4 打开，方法 5 只查询状态，旧方法 3 已移除。服务地址、板型缺失时提示
重新配置构建。两个方法均拒绝 TCP，不返回或记录设备 token。

设备端实现位于 [iris_bridge 组件](components/iris_bridge/)。
旧本地 HTTP server、URL 下载 RPC、`http-update-code` 和 `--manifest-url`
已移除，USB bundle 和 NAND 更新继续使用共享写入后端。

### Recovery 从 NAND LittleFS 读取系统更新

ESP-Mosaico 的板载 NAND 与 `esp-mosaico-claw` 一致，使用 SPI NAND、wear-leveling
block device 和 LittleFS，挂载点为 `/nand`。Recovery 读写挂载已有文件系统，并将
整个挂载点注册为 ESP-Iris 文件卷 `nand`；可通过 Gateway 列目录、读取、写入、删除、
建目录和重命名。写入先落到同目录临时文件，校验 SHA-256 后再原子替换目标文件。
挂载失败时不会格式化 NAND，也不会阻止 Recovery USB 维护服务启动。将解包后的
bundle 放到同一目录，例如：

```text
/nand/system-update/manifest.json
/nand/system-update/ota_0.bin
/nand/system-update/bootloader.bin
/nand/system-update/partition-table.bin
```

Recovery 首页底部提供 **NAND update**：进入后固件会异步扫描以下两种
catalog 布局，最多列出 8 个完整 bundle；点击条目可先核对 release、组件数、总
容量和 manifest 路径，再确认更新。

```text
/nand/system-update/manifest.json
/nand/system-update/<release>/manifest.json
```

每个 `manifest.json` 引用的组件必须与它位于同一目录。扫描阶段会过滤 manifest
格式错误、组件缺失或文件大小不符的条目；确认安装后，System Update backend
仍会重新执行完整 manifest、SHA-256、镜像和布局校验。也可以通过产品 CLI 直接
启动指定路径：

```sh
python mosaico.py iris system-update --device-id DEVICE_ID \
  --manifest-path /nand/system-update/manifest.json
```

Recovery 逐块读取组件并复用与 USB、Bridge 相同的 manifest、SHA-256、镜像及
分区布局校验。v1 manifest 必须将 partition table 放在首个组件；Recovery 验证
当前表和目标表中的五个不可变分区后，按照目标表流式写入 application 和 data。
bootloader 暂存到 PSRAM，全部验证完成后统一提交。三种来源共用一个 Flash writer owner，不能
并行执行。可通过 `CONFIG_IRIS_FACTORY_NAND_SYSTEM_UPDATE_AUTO_START=y` 配置固定
路径自动启动；默认关闭，避免误用 NAND 中遗留的旧 bundle。启动 NAND 更新后不要
再通过文件服务修改该 bundle；单文件上传是原子的，但一个 bundle 的多个文件不构成
同一事务。

## Flash 布局

16 MiB Flash 使用固定系统前缀和尾部可缩减的单应用槽：

| 分区 | Offset | Size | 用途 |
| --- | ---: | ---: | --- |
| `otadata` | `0x9000` | 8 KiB | ESP-IDF OTA 选择与回滚状态 |
| `phy_init` | `0xb000` | 4 KiB | PHY 初始化数据 |
| `sysmeta` | `0xc000` | 80 KiB | 系统专用 NVS |
| `factory` | `0x20000` | 1.75 MiB | 保留 Recovery |
| `coredump` | `0x1e0000` | 128 KiB | 崩溃证据预留空间 |
| `nvs` | `0x200000` | 64 KiB | 应用 NVS |
| `ota_0` | `0x210000` | 13.94 MiB | 普通应用；后续布局可从尾部缩减 |

固定系统前缀从 Flash 起始到 `coredump` 结束恰好为 2 MiB，
`nvs` 和普通应用从 `0x200000` 之后开始。

`sysmeta` 中的 `esp_iris`、`wifi`、`iris_ota_demo` 和 `update` namespace
分别保存 TCP pairing token、Factory Wi-Fi、Recovery OTA 状态和
最后一次系统更新结果。System Update v1 只要求 `otadata`、`phy_init`、
`sysmeta`、`factory` 和 `coredump` 的名称、类型、子类型、offset、size 与 flags
严格符合上表；`nvs`、`ota_0` 以及其他应用数据分区可由目标表调整。

常用选项可通过 `python mosaico.py <command> --help` 查看。自动化环境可加
`--json`；`recover` 在唯一识别到受支持设备后直接执行，无需二次确认。

## 工程维护者

普通用户不应直接调用底层构建或写入命令。Recovery 基础包和内部写入 target
由 `mosaico.py recover` 管理；普通应用始终由 `mosaico.py iris app-update` 通过
ESP-Iris 安装。评审包包含完整哈希与布局约束，只有通过构建校验和真机验收后
才应发布。

Recovery 工程直接使用 workspace 中的 `ESP-Iris`。BSP 依赖由组件
manifest 指向 `https://github.com/esp-mosaico/esp-mosaico-bsp`；集成到
ESP-Mosaico workspace 时，`mosaico.py recover` 会根据宿主 workspace 的
`.mosaico.json` 注入本地 `esp-mosaico-bsp`，并复用 workspace 中的 ESP-Iris，
保证 Recovery、设备工具与普通应用使用同一 Iris 版本。


## Vibe Mode 界面与构建

用户可见名称为 **Vibe Mode**；工程 `factory`、协议角色 `recovery`、
`recover` 命令和 Recovery ABI 1 保持不变；当前固件版本为 `0.1.2`。
九个页面由 `ui/main.json` 和可移植的 `main/vibe_ui.c` 实现，PC 与设备共享
同一控制器及 `ui/profile.yaml` RGB565 编译配置。
`main/factory_ui.c` 适配网络、Bridge、NAND 和更新状态；
异步状态在 GSP 渲染上下文汇总，外部打开下载页的请求通过队列提交。

使用 Component Registry 的 **ESP-GSP 1.4.0**，BSP 开启硬件显示但关闭
`CONFIG_BSP_DISPLAY_LVGL_ENABLE`。该 BSP 选项默认开启，保留现有 LVGL
应用行为。构建必须使用包含该选项的 workspace BSP；旧版 BSP 不支持此模式。
GSP 1.4 在调用方同步创建、校验 UI，Recovery 将
`CONFIG_ESP_MAIN_TASK_STACK_SIZE` 设为 20480；仅增加渲染任务栈不能覆盖该阶段。
启动日志记录 UI 初始化后的主任务栈余量，便于检查后续资源改动。

场景、字形与键盘图标全部嵌入 Recovery，不依赖应用资源分区。
GSPB 在构建时无损 Deflate 压缩，启动时使用 ROM 解压器还原到 PSRAM；
GSP 继续执行原始 bundle 校验。界面仅使用预烘焙字形，不启用运行时字体；
通过 GSP 1.4 的 `CONFIG_ESP_GSP_ENABLE_JPEG=n` 排除 JPEG 解码器，
现有图标仍为编译后的 RGB565_A8。32 px 更新标题与百分比只打包对应状态文案
和数字所需的字形，网络名称与密码仍保留完整的既有字符集。
若增加运行时字体或 JPEG 图片，必须同步启用相应能力并重新验证大小和功能。
原生键盘支持大小写、符号、删除、确认、密码显示切换和 64 字节上限，
离开输入页会清理密码。

在宿主 workspace 中启动共享控制器的模拟器：

```sh
python3 tools/gsp-sim/run.py submodule/esp-mosaico-utils/esp-mosaico-recovery/firmware/recovery/ui/main.json --headless
```

运行 Recovery 主机测试前，设置 `GSPC_EXECUTABLE`（0.5.0）和
`GSP_SIM_EXECUTABLE`（1.4.0），安装 `tests/requirements-ui.txt` 中的主机依赖。
原生模拟器测试使用真实键盘、列表和回调，
并检查 Wi-Fi 列表的截图像素，覆盖字形完整性、卡片间距和反复导航。
二维码由独立解码器直接从渲染截图验证；OTA 使用确定性的时间与字节数序列，
覆盖组件切换、停滞、重试、新任务、未知大小及传输完成后的提交失败。
服务数据由 PC 后端模拟；模拟通过不代表真机网络或更新验收通过。

网站二维码是提交到 `ui/assets/ideas-qr.png` 的静态资源，无设备端编码库。
需要重新生成时，在主机安装 `tools/requirements-qr.txt`，运行
`python tools/generate_ideas_qr.py`。脚本固定完整 HTTPS URL、QR version 3、
M 级纠错、四模块白边和 5 倍整数缩放，生成 185×185 PNG；普通构建直接使用
该文件，经现有 GSPB 与 Zopfli 压缩链路嵌入固件。

固定 Recovery 槽仍为 `0x20000` / `0x1c0000`。须核对实际构建目录中的
`factory.bin` 大小；不能仅凭 IDF 构建成功判断镜像适合较小的 factory 槽。
预编译包只在真机验收后通过 `update-recovery-prebuilt` 整体更新。
