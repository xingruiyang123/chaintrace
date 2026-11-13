# 链迹核验研发仓库

**链迹核验——涉虚拟货币案件多源资金关联分析与辅助核验系统**。本仓库是从本地候选源码整理出的**公开研发仓库**，面向五人团队协作。仓库只收录源码、合成数据与文档，不含真实案件资料；当前为开发阶段原型，未形成发布版。

## 当前实现边界

**两条链路，互不替代：**

1. **关联核验**（算法一）：把「资金主张」（笔录、流水、报案材料里的一句话）与「资金事件」（链上转账、平台充值/提现、内部记账）做结构化比对，输出五类结论（编号规则匹配 / 较强候选 / 待复核候选 / 信息不足 / 明确冲突），每条结论可回溯到输入字段与来源记录。
2. **追缴研判**（算法二）：混币池识别 → 断链归因（入金×出金候选配对）→ 法币出口识别 → 资产可控性指数 → 追缴路径方案。

边界必须一起读：

- Python 规则引擎对同案资金事件与结构化主张生成可解释候选，并区分缺失与冲突；结果需要人工复核。
- **混币段在密码学上不可穿透**（Tornado 类 zk-SNARK + Merkle 承诺、Whirlpool 固定匿名集均未被破）。本仓库做的是**边界刻画 + 置信度标注 + 候选归因**，不是破解；材料与界面中不使用「穿透」作为能力描述。
- **归因分是规则命中累加（0–100），不是概率**。池内有 N 个等额候选时先验即 1/N，界面直接显示该先验，并标注同分并列。
- `S_off` 与 `S_ctrl` 为**专家规则加权指数**，不是冻结成功概率；权重未经真实结果校准，`response_hours_avg` 仍是单值而非分布。
- 导出来源目前关联到输入对象及来源记录；尚未映射到原始材料的页码、段落或截图区域。
- 仓库中的 PostgreSQL SQL 是逻辑 Schema 与冒烟用例。当前没有实际建库、RLS、事务写回或回滚实现。
- 本快照不是已部署系统，不接入真实案件数据，不自动确认人员身份或资金转移关系。时间线到关联图的联动曾记录为单向/未通过。

### 评估口径（引用时必须带限定语）

混币断链归因在**合成评估集**（8 案 / 209 节点 / 254 边 / 31 对构造真值）上的**归因一致性**：

| 口径 | 本方法（六维加权） | 基线：仅金额匹配 | 基线：仅时间最近 |
|---|---|---|---|
| 全体（n=31） | **p@1 0.839 ｜ p@3 0.936 ｜ MRR 0.896** | p@1 0.548 ｜ p@3 0.903 | p@1 0.097 ｜ p@3 0.419 |
| 含竞争条件（n=27） | p@1 0.815 | p@1 0.482 | p@1 0.074 |
| 零竞争条件（n=4） | p@1 1.000（样本极小，仅示上限） | p@1 1.000 | p@1 0.250 |

**这些是「合成数据 · 归因一致性」，不是识别准确率，不能外推真实案件。** 固定面额案对任何方法 p@1 都只有 0.2——同额同批在结构上不可解，我们不给假精度。基准原始数据见 `desktop/algo2/outputs/benchmark.json`。

## 目录

- `desktop/`：Electron 入口、React/Vite UI、Python 桥接与合成样例。
- `desktop/algo2/`：**算法二**（混币→法币出口识别）：图构建、混币识别、断链归因、出口评分、可控性、路径、评估集与基准。纯标准库，无第三方依赖。
- `desktop/src/modules/`：六个新增可视化模块（总览 / 资金流向 / 混币归因 / 法币出口 / 可控性 / 路径沙盘）。
- `desktop/冻结原始代码/原始源码与测试证据/算法一V0.1/`：桥接实际导入的冻结算法、测试、fixture、实验脚本和逻辑 Schema。
- `PROGRESS.md`：共享研发状态、已知问题和下一阶段计划。
- `CHANGELOG.md`：重要迭代记录；`docs/decisions/`：技术决策及原因。
- `docs/technical/`：算法方法与数据库修订说明。
- `.github/workflows/ci.yml`：Python 双算法测试、UI 单元测试与 Vite 构建。

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

> **已知环境坑（2026-11-13 实测）**：若宿主环境注入了 `ELECTRON_RUN_AS_NODE=1`，Electron 会以纯 Node 模式执行 `main.cjs`，`require('electron')` 拿到的是 npm 包而非内置模块，首行 `ipcMain.handle(...)` 直接抛 `Cannot read properties of undefined (reading 'handle')`。启动前 `set ELECTRON_RUN_AS_NODE=` 清掉即可；本仓库的 `desktop/启动链迹核验.cmd` 已内置该处理。

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

算法二（混币→法币出口识别）测试：

```powershell
cd desktop
$env:PYTHONPATH="$PWD\离线原型"
python -m pytest -q algo2/tests
```

重新生成评估集与基准：

```powershell
cd desktop
python algo2\generate_fixture.py
python algo2\benchmark.py
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
