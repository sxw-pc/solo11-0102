# 票据投递回执台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m receiptdesk demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m receiptdesk --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

## 保留与擦除

混合收件人原文（如 `fixtures/mixed_recipient_source.jsonl`，R1+R2 共处一个 `source.raw` BLOB）支持两份授权合同，二者在活动库中产生不同的真实字节，而不只是报表隐藏字段：

- `shared_source_hold`：R1 保全凭据（按 SHA-256 校验）有效期间，保留完整 R1+R2 BLOB 及其原哈希；R2 擦除请求记为 `pending`，正常 report/read 路径（`store.masked_subjects`、`store.visible_events`、`report.export`）屏蔽 R2 事件、决策、证据与地址。保全解除（`release-hold`）在同一写事务中串行重处理待办，真正重建来源，绝不假装已擦除。
- `subject_erasure_with_rebase`：单个事务内重建只含 R1 原始行（保留原行号）的新 source/raw/SHA；`source_lineage` 记录原来源哈希、旧→新行号承接，`source_hash_tombstone` 为旧混合哈希立碑；R2 event/payload 与旧 BLOB 物理移除，R1 event 外键迁移到完整新来源，不允许孤儿证据或哈希不匹配的就地 UPDATE。

每份擦除按文件哈希、原始行号、事件归属（identity/source/line/canonical hash/subject 全核对）与凭据版本核验；坏行、身份无事件归属或跨主体混用身份一律失败关闭，不会被猜成 R2 行删除。台账（`retention_request`、`retention_audit`）记录请求 ID、凭据版本、受影响 source/event/行、旧/新哈希与保全原因；同请求重跑幂等，`BEGIN IMMEDIATE` 串行化保全解除与擦除，重建/外键迁移/旧 BLOB 移除/台账同事务提交，失败回滚到完整旧状态。已立墓碑的哈希禁止重新导入。命令见 `hold`、`erase`、`release-hold`、`retention-status`。本工具不判定送达是否有效，也不决定外部统计文件披露。
