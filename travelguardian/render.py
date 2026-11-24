"""Terminal output."""

from __future__ import annotations

import os
import sys
from typing import Optional

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return c(t, "1")


def dim(t: str) -> str:
    return c(t, "2")


SEV = {"critical": ("CRITICAL", "1;31"), "warning": ("WARNING ", "33"), "info": ("INFO    ", "36")}
ICON = {"flight": "✈", "hotel": "⌂", "event": "◆"}


def heading(t: str) -> None:
    print("\n" + bold(t))
    print(dim("─" * min(72, max(len(t), 40))))


def itinerary(data: dict) -> None:
    heading(f"Itinerary · {data.get('traveler', '')}")
    day = None
    for it in data["items"]:
        at = it["at"] if isinstance(it["at"], str) else it["at"].isoformat()
        d = at[:10]
        if d != day:
            day = d
            print(dim(f"  {d}"))
        print(f"  {ICON.get(it['type'], '·')} {bold(it['title'])}")
        print(f"      {dim(it['detail'])}")
        if it.get("status"):
            print(f"      {c(it['status'], '1;31')}")


def conflicts(items: list[dict], title: str = "Conflicts") -> None:
    heading(title)
    if not items:
        print("  " + c("None found.", "32"))
        return
    for x in items:
        label, code = SEV[x["severity"]]
        print(f"  {c(label, code)} {x['message']}")


def step(n: int, text: str) -> None:
    print("\n" + c(f"[{n}]", "1;35") + " " + bold(text))


def reasoning(text: str) -> None:
    print(dim(text), end="", flush=True)


def tool_call(name: str, args: dict) -> None:
    short = ", ".join(f"{k}={v}" for k, v in args.items() if k not in ("body",))
    print(dim(f"\n  ↳ {name}({short[:110]})"), file=sys.stderr)


def approval_prompt(summary: dict) -> Optional[bool]:
    """Ask the human in the terminal. No TTY means nobody can approve: leave it pending."""
    print()
    print(c("┌─ Approval needed ─────────────────────────────────────────────", "1;33"))
    print(c("│ ", "1;33") + "Replace: " + "; ".join(summary["replace"]))
    print(c("│ ", "1;33") + "With:    " + summary["with"])
    print(c("│ ", "1;33") + "Cost:    " + summary["cost"])
    print(c("│ ", "1;33") + "Why:     " + summary["reason"])
    print(c("└───────────────────────────────────────────────────────────────", "1;33"))
    if not sys.stdin.isatty():
        print(dim("  (no terminal attached; left pending. Approve later with `tg approve " + summary["proposal_id"] + "`)"))
        return None
    while True:
        ans = input(bold("  Change this booking? [y/n] ")).strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def drafts(items: list[dict]) -> None:
    heading("Drafts (not sent)")
    if not items:
        print("  None.")
    for d in items:
        print(f"  {bold(d['subject'])}  {dim('→ ' + d['to'])}  {dim('[' + d['kind'] + ']')}")
        for line in d["body"].splitlines():
            print("    " + dim(line))
        print()
