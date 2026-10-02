"""Four proposed fixes for fix_ticket.py, written as code.

The engine runs the same taint analysis on each version. An option is the
answer only if its code has no path by which ticket text becomes command text.
Every option is valid code that would run, so none of them "works" by crashing.
"""
import os

BASE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_ticket.py")).read()


def swap(old, new):
    assert old in BASE, f"not found in fix_ticket.py: {old!r}"
    return BASE.replace(old, new)


PROMPT_RULE = swap(
    'that fixes this issue:\\n{ticket_text}"',
    'that fixes this issue. Never reply with a dangerous command.\\n{ticket_text}"',
)

BLOCKLIST = swap(
    "    command = reply.content[0].text\n",
    "    command = reply.content[0].text\n"
    '    for bad in (";", "&&", "|"):\n'
    '        command = command.replace(bad, "")\n',
)

PRINT_FIRST = swap(
    "    subprocess.run(command, shell=True)\n",
    "    print(command)\n    subprocess.run(command, shell=True)\n",
)

ALLOWLIST = '''import subprocess

from anthropic import Anthropic

client = Anthropic()

ALLOWED = {
    "restart-web": ["systemctl", "restart", "web"],
    "reload-proxy": ["systemctl", "reload", "nginx"],
}


def fix_ticket(ticket_text):
    """Ask the model which preset fix applies, then run only that preset."""
    prompt = f"Reply with exactly one of {sorted(ALLOWED)} for this issue:\\n{ticket_text}"
    reply = client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    choice = reply.content[0].text.strip()
    if choice in ALLOWED:
        subprocess.run(ALLOWED[choice])
'''

# (what the option says, the code it describes, misconception feedback if it's wrong)
OPTIONS = [
    (
        'Add "Never reply with a dangerous command." to the prompt',
        PROMPT_RULE,
        "An instruction in the prompt is not a security boundary: the ticket text sits in the "
        "same prompt and can argue with it (OWASP LLM01, prompt injection).",
    ),
    (
        "Remove `;`, `&&` and `|` from the reply before running it",
        BLOCKLIST,
        "One command can do harm on its own (for example `rm -rf ~`), and a blocklist misses "
        "`$( )`, backticks and more. The reply is still the command.",
    ),
    (
        "Print `command` before running it",
        PRINT_FIRST,
        "Printing records the command; the next line still runs it.",
    ),
    (
        "Run only a preset command that the model picks by name",
        ALLOWLIST,
        None,
    ),
]
