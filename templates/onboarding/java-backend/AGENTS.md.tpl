# {{PROJECT_NAME}} Agent Instructions

本仓库是 Java 后端项目，构建工具由仓库中的 Maven 或 Gradle 证据决定。

## 开始前

1. 定义完成标准、影响模块和验证命令。
2. 从 `docs/README.md` 或模块文档定位业务合同。
3. 优先使用项目提交的 `mvnw` 或 `gradlew`，不混用两套依赖与生命周期。

## Agent 文档路由

- `agent_docs/ARCH.md`：模块、包、应用层和适配器边界。
- `agent_docs/DATA_ENGINEERING.md`：持久化、事务、迁移和序列化边界。
- `agent_docs/BUILD.md`：JDK、Maven/Gradle Wrapper 和质量脚本。
- `agent_docs/TEST.md`：单元、切片、集成与容器测试。
- `agent_docs/DEV_TASK_EXECUTION.md`：Dev Task、分支、worktree 与状态合同。

格式化和静态检查只调用构建文件已声明的插件或 task，不凭空注入框架。
