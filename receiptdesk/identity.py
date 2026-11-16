"""批次内别名归并。

别名只在冻结名单内有效：resolve 仅把名单登记过的别名归到对应收件人；
未登记的地址保持事件原样，由规则层标记 unknown_recipient 并隔离，
绝不自动归并到其他收件人，也不参与任何他人的因果裁决。
"""


def alias_map(recipients):
    return {alias: recipient['id'] for recipient in recipients for alias in recipient.get('aliases', [])}


def resolve(subject, address, aliases):
    return aliases.get(address, subject)
