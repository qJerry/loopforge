# {{PROJECT_NAME}} Agent Instructions

## 这是什么仓库

本仓库是 Go 后端项目。本文件只维护仓库入口、阅读路由和必须遵守的 Agent 约束；业务能力、字段和协议事实应写入对应模块文档。

开始工作前先确认当前仓库已经存在的运行入口、包结构、schema、配置和生成命令。模板只提供通用边界，不得把示例目录当成已落地事实。

## 文档入口

- `docs/README.md`：存在时作为产品模块地图；模块长期文档使用 `docs/<module>/requirements.md|design.md|specs.md`。
- `agent_docs/ARCH.md`：Go 分层、包职责、依赖方向和 adapter 边界。
- `agent_docs/DATA_ENGINEERING.md`：schema、迁移、repository、事务、数据不变量和生成代码。
- `agent_docs/BUILD.md`：Go 工具链、标准脚本、配置、构建与 CI 门禁。
- `agent_docs/TEST.md`：测试分层、TDD、fixture、mock、失败路径和完成门槛。
- `agent_docs/DEV_TASK_EXECUTION.md`：Dev Task、文档回填、分支、worktree、Target 和状态写入边界。
- 当前任务的 PRD、设计和实施计划：只约束当前任务范围，不替代长期工程规范。

不存在某个可选文档时，不要伪造其内容；依据已确认代码和任务合同推进，并把需要长期保留的新事实补到正确位置。

## 开始前必须做

1. 定义完成标准：列出本轮行为、文件、边界和验证证据。
2. 确认任务范围、允许修改的模块以及不在本轮处理的内容。
3. 按任务职责阅读最少必要文档，不因目录存在就一次加载全部文档。
4. 检查当前 Git 状态，保留用户和其他会话已有改动，不覆盖无关文件。
5. 先确认包边界、数据边界、生成代码边界和验证命令，再编辑实现。

## 渐进式阅读

- 新需求、业务规则和验收：读对应模块 `requirements.md`。
- 流程、状态、职责、数据流和依赖边界：读对应模块 `design.md`，跨模块时再追加系统设计。
- HTTP/RPC 字段、错误、分页、权限和审计：读对应模块 `specs.md`。
- 分层、目录、接口或 adapter：读 `agent_docs/ARCH.md`。
- DDL、迁移、repository、事务或生成模型：读 `agent_docs/DATA_ENGINEERING.md` 和实际 schema。
- 构建、依赖、工具、配置或运行入口：读 `agent_docs/BUILD.md` 和对应脚本。
- 测试、fixture、mock、build tag 或质量门槛：读 `agent_docs/TEST.md`。
- 可观察验收场景：读对应模块 `specs.md`。
- Dev Task 状态、文档回填、分支或 worktree：只读 `agent_docs/DEV_TASK_EXECUTION.md` 的合同。

## 单一真实来源与冲突裁决

当工件冲突时，先确认项目是否声明了更具体的优先级；没有时按下列顺序修正低优先级工件：

1. 已批准的 schema、迁移和外部协议：字段、索引、约束和协议结构。
2. `agent_docs/ARCH.md`：稳定分层和依赖边界。
3. `agent_docs/DATA_ENGINEERING.md`：存储、事务、repository 和生成规则。
4. `agent_docs/BUILD.md` 与实际脚本：工具、命令、配置和产物。
5. `agent_docs/TEST.md`：测试策略和质量门槛。
6. 模块 `requirements/design/specs`：业务目标、设计裁决和协议语义。
7. 当前任务工件：当前范围、临时决策和验收。
8. 代码与测试：当前实现证据；发现与已确认规范冲突时不能静默把实现当成新标准。

证据不足时记录问题并阻塞相关假设，不自行发明字段、权限、状态、真实连接或生产行为。

## 代码与文档约束

- 只做当前任务要求的最小完整改动，避免无关重构。
- 包和目录必须有清晰职责，禁止创建无边界的 `common`、`utils`、`helper`、`dao` 或 `model` 大杂烩。
- 业务代码遵守 `agent_docs/ARCH.md` 的依赖方向；具体框架对象不得穿透稳定业务边界。
- schema、生成代码和 repository 变更遵守 `agent_docs/DATA_ENGINEERING.md`，不手工编辑生成文件。
- 行为、字段、接口或边界变化必须同步相应长期文档；纯实现细节不制造无意义文档 diff。
- 真实密钥、生产地址、个人数据和未脱敏载荷不得进入代码、fixture、日志或文档。
- 代码注释解释约束、原因和边界，不逐字复述代码。

## Planning 与 Dev Task

- `data/tasks/*.json` 的执行状态只由 LoopForge 调度器更新并发布；项目 Agent 和脚本不得领取、推进、归档或回写状态。
- Trellis、OpenSpec 或其他 planning system 存在时，由其自行维护目录和工件；本入口不假设固定规划目录。
- Target、状态、文档回填、分支和 worktree 合同只以 `agent_docs/DEV_TASK_EXECUTION.md` 为准。

## 默认验证

源码、脚本或生成代码变更后，按顺序运行：

```text
scripts/format-check.sh
scripts/lint.sh
scripts/test.sh unit
scripts/build.sh
```

涉及真实 adapter 时运行 `scripts/test.sh integration`；发布或合入前可使用 `scripts/test.sh all`。项目有代码生成、API 合同、迁移检查或 E2E 时，再运行 `agent_docs/BUILD.md` 与 `agent_docs/TEST.md` 登记的项目专用门禁。

纯 Markdown 变更可以跳过源码门禁，但必须检查链接、旧引用和 `git diff --check`，并记录跳过理由。任何未执行或因外部条件失败的验证都必须明确说明，不能包装为通过。
