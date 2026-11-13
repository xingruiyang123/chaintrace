# 变更记录

本文件记录仓库中可追踪的代码/协作基线变化，不代替 Git 提交记录或测试结果。

## Unreleased

- 初始化单体仓库候选，纳入算法、Electron/React 桌面端、Python 桥接、合成 fixture、SQL Schema、测试和技术文档。
- 添加 Python、Node UI 与 Vite 构建的 GitHub Actions 工作流；私有仓库已建立并推送。首次运行失败：Python 测试收集找不到冻结算法模块，Node UI 测试引用未纳入仓库的本地生成结果，导致 UI 构建被跳过。
- 添加 PR 模板、协作规则、`.gitignore` 和项目状态/技术决策记录。
- 未修改算法语义；本仓库快照未在迁移阶段重跑全部测试或 Windows 打包。

## 2026-11-13

- **仓库可见性：私有 → 公开。** 公开前已完成一次内容自查：全仓无凭据/密钥、无手机号与证件号、无邮箱与学校名称；`git log --all --diff-filter=D` 无删除记录，历史中不存在「已删除但仍可检出」的文件；`.gitignore` 覆盖 `.env*`、`*.key`、`*.pem`、`node_modules/`、`dist/` 与导出产物。仅收录源码、合成数据与文档，不含申报书、评审材料或真实案件资料。
- **修复 CI 两处失败**（自 2026-11-09 首次推送起一直红）：
  - Python 作业：测试文件 `from chaintrace_matcher import ...` 失败，因算法模块与桥接服务分处两层目录。改为显式设置 `PYTHONPATH`（`ALGO_DIR` + `PROTOTYPE_DIR`），不再依赖工作目录。
  - UI 作业：`node --test tests/analysis.test.mjs` 读取的 `desktop/验收导出/核验完整结果.json` 被 `.gitignore` 排除，仓库里本就不存在。改为**先用冻结引擎跑一次导出**再跑测试，并断言导出行数 > 0、结果文件存在。
- 本地已验证等价命令：Python `27 passed`；导出生成 `event_claim_rows = 16`；UI `5 passed`；`vite build` 通过。
- 文档同步：README 的「私有仓库候选」与「按私有仓库准备」改为公开事实；PROGRESS 的「私有 monorepo」改为公开；ADR 0001 增加 2026-11-13 状态说明（保留原决策背景，标注哪半句已失效）。**仍未附加 LICENSE**——无许可证即默认保留所有权利。
## 2026-11-07 候选基线说明

- 来源导出候选记录新增结构化输入对象及关联来源记录导出；哈希仅用于内容一致性检查，不证明原始材料真实性或完整页段定位。
- 此日期的候选验证结论只针对当时目录。导入本仓库不等于重新验收。
