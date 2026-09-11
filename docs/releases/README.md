# Release Note Layout

本目录用于保存可直接用于 GitHub Releases 的发布说明正文及其仓库内备份。

这里的 Markdown 文件只保存发布说明正文，不备份 GitHub Releases 的 EXE、
校验清单等附件。已发布版本、正文和下载附件以 GitHub Releases 页面为准。

## 规则

- 当前版本说明固定为 `docs/releases/v<version>.md`。
- 历史版本说明放在 `docs/releases/history/v<version>.md`。
- 文件第一行使用 `# v<version>` 记录完整版本标识。
- 文件名只用于定位文件，不要求承载全部版本信息。
- 不使用 YAML front matter 作为版本元数据。
- `docs/releases/README.md` 只描述目录规范，不作为某个版本的 Release 正文。
- 发布说明与 GitHub Release 正文保持一致。
- 发布说明只声明资产名和校验清单是否随附；资产大小、构建信息和 SHA-256 由发布页或 `SHA256SUMS.txt` 承载，不写入发布说明。
- 上一条自 `v2.7.1` 起适用，更早版本的历史说明保留内联 SHA-256，以免与已发布的 Release 正文不一致。

## 校验与提取

```powershell
./scripts/release-notes.ps1 -Check
# 将 v<version> 替换为需要提取的实际版本号
./scripts/release-notes.ps1 -Body -Version v<version>
```

该脚本只读取文件并输出正文，不修改文件，不创建或推送 Release。

发布产物的 `SHA256SUMS.txt` 由 `./scripts/checksums.ps1` 生成与校验，规范见 `AGENTS.md` 的「发布产物与校验清单」。

## 文件布局

- 当前维护中的版本说明：`docs/releases/v<version>.md`
- 历史版本说明：`docs/releases/history/v<version>.md`
- 文件第一行使用 `# v<version>` 记录对应版本。
