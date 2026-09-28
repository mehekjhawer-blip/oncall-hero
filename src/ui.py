"""Tiny presentation shim: uses `rich` when installed, plain text otherwise, so the
demo never crashes on a missing cosmetic dependency."""
from __future__ import annotations

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table

    _RICH = True
    console = Console()
except ImportError:  # pragma: no cover - exercised in minimal environments
    _RICH = False
    console = None


def esc(text) -> str:
    """Escape untrusted text (recalled memories, logs) so [brackets] are not read as rich markup."""
    text = str(text)
    if _RICH:
        from rich.markup import escape

        return escape(text)
    return text


def say(text: str = "") -> None:
    if _RICH:
        console.print(text)
    else:
        import re

        print(re.sub(r"\[/?[a-z ]+\]", "", text))


def panel(body: str, title: str = "", style: str = "white") -> None:
    if _RICH:
        console.print(Panel(body, title=title, border_style=style))
    else:
        import re

        clean = lambda s: re.sub(r"\[/?[a-z ]+\]", "", s)
        bar = "-" * 72
        print(f"\n{bar}\n{clean(title)}\n{bar}\n{clean(body)}\n{bar}")


def rule(title: str) -> None:
    if _RICH:
        console.print()
        console.print(Rule(f"[bold]{title}[/bold]", style="cyan"))
    else:
        print(f"\n===== {title} =====")


def table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    if _RICH:
        t = Table(title=title, show_lines=False)
        for c in columns:
            t.add_column(c)
        for r in rows:
            t.add_row(*[esc(x) for x in r])
        console.print(t)
    else:
        print(f"\n{title}")
        print(" | ".join(columns))
        for r in rows:
            print(" | ".join(str(x) for x in r))
