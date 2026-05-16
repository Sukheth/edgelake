from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import INBOX, PROCESSED
from .emburse.session import chromeriver_session
from .emburse.uploader import create_report, upload_draft
from .fetchers import blinkit as blinkit_fetcher
from .ledger import (
    already_drafted,
    get_last_fetched,
    hash_file,
    record,
    set_last_fetched,
)
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


@main.command()
@click.option("--dir", "directory", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None,
              help="Folder to rename PDFs in. Defaults to receipts/inbox/.")
@click.option("--dry-run", is_flag=True, help="Print proposed renames without applying.")
def rename(directory: Path | None, dry_run: bool) -> None:
    """Rename existing Blinkit PDFs to Blinkit_<ORDERID>_<YYYY-MM-DD>_0000_<AMOUNT>.pdf.

    Time is '0000' because backfill has no row-level time data — only date+amount
    are recoverable from the PDF itself. New fetches keep their real time."""
    import re as _re
    directory = directory or INBOX
    pdfs = sorted(directory.glob("*.pdf"))
    if not pdfs:
        console.print(f"[yellow]No PDFs in {directory}[/yellow]")
        return

    target_shape = _re.compile(r"^Blinkit_ORD\d+_\d{4}-\d{2}-\d{2}_\d{4}_\d+\.pdf$")
    renamed = 0
    skipped = 0
    failed = 0
    for pdf in pdfs:
        if target_shape.match(pdf.name):
            skipped += 1
            continue
        # Need an ORD token to anchor on.
        if "ORD" not in pdf.stem:
            console.print(f"[dim]  skip (no ORD in name): {pdf.name}[/dim]")
            skipped += 1
            continue
        order_id = pdf.stem[pdf.stem.index("ORD"):].split("_")[0]
        try:
            r = parse_pdf(pdf)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]  parse failed: {pdf.name}: {e}[/red]")
            failed += 1
            continue
        if r.merchant != "Blinkit":
            console.print(f"[dim]  skip (not Blinkit, got {r.merchant}): {pdf.name}[/dim]")
            skipped += 1
            continue
        date_part = r.date.isoformat()
        amount_part = str(int(round(r.amount)))
        new_name = f"Blinkit_{order_id}_{date_part}_0000_{amount_part}.pdf"
        target = pdf.with_name(new_name)
        if target.exists() and target != pdf:
            console.print(f"[yellow]  conflict: {new_name} exists, leaving {pdf.name}[/yellow]")
            failed += 1
            continue
        if dry_run:
            console.print(f"  would rename: {pdf.name} -> {new_name}")
            renamed += 1
            continue
        try:
            pdf.rename(target)
            console.print(f"[green]  {pdf.name}[/green] -> {new_name}")
            renamed += 1
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]  rename failed: {pdf.name}: {e}[/red]")
            failed += 1

    verb = "would rename" if dry_run else "renamed"
    console.print(f"\n[cyan]{verb}: {renamed}  skipped: {skipped}  failed: {failed}[/cyan]")


@main.command()
@click.option("--merchant", type=click.Choice(["blinkit"]), default="blinkit",
              help="Which merchant to fetch from (only blinkit supported for now).")
@click.option("--since", "since", default=None,
              help="ISO date/time cutoff (e.g. 2026-05-01 or 2026-05-01T09:00). "
                   "Overrides the last-run watermark for this run only.")
@click.option("--resume/--no-resume", default=True,
              help="When --since is not given, resume from the last successful fetch "
                   "watermark. Use --no-resume to pull everything available.")
@click.option("--skip-weekends", is_flag=True,
              help="Skip orders placed on Saturday or Sunday.")
def fetch(merchant: str, since: str | None, resume: bool, skip_weekends: bool) -> None:
    """Download invoice PDFs from a merchant into receipts/inbox/."""
    if since:
        since_iso = since
        console.print(f"[cyan]Since cutoff (explicit):[/cyan] {since_iso}")
    elif resume:
        since_iso = get_last_fetched(merchant)
        if since_iso:
            console.print(f"[cyan]Resuming from last run:[/cyan] {since_iso}")
        else:
            console.print("[cyan]No prior fetch on record — pulling everything available.[/cyan]")
    else:
        since_iso = None
        console.print("[cyan]--no-resume: pulling everything available.[/cyan]")

    if skip_weekends:
        console.print("[cyan]Skipping Saturday/Sunday orders.[/cyan]")

    from datetime import datetime
    run_started = datetime.utcnow().isoformat()

    if merchant == "blinkit":
        n = blinkit_fetcher.fetch(since_iso, skip_weekends=skip_weekends)
    else:
        console.print(f"[red]Unsupported merchant: {merchant}[/red]")
        return

    console.print(f"[green]Fetched {n} PDF(s).[/green]")
    # Only advance the watermark if at least one file came down, so a failed
    # discovery run doesn't silently skip a real backlog.
    if n > 0:
        set_last_fetched(merchant, run_started)
        console.print(f"[dim]  watermark set: {merchant} -> {run_started}[/dim]")


if __name__ == "__main__":
    main()
