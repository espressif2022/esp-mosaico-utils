# 登录、首次上传、版本更新与失败恢复

以下示例在消费工作区根目录执行。`PROJECT` 为应用绝对路径，`IDEAS_SERVER`
为用户确认的 HTTPS Origin，`BUNDLE` 为已检查的 `.irisfw` 绝对路径。
不得把示例域名当成真实上传目标；仅 localhost 开发允许 HTTP。

## 账号

```bash
python mosaico.py account status --server "$IDEAS_SERVER"
python mosaico.py account login --server "$IDEAS_SERVER" --open-browser
```

先查看状态，只在确需登录且用户授权时发起设备授权。用户自己在浏览器确认授权；
不代替用户批准设备码。CI 没有浏览器时通过既有安全凭据通道提供 token，不将 token
写入命令行、源码、manifest、README、提交或答复，不打印整个环境。

环境变量仍为 `MAKER_SPARK_SERVER` 和 `MAKER_SPARK_TOKEN`，不能擅自改成新拼写。
环境 token 优先于保存的 token；若旧环境 token 导致登录后仍未认证，应检查其是否
仍覆盖新保存的凭据，不能自动撤销或删除用户的其他凭据。
私有状态位于 `state_root("esp-mosaico")/mosaico-ideas/<server-hash>/`，CLI 会迁移
旧目录；不要手工搬动或清空它来重试。
`account logout` 会撤销选中的 token，不是只读诊断命令。

## 首次创建

交互环境可以让 CLI 提示创建/更新选择：

```bash
python mosaico.py project upload --project "$PROJECT" --server "$IDEAS_SERVER"
```

若用户已经明确要新建，显式指定 `--create`。它会创建新的服务端应用，不能用于
更新现有 UUID。无人值守示例仅在已确认服务器、账号、版本和创建动作后使用：

```bash
python mosaico.py project upload --project "$PROJECT" --server "$IDEAS_SERVER" --create --yes --json
```

该命令默认先构建。`--yes` 确认实际云端变更，不是安全检查的替代品。

## 更新已有应用

交互上传选择更新后，CLI 从 `/firmware-projects/upload-targets` 读取当前账号名下
可选应用，列出标题、当前版本、状态、完整 UUID 和阻塞原因。
用户选择目标；有多个同名应用时必须核对完整 UUID。若已有 UUID，仍核对所有权与
`can_update`。已有草稿可能被替换，应在继续前明确告知用户。

```bash
python mosaico.py project upload --project "$PROJECT" --server "$IDEAS_SERVER" --update "$APPLICATION_ID"
```

无人值守时同时使用 `--update "$APPLICATION_ID" --yes --json`。
`--create` 与 `--update` 互斥；非交互或 JSON 模式必须明确指定其中之一。
不要因应用受审核状态限制而自动撤回审核、换成新建或选择别的应用。

## 使用已检查的产物

```bash
python mosaico.py project upload --project "$PROJECT" --server "$IDEAS_SERVER" --skip-build --update "$APPLICATION_ID" --yes --json
python mosaico.py project upload --project "$PROJECT" --server "$IDEAS_SERVER" --bundle "$BUNDLE" --update "$APPLICATION_ID" --yes --json
```

第一种复用 `build/project_description.json` 和同目录按项目名命名的包；第二种直接
使用指定包，不构建，也不自动读取 build 版本。两种参数不能同时使用。
包内缺少 release 时追加 `--version "$RELEASE_VERSION"`；已有 release 时该参数
必须与它完全一致。两种方式仍需要应用目录和 `mosaico-ideas.json`。

## 结果与重试

成功意味着创建/更新草稿，记录 `application_id`、`revision_id`、`version`、
`bundle_sha256`、`status`、`draft_url`。核对版本、目标 UUID 与包 SHA-256，
不以进程退出码之外的单条“已上传”日志断言整体成功。

- 网络中断：保留原包、元数据、资源文件和私有 ledger。CLI 已有有限重试与幂等键，
  恢复时保持服务器/账号/目标及内容不变；必要时复用原包，避免重新构建改变指纹。
- 最终结果不确定（如丢失成功响应/输出）：先检查对应应用和草稿，尤其是创建操作。
  不盲目再次 `--create`；完成后 ledger 会清理，再次新建可能产生另一个应用。
- 资源在校验后变化：确认最终文件内容后重新校验，不篡改 ledger 里的哈希。
- 版本冲突、不可更新、权限不足：重新读取状态并停下说明，不强制覆盖或绕过所有权。
- 限流/服务故障：遵循重试提示，最多追加一次受控的人工重试；同样失败就报告诊断
  和下一步，避免无限重试。登录过期先处理凭据，不删除重试记录。
- 保留必要的错误码和 request ID；清理诊断中的 token、设备授权秘密和签名存储 URL。

交接时说明哪些检查完成、是否仍有缺失数据镜像风险、哪些尚未真机验证，并提供草稿链接。
用户只要求准备或打包时，交接本地文件、版本和 SHA-256，到此停止，不进入云端流程。
