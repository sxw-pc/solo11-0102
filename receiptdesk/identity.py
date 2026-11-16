def alias_map(recipients):
    return {alias: recipient['id'] for recipient in recipients for alias in recipient.get('aliases', [])}


def resolve(subject, address, aliases):
    return aliases.get(address, subject)
