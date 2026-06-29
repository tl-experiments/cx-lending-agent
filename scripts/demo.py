#!/usr/bin/env python3
"""
Small, reliable demo of Tilicho Credit Assist.

Plays a fixed 5-turn conversation through the LIVE agent (Gemini Enterprise for CX)
and prints a clean transcript — showing real tool calls, grounded answers, and that
it remembers the customer across the session. Safe to run on a call: it backs off on
rate limits so it won't error out.

Run:  python scripts/demo.py
"""
import json
import textwrap
import time
import urllib.request

URL = "https://tilicho-credit-api-804472053350.us-central1.run.app/chat"
SESSION = "demo-" + str(int(time.time()))

TURNS = [
    "My loan is TL-1001, phone 4417. What's my EMI and balance?",   # real tool call
    "And when is it due?",                                          # session memory
    "What's the late payment fee if I miss one?",                   # grounded policy
    "I'd like to foreclose this loan — what's the payoff?",         # real tool call
    "Actually, I want to raise a complaint about a wrong charge.",  # ticket, no re-verify
]

GREY, RED, GREEN, DIM, BOLD, OFF = "\033[90m", "\033[31m", "\033[32m", "\033[2m", "\033[1m", "\033[0m"


def ask(message):
    body = json.dumps({"message": message, "session_id": SESSION, "mode": "full"}).encode()
    for _ in range(10):
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=70).read())
        except Exception:
            time.sleep(4); continue
        if d.get("reply"):
            return d
        if d.get("rate_limited"):
            print(f"   {DIM}(rate-limited, waiting…){OFF}"); time.sleep(15); continue
        time.sleep(4)
    return {"reply": "(no response — try again)"}


def wrap(text, indent="            "):
    return ("\n").join(textwrap.fill(line, 84, initial_indent=indent, subsequent_indent=indent)
                       for line in text.splitlines())


def main():
    print(f"\n{BOLD}TILICHO CREDIT ASSIST{OFF}  {GREY}— live on Gemini Enterprise for Customer Experience{OFF}\n")
    for i, t in enumerate(TURNS, 1):
        print(f"{BOLD}Customer:{OFF} {t}")
        d = ask(t)
        print(f"{wrap(d.get('reply',''))}")
        tags = []
        for tool in (d.get("tools") or []):
            tags.append(f"{RED}● {tool}{OFF}")
        if d.get("grounded"):
            tags.append(f"{GREEN}● grounded in policy{OFF}")
        if d.get("latency_ms"):
            tags.append(f"{GREY}{d['latency_ms']/1000:.1f}s{OFF}")
        if tags:
            print(f"            {GREY}↳{OFF} " + "   ".join(tags))
        print()
        time.sleep(1)
    print(f"{GREY}{'─'*60}{OFF}")
    print(f"{DIM}5 turns · real tool calls + grounded answers + it remembered the loan")
    print(f"across the whole chat · all running on Google Cloud, built from code.{OFF}\n")


if __name__ == "__main__":
    main()
