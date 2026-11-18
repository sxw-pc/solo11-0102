# 票据投递回执台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m receiptdesk demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m receiptdesk --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

来源导入按固定块流式读取、解码、切行与哈希，内存峰值随 `--chunk-size`（默认 64 KiB）而非文件大小增长；`source.raw` 由暂存块增量写入并与所记 SHA-256 逐位一致。入站路径在读取途中被原子替换时，用 `ingest --contract freeze_on_open` 固定打开时的原版，或用 `ingest --contract verify_then_retry_latest` 丢弃过期暂存并对最终稳定版本重试；任一合同都不会同时混用两版字节、哈希或事件。`receiptdesk audit` 列出每个来源实际选中的快照身份、合同、尝试次数与暂存/提交行数。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。
