# 元数据、README 与版本

## 本地文件与包内内容

`mosaico-ideas.json` 位于应用根目录，schema 固定为
`mosaico-ideas/project-upload/v1`。这是本地上传配置，不是 `.irisfw` 的内部 manifest。
已废弃的预发布配置文件名和 schema 不再是输入别名；迁移现有配置时保留字段值，
仅在确认其结构一致后改名/改 schema，不覆盖其他本地文件。

字段用途：

| 字段 | 内容与核实依据 |
| --- | --- |
| `project.title` | 从应用实际用途拟定，1–200 字符 |
| `project.categories` | 至少一项；只可选 `smart-home`、`sensors`、`display`、`audio`、`communication`、`motor` |
| `project.boards` | 可选 `camera`、`ethernet`、`battery`，不是芯片型号列表 |
| `project.tags` | 可选标签，最多 20 项，每项 1–50 字符，不重复 |
| `project.license` | 以项目及资源真实许可为准；未知时不要默认填 MIT |
| `project.external_links` | 最多 10 项，含 `label`、HTTP(S) `url`、`platform`；平台值为 `github`、`oshwhub`、`makerworld`、`other` |
| `content.readme` | 必需，应用根目录相对 Markdown 路径 |
| `content.covers` | 最多 9 张 PNG/JPEG/WebP/GIF 图片；只引用已有、可使用的图片 |
| `content.resources` | README 中引用的外置资料，不是 Flash 数据镜像 |
| `content.attachments` | 最多 20 个独立下载附件 |
| `release.changelog` | 本次变更摘要，最多 10,000 字符；不是整个历史变更记录 |

分类最多 6 项、boards 最多 2 项。字段最终以随仓库提供的 JSON schema 和
`platform_manifest.py` 为准；不增加该 schema 不支持的字段。

README 至少说明应用做什么、所需硬件及连接、操作方法、配置条件、已知限制与
资源许可；按项目情况组织，不强制无关章节。区分实际验证结果与预期支持。
必要的封面/截图缺失时说明缺项；生成新素材需有相应工具及任务授权。

## 路径和资源校验

- 路径使用应用目录内的相对路径；拒绝绝对路径、`..`、反斜杠和逃逸符号链接。
- 同一文件不能在 covers/resources/attachments 中重复声明；但 README 可以引用
  已声明的封面或附件。README 不能再次作为资源文件声明。
- Markdown 内联链接/图片和引用式定义中的本地文件会自动发现、上传并改写 URL；
  `content.resources` 中显式列出的文件必须在 README 中引用，独立资料用 attachments。
- 空格用 `%20` 或尖括号路径，括号应转义为百分号编码。代码块和外部 HTTP(S) 链接
  不改写；HTML 资源链接不会自动改写，不依赖它们上传本地文件。
- manifest 最多 64 KiB；README 为 UTF-8，最多 100,000 字符且不超过 400 KiB。
  图片单文件最多 10 MiB，其余资料最多 20 MiB；资料总计最多 100 文件/100 MiB。
  所有待上传文件必须非空。前端发布流程还可能有封面等要求，不把 CLI 接受等同于可发布。
- `.irisfw` 最多 32 MiB，上传为固件，不把需要烧入 Flash 的资源仅作为网页附件上传。

可直接复用 CLI 的本地校验，不请求服务器。将 `TOOLS_ROOT`、`PROJECT` 设为已核实的
绝对路径，并使用能运行当前 CLI 的 Python：

```bash
PYTHONPATH="$TOOLS_ROOT/tools" python -c 'import sys; from pathlib import Path; from mosaico_cli.platform_manifest import load_manifest; load_manifest(Path(sys.argv[1])); print("metadata valid")' "$PROJECT"
```

不通过打印整个配置或环境来调试凭据问题。

## 版本的三个来源

应用版本来源是构建产物 `build/project_description.json` 的 `project_version`、
包内 manifest 的 `release` 字符串，以及上传的 `--version`。已提供的来源必须完全一致，
不自动去掉 `v` 前缀或做语义归一化。外置 `mosaico-ideas.json` 的 `release` 对象只放
`changelog`，**没有 `version` 字段**。

版本为 1–80 个字符，以字母/数字开头，仅含字母、数字、`.`、`_`、`+`、`-`；
包内 release 另有 64 字符上限。若使用无 release 的现有包，须明确提供 `--version`。
修改版本应在项目的真实版本来源完成，然后重新构建；不能只改 ZIP manifest 的版本，
让应用镜像内版本、构建描述与发布说明互相矛盾。
