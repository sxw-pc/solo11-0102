# 票据投递回执台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m receiptdesk demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m receiptdesk --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 离线现场核验派工

`python -m receiptdesk field-demo` 在临时工作区登记 `fixtures/field_case.json`、生成精确派工计划、按计划实际行走并输出 `field_report.json` / `.md`。手动流程：`field-register` → `field-plan --objective min_total_travel|min_max_lateness` → `field-move` / `field-inspect`（带幂等 `--op`）→ `field-report`；`field-close` / `field-reopen` 设置时段封路，`field-recover` 在重启后按已执行动作恢复位置，`field-sync [--recheck]` 核对任务资格快照。

只有当前结论为 `undelivered` / `insufficient` 且事件来源 SHA 与登记一致的目标会进入任务。计划与执行分开保存：`field_plan*` 是版本化的待派路线（含计划依据），`field_move` / `field_inspection` 是只追加的实际动作；到访后才记录观察，每个地点只检查一次，最后须在班次内抵达收工点。路网是有向边，时段关闭的边在窗口内不可通行，反向路与直线距离都不视为道路。

两名核验员、最多 8 个地点时，规划器用子集动态规划枚举所有地点归属与访问顺序，并按目标字典序取全局最优：`min_total_travel` 先最小化道路分钟总和、再最小化最大迟到；`min_max_lateness` 反过来。`optimality` 字段记录搜索是否完整、最优键、次优键与无更优计划的理由。途中封路、操作重放或进程重启后只对未到访地点重新派工，已检查地点不会再次写入。结论/来源快照失效时冻结案件并停止未执行派工，`field-sync --recheck` 重新核对后才恢复；现场观察不会改写原投递结论，也不代替邮寄标签打印或金融核销。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。
