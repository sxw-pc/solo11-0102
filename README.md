# 票据投递回执台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m receiptdesk demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m receiptdesk --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

## 保密校样废纸整批处置

废纸案 fixture 冻结包（油墨级别、实称质量、纤维/残渣、queued 来源 SHA）、箱容量与两份运营方授权合同。离线 CLI：

```
python -m receiptdesk ingest --db w/ledger.db --config fixtures/paper-config.json fixtures/paper-events.jsonl
python -m receiptdesk paper-register   --db w/ledger.db --case-id paper-01 fixtures/paper-case.json
python -m receiptdesk paper-preflight  --db w/ledger.db --case-id paper-01
python -m receiptdesk paper-plan       --db w/ledger.db --case-id paper-01 --contract maximize_recovery
python -m receiptdesk paper-campaign   --db w/ledger.db --case-id paper-01 --plan-id camp-1 --contract maximize_recovery
python -m receiptdesk paper-run        --db w/ledger.db --case-id paper-01 --plan-id camp-1
python -m receiptdesk paper-handoff    --db w/ledger.db --plan-id camp-1 --out w/handoff.json
```

`maximize_recovery` 在 fixture 上把 WATER 的 P1 与 TONER 的 P2 分箱各运一次（C9/C5），实际回收纤维 7kg、受控残渣 2kg；`minimize_shipments` 把两包合装 C9 只运一箱，混装油墨使纤维不可回收，9kg 全部保密热处置。两者都处理完全部 9kg，但运输、路线与产物不同——规划器是精确子集动态规划，不是报表改名。

`paper-run` 可在任意中断后重入：已持久化的操作按操作 ID 与规范意图对账，绝不重复运箱或二次处理；操作 ID 换动作复用时冲突隔离。缺包、来源摘要变化、容量不足或处理厂拒绝会隔离受影响箱并对剩余库存重规划，交接 JSON 给出逐包来源追溯、逐箱载重与油墨组成、操作流水、逐路线实际产物和包/箱/批三层质量守恒。`paper-authorize` 可离线吊销处理厂路线授权。
