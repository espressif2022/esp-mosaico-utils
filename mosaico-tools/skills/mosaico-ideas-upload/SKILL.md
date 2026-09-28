---
name: mosaico-ideas-upload
description: "Prepare Mosaico Ideas application metadata, build and inspect unsigned Iris firmware bundles, and create or update application drafts with the ESP-Mosaico CLI. Use for 元数据拟定、Iris 打包、项目上传、版本更新 and failed-upload recovery; not for device flashing or Recovery firmware releases."
---

# Mosaico Ideas 项目上传与版本更新

把消费工作区中的应用整理成可复现的 `.irisfw` 与外置元数据，并按用户确认的
服务器、账号和目标应用创建或更新 **草稿**。不要把“上传成功”表述为“已公开发布”。

## 先确定本次范围

- 只要求拟定元数据：检查项目后编写本地文件，不登录、不上传。
- 只要求打包：构建并校验产物，不操作云端或设备。
- 要求上传或更新：确认目标服务器、账号、版本以及创建/更新选择；已有明确授权
  不必重复确认，但缺失的目标 UUID、覆盖草稿或缺失数据镜像风险需要澄清。
- 这个 skill 不授权提交审核、撤回审核、正式发布、删除项目、设备烧录或升级 Recovery。
  不为完成上传而执行 `recover`、`install`、`iris app-update` 或 `iris system-update`。

## 定位工作区和工具

1. 读取当前项目及父目录的 `AGENTS.md`，检查 Git 状态，保留已有修改。
2. 定位应用目录、消费工作区根目录的 `mosaico.py` 和 `.mosaico.json`。
   应用只是被复制出来、缺少父级 CMake/SDK 时，索取完整工作区，不拼凑依赖。
3. 从工作区配置解析工具与 ESP-Iris 路径，使用该工作区固定的 utils/SDK 版本。
   `esp-mosaico-utils` 自身不是待上传应用；Recovery 项目也不是应用。
4. 在消费工作区根目录核对 `python mosaico.py project upload --help` 和
   `python mosaico.py account --help`。找不到根 launcher 时，可使用已定位的
   `mosaico-tools/mosaico.py --workspace "$WORKSPACE" ...`，不要猜测其他命令别名。

本 skill 在仓库中随 `mosaico-tools` 发布；实现和现有 schema 是事实来源：

- `mosaico-tools/mosaico-ideas.schema.json`
- `mosaico-tools/tools/mosaico_cli/platform_manifest.py`
- `mosaico-tools/tools/mosaico_cli/platform_upload.py`
- `mosaico-tools/tools/mosaico_cli/platform_account.py`、`platform_client.py`

若 skill 被复制到别处，按上述仓库相对路径重新定位，不依赖开发者的绝对路径。

## 执行流程

### 1. 拟定元数据和版本

读 [元数据规则](references/metadata.md)。先核实应用实际功能、硬件、操作方式、
资源来源与许可，再拟定标题、简介、README、分类、封面及本次 changelog。
不虚构测试结果、兼容板型、功能或开源许可证；无法确认的发布信息向用户询问。

应用目录已有 `mosaico-ideas.json` 时增量编辑；没有时参考
[最小模板](assets/mosaico-ideas.json)。模板中的空标题和分类必须补齐。
不把平台应用 UUID、账号信息、Header ID 或凭据写入 manifest/固件。

### 2. 打包并检查

读 [打包和镜像检查](references/bundle.md)。检查 `system-update-bundle` 目标、
构建版本、包内 release、组件及分区覆盖关系。默认上传会重新构建，只有确认
既有产物就是本次版本时，才用 `--skip-build` 或 `--bundle`。

特别区分网页资料和设备资源：README/封面/附件放在包外；需要写入 Flash 的
资源镜像属于包内 `data` 组件。资源若已经嵌入应用二进制，不要重复打包。

### 3. 登录、创建或更新

只有本次任务包括云端操作时，读 [上传、更新和重试](references/upload.md)。
使用确认过的服务器和浏览器设备授权流程；保持凭据私密。

创建与更新必须显式选择。更新使用服务端 UUID，并核对账号名下的应用标题、
当前版本、可更新状态和现有草稿。**不要凭本地目录名或同名标题推断 UUID。**

只有已获授权的无人值守操作才加 `--yes`；它同时确认创建/更新及覆盖已有草稿。
`--json` 不是 dry-run，CLI 当前没有上传 dry-run 参数。

### 4. 核对结果并交接

核对返回的 `application_id`、`revision_id`、`version`、`bundle_sha256`、
`status: "draft"` 和 `draft_url`；服务端项目并发版本不等于应用 release。
报告本地修改、构建/校验结果、所选创建或更新目标，以及草稿链接。
没有做设备测试就明确说明。需要审核或公开发布时，仅说明下一步，除非用户另有授权。

失败时保留原产物和重试状态，按上传参考排查；不靠换应用、删除记录或重复创建
绕过失败。未获授权，不提交代码、不推送仓库、不改生产配置。
