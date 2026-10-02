def first_repeat(emails):
    """Return the first email that appears twice, or None if all are different."""
    seen = []
    for e in emails:
        if e in seen:
            return e
        seen.append(e)
    return None
