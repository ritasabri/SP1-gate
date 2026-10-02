SHOW = "get_profile(7) called three times in a row"


def probe(m):
    # Count how many times the slow fetch really runs.
    calls = 0
    real = m.fetch_profile

    def counting(user_id):
        nonlocal calls
        calls += 1
        return real(user_id)

    m.fetch_profile = counting
    for _ in range(3):
        m.get_profile(7)
    return calls
