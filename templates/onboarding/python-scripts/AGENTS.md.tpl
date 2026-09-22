# {{PROJECT_NAME}} Agent Instructions

本仓库是 Python 脚本项目。脚本入口、依赖、副作用和退出码必须可追踪、可测试。

## 开始前

1. 定义完成标准、输入输出、副作用和验证命令。
2. 读取 `pyproject.toml`、现有入口和相关 `docs/*`，不假设部署形态。
3. 明确文件、网络、数据库和子进程边界；默认测试不得连接生产或共享服务。

## Agent 文档路由

- `agent_docs/ARCH.md`：模块、入口、依赖与副作用边界。
- `agent_docs/SCRIPT_ENGINEERING.md`：CLI、配置、幂等、日志和退出码。
- `agent_docs/BUILD.md`：Python 环境、依赖和质量脚本。
- `agent_docs/TEST.md`：单元、CLI、fixture 与外部依赖隔离。
- `agent_docs/DEV_TASK_EXECUTION.md`：Dev Task、分支、worktree 与状态合同。

真实凭证、生产地址和个人环境路径不得写入模板、fixture 或日志。
