# 票据投递回执台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m receiptdesk demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m receiptdesk --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

## 外部渠道汇总

内部 `report` 仍保存完整证据；外部合作方走 `disclose` 命令：

- `python -m receiptdesk disclosure-demo --workdir DIR` 导入 `channel_disclosure_*` fixture 并演示发布。
- `python -m receiptdesk disclose --db DB --request fixtures/channel_disclosure_requests.json --policy deny_unsafe|minimum_safe_coarsening [--out-dir DIR]` 发布或记录拒绝。
- `python -m receiptdesk disclosure-ledger --db DB --audience external-partner` 查看该受众的发布台账。
- `python -m receiptdesk recover-disclosures --db DB [--out-dir DIR]` 修复中断的文件/台账。

外部文件只含发布 ID、实际 cohort、该渠道计数、批次与配置版本。安全性按受众分别核验：把该受众全部已发布群组等式与新等式联立，以每人的渠道为二元变量；若任何个人的渠道被唯一推出即拒绝。拒绝的尝试不进入可见知识，其他受众的发布也互不可见。`minimum_safe_coarsening` 只在合同 `allowed_expansion` 内加入最少的人；同请求 ID 重放不重复披露，发布按受众串行提交，文件生成与台账登记中途失败由 `recover-disclosures` 修复。
