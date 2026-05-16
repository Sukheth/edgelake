from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import INBOX, PROCESSED
from .emburse.session import chromeriver_session
from .emburse.uploader import create_report, upload_draft
from .ledger import already_drafted, hash_file, record
from .parsers.pdf import parse_pdf

console = Console()


@click.group()
def main() -> None:
    """edgelake — auto-file Swiggy/Blinkit receipts to Chrome River."""


@main.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def parse(path: Path) -> None:
    """Parse a single PDF and print extracted fields."""
    r = parse_pdf(path)
    table = Table(show_header=False)
    table.add_row("merchant", r.merchant)
    table.add_row("date", r.date.isoformat())
    table.add_row("amount", f"{r.currency} {r.amount:.2f}")
    console.print(table)


@main.command(name="parse-all")
@click.option("--dir", "directory", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None)
def parse_all(directory: Path | None) -> None:
    """Parse every PDF in inbox (or --dir) and print a summary table."""
    directory = directory or INBOX
    pdfs = sorted(directory.glob("*.pdf"))
    if not pdfs:
        console.print(f"[yellow]No PDFs in {directory}[/yellow]")
        return
    table = Table(title=f"Receipts in {directory}")
    table.add_column("File")
    table.add_column("Merchant")
    table.add_column("Date")
    table.add_column("Amount", justify="right")
    table.add_column("Status")
    for pdf in pdfs:
        try:
            r = parse_pdf(pdf)
            table.add_row(pdf.name, r.merchant, r.date.isoformat(), f"{r.currency} {r.amount:.2f}", "ok")
        except Exception as e:  # noqa: BLE001
            table.add_row(pdf.name, "-", "-", "-", f"[red]{e}[/red]")
    console.print(table)


@main.command()
@click.option("--dir", "directory", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None)
@click.option("--dry-run", is_flag=True, help="Parse + log only, do not open browser.")
def upload(directory: Path | None, dry_run: bool) -> None:
    """Create draft expenses in Chrome River for every PDF in inbox."""
    directory = directory or INBOX
    pdfs = sorted(directory.glob("*.pdf"))
    if not pdfs:
        console.print(f"[yellow]No PDFs in {directory}[/yellow]")
        return

    # Parse first so we fail fast on bad PDFs before opening the browser
    parsed: list[tuple[Path, object, str]] = []
    for pdf in pdfs:
        sha = hash_file(pdf)
        if already_drafted(sha):
            console.print(f"[dim]skip (already drafted): {pdf.name}[/dim]")
            continue
        try:
            r = parse_pdf(pdf)
            parsed.append((pdf, r, sha))
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]parse failed: {pdf.name}: {e}[/red]")

    if not parsed:
        console.print("[yellow]Nothing to upload.[/yellow]")
        return

    if dry_run:
        for pdf, r, _ in parsed:
            console.print(f"would draft: {pdf.name} → {r.merchant} {r.date} {r.amount}")
        return

    # Group: one report per merchant, per run.
    merchants = {r.merchant for _, r, _ in parsed}
    if len(merchants) > 1:
        console.print(
            f"[yellow]Inbox has receipts from multiple merchants ({merchants}). "
            "Run separately for each — keep one merchant in inbox/ at a time.[/yellow]"
        )
        return

    receipts = [r for _, r, _ in parsed]
    with chromeriver_session() as page:
        ok = False
        try:
            ok = bool(create_report(page, receipts))
            page.wait_for_timeout(2000)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]flow failed: {e}[/red]")

    if not ok:
        console.print("[yellow]Draft flow did not complete — leaving inbox untouched.[/yellow]")
        return

    PROCESSED.mkdir(parents=True, exist_ok=True)
    for pdf, r, sha in parsed:
        record(
            sha=sha,
            filename=pdf.name,
            merchant=r.merchant,
            date=r.date.isoformat(),
            amount=r.amount,
            currency=r.currency,
            status="drafted",
        )
        target = PROCESSED / pdf.name
        if target.exists():
            target = PROCESSED / f"{pdf.stem}_{sha[:8]}{pdf.suffix}"
        pdf.rename(target)
        console.print(f"[dim]  moved -> {target.relative_to(PROCESSED.parent)}[/dim]")
    console.print(f"[green]Recorded {len(parsed)} receipt(s) as drafted.[/green]")


if __name__ == "__main__":
    main()
