# {{PROJECT_NAME}} Agent Instructions

本仓库是 Flutter App 项目。共享 Dart 代码、平台工程和数据契约必须分别确认影响范围。

## 开始前

1. 定义完成标准、目标平台和验证设备/构建目标。
2. 从 `docs/README.md` 或模块文档确认页面流程和业务状态。
3. 先读 `pubspec.yaml`、`analysis_options.yaml` 与现有目录结构，不假设状态管理方案。

## Agent 文档路由

- `agent_docs/ARCH.md`：feature、widget、state、service 与平台边界。
- `agent_docs/DATA_CONTRACT.md`：接口模型、本地存储、序列化和敏感数据。
- `agent_docs/BUILD.md`：Flutter SDK、依赖、构建目标和标准脚本。
- `agent_docs/TEST.md`：unit、widget、integration 与平台验证。
- `agent_docs/DEV_TASK_EXECUTION.md`：Dev Task、分支、worktree 与状态合同。

签名、发布和真实设备凭证不由 onboarding 模板生成。
