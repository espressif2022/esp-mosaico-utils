# Iris 打包与分区覆盖检查

## 构建入口

确认应用或其消费工作区提供 `system-update-bundle` CMake 目标。它不由上传 CLI
凭空创建；追踪项目 `CMakeLists.txt` 引入的工作区脚本及资源 manifest 生成规则。
例如相对路径引用的父级 `cmake/system_update.cmake` 缺失时，应找回完整工作区。

执行 `project upload` 且不带 `--skip-build` / `--bundle` 时，CLI 会：

1. 准备固定版本 ESP-Iris 的宿主工具环境，但不启动 Gateway。
2. 调用 ESP-IDF 的 `system-update-bundle`，传入 `ESP_IRIS_PYTHON`。
3. 从 `build/project_description.json` 读取项目名和版本，并使用
   `build/<project_name>-system-update.irisfw`。

只需构建包而不上传时，在已激活且兼容的 ESP-IDF 环境使用已有目标。例如：

```bash
python "$IDF_PATH/tools/idf.py" -C "$PROJECT" -B "$PROJECT/build" \
  -D "ESP_IRIS_PYTHON=$IRIS_PYTHON" system-update-bundle
```

`PROJECT`、`IDF_PATH`、`IRIS_PYTHON` 必须是从本工作区核实的路径；`IRIS_PYTHON`
指向满足固定 ESP-Iris 宿主依赖的 Python，不能随意指向缺依赖的系统 Python。
检查工作区自己的构建包装器；若有等效的只构建入口，也可复用。
不把 `iris system-update` 当成纯打包命令，它包含设备更新。

## 包的检查

ESP-Iris 的公开本地检查入口是：

```bash
"$IRIS_PYTHON" "$IRIS_ROOT/components/esp_iris/tools/esp_iris.py" bundle --json inspect "$BUNDLE"
```

`IRIS_ROOT` 从工作区配置解析。这个子命令只检查本地包；不要切换到 Gateway
或设备命令。记录完整包 SHA-256、大小、release、minimum_recovery_version 及
组件种类/目标地址/大小。平台目前接受：

- 无签名 `.irisfw`，内部 schema 为 `esp-iris-system-update/v1`；不添加签名或密钥。
- ESP32-S31，`chip_id = 32`，Flash 容量与当前平台 Header 和目标设备相容。
- 恰好一个 `application`，可附带 `partition_table` 和 `data`；flags 为 0。
- 不含 `bootloader`、`recovery` 或平台身份；Header 由平台统一选择。

包内 `components` 明确列出每个镜像，打包器不会因为分区表有某个分区就自动生成
镜像。核对镜像存在、长度和 SHA-256；分区表组件应覆盖 4 KiB 扇区，其 hash 与
`target_layout_sha256` 一致。平台/Bridge 仍做权威校验，本地通过不代表服务器必定接受。

## 资源究竟是否需要独立镜像

先追踪 CMake 和运行时代码，再决定处理方式：

- C 数组、`EMBED_FILES` 或 `gsp_add_bundle()` 嵌入应用的资源，通常已经在应用镜像内。
- 运行时从文件系统/Flash 分区读取的预置资源，要有对应镜像构建规则，并在包内声明
  `data` 组件；只上传 `content.resources` 或附件不能让设备获得这些字节。
- 只在分区表预留、暂未使用或由应用初始化的分区，可以没有初始镜像。
  不为消除警告而擅自删除分区、生成全 `0xff` 镜像或改成 NVS。

含分区表的包，逐项比较目标表与 application/data 的目标地址；只将类型匹配且
大小不超分区的组件视为覆盖。可用工作区已有解析器或 ESP-IDF
`components/partition_table/gen_esp32part.py` 检查二进制表。读取 ZIP 时限制解压大小，
只取 manifest 指定的所需文件，不对不可信归档直接 `extractall`。

对 `offset >= 0x200000`、type 为 data、subtype 非 NVS (`0x02`) 且没有 data 镜像的
分区，列出名称、地址、容量和用途。说明：空中更新不会为省略分区单独写入/清空；
布局改变或其他镜像复用旧地址时，旧数据可能无效或被覆盖。有线拼合 BIN 也不能作为
数据保留保证。核实应用无需预置数据或能自行初始化，并在上传前取得用户确认。

该场景要求支持此策略的 Bridge 和 Recovery；utils 中从 Recovery `0.1.2` 支持。
旧设备即便能有线运行，也可能拒绝空中布局更新。网页上传已有确认提示，**当前 CLI
没有同等的分区缺项确认**，不能把 `--yes` 当成用户已知晓这个风险。
依赖这项能力时建议在包的兼容要求中声明至少 `0.1.2`，先核实工作区如何生成该字段。
不要为解决兼容问题自动升级用户设备。应用分区镜像与受保护区域的规则仍须满足。
