# 票据投递回执台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m receiptdesk demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m receiptdesk --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

## 实体标签发行

`print_lab` 只是本地打印机与空白标签库存端点：它保存每枚序列的物理状态和修订号，用 `(serial, job, operation_id, expected_revision)` 做比较交换，并可按夹具声明的故障返回 `committed`、`jammed` 或 `commit_then_ack_lost`（对外表现为 `timeout`，但纸张已实际打印）。它不做任何编排判断。

`issuance` 在其上实现可恢复的发行流程：

- **冻结工单**：`open_case` 把每个工单固定到一个真实 `queued` 事件（事件 ID、身份、事件 SHA、来源 ID、来源 SHA）、通知 ID、版面版本与内容摘要，并校验摘要确由 `label-v1|job|subject|message_id|version` 计算得到。未知收件人、重复通知/工单/收件人、旧版面摘要都会在开案时被拒绝。
- **打印前核验**：每次打印都重新核对工单仍在冻结名单内、queued 事件与来源 SHA 未变、无身份冲突；否则工单进入 `manual_review`，不占用库存。设备上已存在归属本工单且摘要吻合的实物标签时，直接接管（视为 ACK 丢失），绝不重打。
- **修订比较交换**：分配与打印都带 `expected_revision`。外部维护改变修订号或内容摘要时，流程停止并标记 `label_revision_conflict` / `device_print_unverified`，不覆盖设备。
- **卡纸与废标**：卡纸的序列立即记为 `jammed`、计入纸张消耗，永不再次发给任何收件人；工单换下一枚序列。
- **作废**：`void` 只有在设备确认 `void` 后才释放工单的标签槽位；作废 ACK 丢失时先读设备实况，未确认前禁止重打，因此 L100 与替代标签不可能同时被放行。
- **邮资账**：每枚实际打印的序列记一次整数分邮资（废标也计费），`label_postage` 以序列为主键保证不重复收费。
- **交邮就绪**：仅当设备确认该序列归属该工单、内容摘要吻合、邮资已入账且序列仍是工单当前标签时才为 `mail_ready`。`queued` 永不视为已送达，`decision` 表不被发行层改写。

两套均获授权的处置合同：

| 合同 | J1 | J2 | 有效序列 | 废标 | 纸张消耗 | 邮资 |
| --- | --- | --- | --- | --- | --- | --- |
| `retain_verified_serial` | 核对设备后保留 L100 | 越过卡纸 L101 打印 L102 | L100, L102 | L101（卡纸） | 3 | 160 分 |
| `void_and_reissue` | 作废 L100 并取得回执后打印 L103 | 越过卡纸 L101 打印 L102 | L103, L102 | L100（作废）、L101（卡纸） | 4 | 240 分（有效 160） |

命令：`python -m receiptdesk label-demo --contract void_and_reissue` 一键运行并写出 `labels.json` 与 `labels.md`。手动步骤为 `label-register` → `label-open` → `label-print` / `label-void` → `label-recover` → `label-run` → `label-report`。所有设备动作使用持久意图 + 操作 ID，同一命令重放返回既有结果；重启后 `label-recover` 按设备实况接管已打印、确认已作废、标记卡纸或把未触纸的意图回退为可重试。

