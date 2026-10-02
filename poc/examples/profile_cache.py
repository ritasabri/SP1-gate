import time

PROFILES = {7: "Ana Ruiz", 8: "Bo Chen"}


def fetch_profile(user_id):
    time.sleep(0.05)  # stands in for a slow network call
    return PROFILES.get(user_id, "unknown")


def get_profile(user_id):
    cache = {}
    if user_id not in cache:
        cache[user_id] = fetch_profile(user_id)
    return cache[user_id]
