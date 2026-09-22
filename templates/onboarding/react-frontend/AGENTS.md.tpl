# {{PROJECT_NAME}} Agent Instructions

本仓库固定使用 React 18 + Umi Max 4 + Ant Design 5/ProComponents。页面行为、数据契约和质量门禁分别由对应文档承载。

## 开始前

1. 定义完成标准、受影响路由/组件和可重复验证方式。
2. 从 `docs/README.md` 或模块文档确认产品行为、状态和接口口径。
3. 检查 `package.json`、锁文件和既有组件模式，不替换项目已选择的工具链。

## Agent 文档路由

- `agent_docs/ARCH.md`：路由、页面、组件、状态和依赖边界。
- `agent_docs/DATA_CONTRACT.md`：API、DTO/View Model、空值和敏感数据。
- `agent_docs/BUILD.md`：包管理器、开发、构建和质量脚本。
- `agent_docs/TEST.md`：单元、组件、集成和浏览器验收。
- `agent_docs/DEV_TASK_EXECUTION.md`：Dev Task、分支、worktree 与状态合同。

项目专用 UI 场景、selector、mock 和框架插件配置保留在项目文档与脚本中，不进入通用入口。不得用 mock 页面冒充真实验收。
