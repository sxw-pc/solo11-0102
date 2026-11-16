STATES = {
    'queued': {'sent', 'revoke'},
    'sent': {'deferred', 'bounced', 'receipt', 'revoke'},
    'deferred': {'sent', 'bounced', 'revoke'},
    'bounced': set(),
    'receipt': set(),
    'revoke': set(),
}


def advance(current, kind):
    if current is None:
        return (kind, None) if kind == 'queued' else (None, 'missing_queue')
    if kind not in STATES.get(current, set()):
        return current, 'illegal_transition'
    return kind, None
