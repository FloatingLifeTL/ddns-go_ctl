# Contributing

## 项目边界

`ddns-go_ctl` 是面向 DDNS-GO 的外部 Windows 管理工具，不是 DDNS-GO 的源码 Fork。贡献者应保持以下边界：

- 不复制、修改或打包 DDNS-GO 源码。
- 不提交 `ddns-go.exe`。
- 不提交本机绝对路径、用户配置、日志、token、API Key 或其他敏感数据。
- 不假设 DDNS-GO 的特定版本。

## 维护与决策

本仓库由主发布者 `FloatyTFL`（GitHub `FloatingLifeTL`）拥有并维护。主发布者保留最终的修改、审核、合并、回滚和发布决定权。

## 开发环境

- Windows x86_64。
- CPython 3.11 或更高版本。
- 只使用 Python 标准库，不引入第三方运行时依赖。

## 提交前检查

1. 阅读 `README.md`、`docs/project.md`，并阅读脚本顶部 `DDNS_GO_PARAMS`、`MENU_KEY_MAPPINGS` 和 `LOG_VIEW_KEY_MAPPINGS`。
2. 保持功能、文档和脚本顶部配置区一致。
3. 新增或修改用户可见行为时，补充对应测试。
4. 运行全部测试：

```bat
python tests\test_ddns_go_ctl.py
```

5. 检查 `.gitignore`，确保构建产物、日志、可执行文件和本地路径不会进入公开副本。
6. 用户可见变化同步更新 `docs/releases/v<version>.md`；发布 EXE 时同时记录附件大小、SHA-256、Python/PyInstaller 版本和是否代码签名。
7. 更新 `docs/THIRD_PARTY_NOTICES.md`，如果外部依赖范围发生变化。

## 提交说明

建议使用清晰的动词和变更范围，例如：

```text
fix: 修正端口占用时的状态显示
feat: 增加日志清理保留份数配置
docs: 补充公开副本的运行时依赖声明
```

## 许可证

提交贡献前，表示你同意将贡献按仓库 `LICENSE` 的 MIT 条款发布。上游 DDNS-GO 的许可和版权信息见 `docs/THIRD_PARTY_NOTICES.md`。
