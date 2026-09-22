# {{PROJECT_NAME}} Agent Instructions

本仓库是多仓项目组入口，只维护跨仓边界、统一命令和任务路由，不复制子仓内部工程规范。

## 开始前

1. 定义本轮完成标准和验证证据。
2. 确认任务是当前仓协调工作，还是带显式 `targets[]` 的子仓执行。
3. 只读取本轮涉及的子仓文档；不要默认扫描或修改所有子仓。

## Agent 文档路由

- `agent_docs/PROJECT_GROUP.md`：子仓登记、职责和跨仓依赖。
- `agent_docs/ARCH.md`：项目组边界与依赖方向。
- `agent_docs/BUILD.md`：聚合命令和子仓命令映射。
- `agent_docs/TEST.md`：跨仓验证和失败定位。
- `agent_docs/DEV_TASK_EXECUTION.md`：Dev Task、Target、分支与状态合同。

长期产品事实仍写入对应项目的 `docs/*`；本文件不承载子仓业务规则。
