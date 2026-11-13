# 链迹核验研发仓库

**链迹核验——涉虚拟货币案件多源资金关联分析与辅助核验系统**。本仓库是从本地候选源码整理出的**公开研发仓库**，面向五人团队协作。仓库只收录源码、合成数据与文档，不含真实案件资料；当前为开发阶段原型，未形成发布版。

## 当前实现边界

- Python 规则引擎对同案资金事件与结构化主张生成可解释候选，并区分缺失与冲突；结果需要人工复核。
- React、Vite 与 Electron 桌面原型使用明确标记的合成数据，可查看关联图和时间线，并支持结构化结果与来源输入快照导出。
- 导出来源目前关联到输入对象及来源记录；尚未映射到原始材料的页码、段落或截图区域。
- 仓库中的 PostgreSQL SQL 是逻辑 Schema 与冒烟用例。当前没有实际建库、RLS、事务写回或回滚实现。
- 本快照不是已部署系统，不接入真实案件数据，不自动确认人员身份或资金转移关系。界面中尚有未实现功能；时间线到关联图的联动曾记录为单向/未通过。使用前应以最新复验记录重新确认。

本仓库候选不包含申报材料。共享研发进度见 [`PROGRESS.md`](PROGRESS.md)；其中记录的既有测试/复验状态不代表当前提交已经重跑。上述功能状态取自本地项目记录与候选目录，迁移准备期间没有重跑测试或完整桌面验收。

## 目录

- `desktop/`：Electron 入口、React/Vite UI、Python 桥接与合成样例。
- `desktop/冻结原始代码/原始源码与测试证据/算法一V0.1/`：桥接实际导入的冻结算法、测试、fixture、实验脚本和逻辑 Schema。
- `PROGRESS.md`：共享研发状态、已知问题和下一阶段计划。
- `CHANGELOG.md`：重要迭代记录；`docs/decisions/`：技术决策及原因。
- `docs/technical/`：算法方法与数据库修订说明。
- `.github/workflows/ci.yml`：Python 测试、UI 单元测试与 Vite 构建。

## 环境与启动

- Python 3.10 或更高版本；当前本机项目环境记录为 Python 3.13.5。
- Node.js 22 LTS（CI 使用）；需要 npm。
- 本地原型按 Electron 35、React 19、Vite 6 依赖锁文件安装。

```powershell
cd desktop
npm ci
npm run dev
```

`npm run dev` 需要 Electron 运行时和 Python。默认使用系统 `python`；若 Python 不在 PATH，可将 `CHAINTRACE_PYTHON` 设为解释器可执行文件路径。它不是密钥，不要把个人机器绝对路径写入代码或提交记录。

构建前端：

```powershell
cd desktop
npm run build
```

Python 引擎及导出测试（从仓库根目录）：

```powershell
python -m pip install -r desktop/冻结原始代码/原始源码与测试证据/算法一V0.1/requirements-dev.txt
python -m pytest -q desktop/冻结原始代码/原始源码与测试证据/算法一V0.1/tests desktop/test_evidence_export.py
```

UI 规则测试：

```powershell
cd desktop
node --test tests/analysis.test.mjs
```

CI 目前不执行 PostgreSQL 冒烟 SQL，也不制作 Windows 便携包。便携程序、PyInstaller 目录及依赖审计均需单独复验；不把 EXE 或构建输出提交到 Git。

## 数据与安全

只允许固定种子或明确标记为 `is_simulated=true` 的合成 fixture。禁止提交真实案件资料、个人身份信息、访问凭据、`.env`、本机配置、运行日志及未脱敏截图。发现凭据时先轮换，再从工作区和 Git 历史清除。**本仓库为公开仓库，任何提交都会立即被任何人看到**，提交前请自查。

当前 npm 锁定文件沿用 2026-11-07 候选；候选验收说明曾记录 Electron 及间接依赖的高危审计结果。本迁移阶段未重新执行依赖审计。首次远程推送前应更新并复核依赖风险。

## 项目状态与许可

本仓库自 2026-11-13 起为**公开仓库**，用于展示与协作。**暂不附加开源许可证**（无 LICENSE 即默认保留所有权利，他人不得直接商用或再分发）。若后续需要允许复用，由项目负责人决定采用何种许可。
