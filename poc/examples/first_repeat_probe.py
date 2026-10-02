import random

SHOW = 'first_repeat(["ana@x.io", "bo@x.io", "cy@x.io", "bo@x.io", "ana@x.io"])'


def sized_input(n):
    # All different: the loop never returns early, so this is the worst case.
    return [f"user{i}@x.io" for i in range(n)]


def call(m, emails):
    return m.first_repeat(emails)


def random_inputs(count=50, seed=7):
    # Small lists that usually contain repeats, for checking that two versions agree.
    rng = random.Random(seed)
    pool = [f"u{i}@x.io" for i in range(8)]
    return [[rng.choice(pool) for _ in range(rng.randint(0, 10))] for _ in range(count)]
