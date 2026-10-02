"""Shared helpers for SP1 Gate. Python 3.9+, standard library only."""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
LETTERS = "ABCD"


def sp1_home():
    """Where SP1 Gate keeps its state. SP1_HOME overrides it (the tests use that)."""
    path = os.environ.get("SP1_HOME") or os.path.join(os.path.expanduser("~"), ".sp1")
    os.makedirs(path, exist_ok=True)
    return path


DEFAULT_CONFIG = {
    # After the person answers the questions about an action, Claude Code shows its own
    # permission prompt ("ask"), so pressing 1 stays the person's decision. "allow" skips
    # that second step and lets the action run as soon as the answers are right.
    "after_answers": "ask",
    "max_action_questions": 2,
    "max_turn_questions": 3,
    "action_wait_seconds": 540,   # the PreToolUse hook's own deadline (hook timeout is 600)
    "open_browser": True,
}


def load_config():
    config = dict(DEFAULT_CONFIG)
    path = os.path.join(sp1_home(), "config.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                config.update(json.load(f))
        except (OSError, ValueError):
            pass
    return config


def load_stdlib_names():
    """Standard-library module names that exist in every Python from 3.10 to 3.13
    (and in 3.9), plus the names whose status depends on the version."""
    with open(os.path.join(HERE, "stdlib_names.json")) as f:
        data = json.load(f)
    return set(data["stable"]), set(data["version_dependent"])


def question(qid, lens, stem, key, key_why, distractors, rng, evidence=None):
    """Build one multiple-choice question.

    key: the correct option's text. key_why: shown after a correct answer.
    distractors: [(text, feedback)], at least 3; the first 3 are used.
    rng: a random.Random seeded from the session's secret, so the agent can't
    predict which letter is right.
    """
    options = [(key, None, True)] + [(t, why, False) for t, why in distractors[:3]]
    texts = [o[0] for o in options]
    if len(options) != 4 or len(set(texts)) != 4:
        return None
    rng.shuffle(options)
    return {
        "id": qid,
        "lens": lens,
        "stem": stem,
        "options": [{"letter": LETTERS[i], "text": o[0], "why": o[1]} for i, o in enumerate(options)],
        "answer": LETTERS[[o[2] for o in options].index(True)],
        "explain": key_why,
        "evidence": evidence or {},
    }


def public_question(q):
    """What the page may see: no key, no feedback."""
    return {
        "id": q["id"],
        "lens": q["lens"],
        "stem": q["stem"],
        "options": [{"letter": o["letter"], "text": o["text"]} for o in q["options"]],
    }


def make_rng(secret, salt):
    return random.Random("{}:{}".format(secret, salt))
