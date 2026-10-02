import subprocess

from anthropic import Anthropic

client = Anthropic()


def fix_ticket(ticket_text):
    """Ask the model for a shell command that fixes the ticket, then run it."""
    prompt = f"Reply with one shell command that fixes this issue:\n{ticket_text}"
    reply = client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    command = reply.content[0].text
    subprocess.run(command, shell=True)
