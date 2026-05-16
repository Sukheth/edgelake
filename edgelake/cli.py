from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import (
    INBOX,
    NEEDS_APPROVAL,
    POLICY_APPROVAL_MIN,
    POLICY_EXACT_MAX,
    PROCESSED,
)
from .emburse.session import chromeriver_session
from .emburse.uploader import create_report, upload_draft
from .fetchers import blinkit as blinkit_fetcher
from .ledger import (
    already_drafted,
    get_last_fetched,
    hash_file,
    list_by_status,
    reconcile_inbox,
    record,
    set_drafted,
    set_filename,
    set_last_fetched,
    set_needs_approval,
    set_parsed,
    set_skipped,
    set_verified,
)
from .parsers.pdf import Receipt, parse_pdf

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


def _amount_str(v: float | None) -> str:
    return f"Rs.{v:.2f}" if v is not None else "—"


def _apply_policy(raw_amount: float) -> tuple[float, str]:
    """Apply the expense policy to a raw chosen amount.

    Returns (final_amount, policy_tag) where policy_tag is one of:
      'exact'         — amount <= POLICY_EXACT_MAX, used as-is
      'capped'        — POLICY_EXACT_MAX < amount < POLICY_APPROVAL_MIN,
                        capped to POLICY_EXACT_MAX before upload
      'needs_approval'— amount >= POLICY_APPROVAL_MIN, do not auto-upload
    """
    if raw_amount >= POLICY_APPROVAL_MIN:
        return raw_amount, "needs_approval"
    if raw_amount > POLICY_EXACT_MAX:
        return POLICY_EXACT_MAX, "capped"
    return raw_amount, "exact"


def _move_to_needs_approval(filename: str) -> str | None:
    """Move a file from inbox/processed to receipts/needs-approval/.
    Returns the new filename on disk (may differ on collision), or None
    if the file couldn't be located."""
    src = None
    for d in (INBOX, PROCESSED):
        candidate = d / filename
        if candidate.exists():
            src = candidate
            break
    if src is None:
        return None
    NEEDS_APPROVAL.mkdir(parents=True, exist_ok=True)
    target = NEEDS_APPROVAL / filename
    if target.exists() and target != src:
        # Suffix with sha-ish disambiguator from the order id in the name.
        target = NEEDS_APPROVAL / f"{src.stem}_dup{src.suffix}"
    try:
        src.rename(target)
        return target.name
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]  could not move {filename} to needs-approval/: {e}[/red]")
        return None


def _commit_decision(order_id: str, raw_amount: float, source: str, filename: str | None) -> str:
    """Apply policy + persist the decision. Returns the resulting status."""
    final_amount, tag = _apply_policy(raw_amount)
    if tag == "needs_approval":
        moved = _move_to_needs_approval(filename) if filename else None
        if moved and moved != filename:
            set_filename(order_id, moved)
        set_needs_approval(order_id, chosen_amount=raw_amount, amount_source=source)
        console.print(
            f"[bold yellow]  {order_id}: Rs.{raw_amount:.2f} >= Rs.{POLICY_APPROVAL_MIN:.0f} "
            f"-> NEEDS APPROVAL, moved to needs-approval/[/bold yellow]"
        )
        return "needs_approval"
    if tag == "capped":
        set_verified(order_id, chosen_amount=final_amount, amount_source=f"{source}+capped")
        console.print(
            f"[cyan]  {order_id}: raw Rs.{raw_amount:.2f} -> capped to Rs.{final_amount:.0f}[/cyan]"
        )
        return "verified"
    set_verified(order_id, chosen_amount=final_amount, amount_source=source)
    return "verified"


def _prompt_choice(order_id: str, listing: float | None, pdf: float | None) -> str:
    """Prompt user once for L/P/S. Returns one of 'listing'/'pdf'/'skip'.
    Blocks until valid input. Default is listing when available, else pdf,
    else skip — matching the project preference of trusting the listing."""
    if listing is not None:
        default_key, default_word = "L", "listing"
    elif pdf is not None:
        default_key, default_word = "P", "pdf"
    else:
        default_key, default_word = "S", "skip"

    legal = []
    if listing is not None:
        legal.append("L")
    if pdf is not None:
        legal.append("P")
    legal.append("S")

    prompt_str = (
        f"[{order_id}] choose: "
        + " / ".join(
            (
                "[bold]L[/bold]isting" if "L" in legal else "[dim]L[/dim]",
                "[bold]P[/bold]df" if "P" in legal else "[dim]P[/dim]",
                "[bold]S[/bold]kip",
            )
        )
        + f"  (default {default_key}={default_word}) "
    )
    while True:
        console.print(prompt_str, end="")
        try:
            raw = input().strip().upper()
        except EOFError:
            raw = ""
        if raw == "":
            raw = default_key
        if raw in legal:
            return {"L": "listing", "P": "pdf", "S": "skip"}[raw]
        console.print(f"[red]  invalid choice. valid: {', '.join(legal)}[/red]")


@main.command()
@click.option("--auto", is_flag=True,
              help="No prompts. Auto-resolve every flagged row by preferring "
                   "the listing amount (falling back to PDF if listing is missing). "
                   "Use this in unattended runs.")
def verify(auto: bool) -> None:
    """Decide which amount (listing vs PDF) to use for each parsed receipt.

    Auto-verifies rows where listing and PDF agree within Rs.1. Flags rows
    that mismatch or are single-source (only listing or only PDF) for review.
    Without --auto, prompts per-row and blocks until input."""
    rows = list_by_status("parsed")
    if not rows:
        console.print("[yellow]No 'parsed' rows to verify.[/yellow]")
        return

    auto_match: list[dict] = []
    flagged: list[dict] = []
    for row in rows:
        listing = row.get("listing_amount")
        pdf = row.get("pdf_amount")
        if listing is not None and pdf is not None and abs(listing - pdf) < 1.0:
            auto_match.append(row)
        else:
            flagged.append(row)

    # Silent auto-verify on matches. Policy still applies — a matched amount
    # over Rs.1000 still gets capped or flagged for approval.
    auto_verified = 0
    auto_capped = 0
    auto_approval = 0
    for row in auto_match:
        result = _commit_decision(
            order_id=row["order_id"],
            raw_amount=row["pdf_amount"],
            source="match",
            filename=row.get("filename"),
        )
        if result == "needs_approval":
            auto_approval += 1
        elif row["pdf_amount"] > POLICY_EXACT_MAX:
            auto_capped += 1
        else:
            auto_verified += 1
    if auto_match:
        console.print(
            f"[green]Auto-matched: {len(auto_match)}[/green]  "
            f"(exact={auto_verified}, capped={auto_capped}, needs_approval={auto_approval})"
        )

    if not flagged:
        console.print("[green]All receipts verified, no review needed.[/green]")
        return

    # Show the review table once before prompting.
    table = Table(title=f"Review needed: {len(flagged)} receipt(s)")
    table.add_column("Order ID")
    table.add_column("Date")
    table.add_column("Listing", justify="right")
    table.add_column("PDF", justify="right")
    table.add_column("Diff", justify="right")
    table.add_column("Why")
    for row in flagged:
        listing = row.get("listing_amount")
        pdf = row.get("pdf_amount")
        if listing is None and pdf is None:
            why = "both missing"
            diff = "—"
        elif listing is None:
            why = "PDF only (no listing)"
            diff = "—"
        elif pdf is None:
            why = "listing only (no PDF)"
            diff = "—"
        else:
            why = f"mismatch (>= Rs.1)"
            diff = f"Rs.{abs(listing - pdf):.2f}"
        table.add_row(
            row["order_id"],
            row.get("date") or "-",
            _amount_str(listing),
            _amount_str(pdf),
            diff,
            why,
        )
    console.print(table)

    resolved = {"listing": 0, "pdf": 0, "skip": 0}
    for row in flagged:
        listing = row.get("listing_amount")
        pdf = row.get("pdf_amount")
        order_id = row["order_id"]

        if listing is None and pdf is None:
            console.print(f"[yellow]  {order_id}: both amounts missing — skipping[/yellow]")
            set_skipped(order_id)
            resolved["skip"] += 1
            continue

        if auto:
            # Unattended: prefer listing, fall back to PDF.
            choice = "listing" if listing is not None else "pdf"
        else:
            choice = _prompt_choice(order_id, listing, pdf)

        if choice == "skip":
            set_skipped(order_id)
        else:
            raw = listing if choice == "listing" else pdf
            _commit_decision(
                order_id=order_id,
                raw_amount=raw,
                source=choice,
                filename=row.get("filename"),
            )
        resolved[choice] += 1

    console.print(
        f"\n[cyan]Resolved {len(flagged)} flagged: "
        f"listing={resolved['listing']}  pdf={resolved['pdf']}  skip={resolved['skip']}[/cyan]"
    )


@main.command(name="parse-pending")
def parse_pending() -> None:
    """Parse every ledger row with status='fetched' and bump it to 'parsed'.

    Pulls the PDF from inbox/ (or processed/ as a fallback), runs parse_pdf,
    and writes pdf_amount + merchant + date + currency back to the ledger.
    A parse failure leaves the row at 'fetched' so it surfaces in the
    verify step rather than getting silently skipped."""
    rows = list_by_status("fetched")
    if not rows:
        console.print("[yellow]No 'fetched' rows to parse.[/yellow]")
        return
    table = Table(title=f"Parsing {len(rows)} pending receipt(s)")
    table.add_column("Order ID")
    table.add_column("File")
    table.add_column("Merchant")
    table.add_column("Date")
    table.add_column("PDF Amount", justify="right")
    table.add_column("Status")
    ok = 0
    failed = 0
    missing = 0
    for row in rows:
        filename = row.get("filename") or ""
        path = INBOX / filename if filename else None
        if path and not path.exists():
            alt = PROCESSED / filename
            if alt.exists():
                path = alt
        if not path or not path.exists():
            table.add_row(row["order_id"], filename or "-", "-", "-", "-", "[red]file missing[/red]")
            missing += 1
            continue
        try:
            r = parse_pdf(path)
            set_parsed(
                order_id=row["order_id"],
                pdf_amount=r.amount,
                merchant=r.merchant,
                date=r.date.isoformat(),
                currency=r.currency,
            )
            table.add_row(
                row["order_id"],
                filename,
                r.merchant,
                r.date.isoformat(),
                f"{r.currency} {r.amount:.2f}",
                "[green]parsed[/green]",
            )
            ok += 1
        except Exception as e:  # noqa: BLE001
            table.add_row(row["order_id"], filename, "-", "-", "-", f"[red]{e}[/red]")
            failed += 1
    console.print(table)
    console.print(f"\n[cyan]parsed: {ok}  failed: {failed}  missing: {missing}[/cyan]")


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


def _row_to_receipt(row: dict, path: Path) -> Receipt | None:
    """Build a Receipt-shaped object from a ledger row for the Chrome River
    uploader. Returns None if the row lacks the minimum required fields."""
    from datetime import date as _Date
    chosen = row.get("chosen_amount")
    date_str = row.get("date") or ""
    merchant = row.get("merchant") or "Unknown"
    currency = row.get("currency") or "INR"
    if chosen is None or not date_str:
        return None
    try:
        y, m, d = (int(x) for x in date_str.split("-"))
        date_obj = _Date(y, m, d)
    except Exception:
        return None
    return Receipt(
        merchant=merchant,
        date=date_obj,
        amount=float(chosen),
        currency=currency,
        raw_text="",
        source_path=path,
    )


@main.command()
@click.option("--dry-run", is_flag=True, help="List what would be drafted, do not open browser.")
def upload(dry_run: bool) -> None:
    """Create draft expenses in Chrome River for every verified receipt.

    Reads ledger rows with status='verified' and uses chosen_amount (post-policy)
    as the line-item amount. Skips needs_approval, skipped, and drafted rows."""
    rows = list_by_status("verified")
    if not rows:
        console.print("[yellow]No 'verified' rows to upload.[/yellow]")
        # Show the user what IS in the ledger so they know what's blocked.
        snapshot = {
            "fetched": len(list_by_status("fetched")),
            "parsed": len(list_by_status("parsed")),
            "needs_approval": len(list_by_status("needs_approval")),
            "skipped": len(list_by_status("skipped")),
            "drafted": len(list_by_status("drafted")),
        }
        nonzero = {k: v for k, v in snapshot.items() if v}
        if nonzero:
            console.print(f"[dim]  ledger snapshot: {nonzero}[/dim]")
        return

    # Resolve each ledger row to a file on disk + a Receipt-shaped object.
    to_upload: list[tuple[dict, Path, Receipt]] = []
    for row in rows:
        filename = row.get("filename") or ""
        path = INBOX / filename if filename else None
        if not path or not path.exists():
            alt = PROCESSED / filename if filename else None
            if alt and alt.exists():
                # Previously drafted file? Shouldn't be 'verified' but guard anyway.
                path = alt
            else:
                console.print(f"[red]  {row['order_id']}: file missing ({filename}), skipping[/red]")
                continue
        receipt = _row_to_receipt(row, path)
        if receipt is None:
            console.print(f"[red]  {row['order_id']}: ledger row incomplete, skipping[/red]")
            continue
        to_upload.append((row, path, receipt))

    if not to_upload:
        console.print("[yellow]Nothing to upload after resolving files.[/yellow]")
        return

    # One report per merchant per run — keep the original guardrail.
    merchants = {rc.merchant for _, _, rc in to_upload}
    if len(merchants) > 1:
        console.print(
            f"[yellow]Verified receipts span multiple merchants ({merchants}). "
            "Run separately for each — keep one merchant verified at a time.[/yellow]"
        )
        return

    if dry_run:
        for row, path, rc in to_upload:
            console.print(
                f"would draft: {row['order_id']}  {path.name}  "
                f"{rc.merchant} {rc.date} {rc.currency} {rc.amount:.2f}  "
                f"(source={row.get('amount_source')})"
            )
        return

    receipts = [rc for _, _, rc in to_upload]
    with chromeriver_session() as page:
        ok = False
        try:
            ok = bool(create_report(page, receipts))
            page.wait_for_timeout(2000)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]flow failed: {e}[/red]")

    if not ok:
        console.print("[yellow]Draft flow did not complete — leaving files untouched.[/yellow]")
        return

    PROCESSED.mkdir(parents=True, exist_ok=True)
    for row, path, _rc in to_upload:
        target = PROCESSED / path.name
        if target.exists() and target != path:
            sha = row.get("sha256") or ""
            target = PROCESSED / f"{path.stem}_{sha[:8] if sha else 'dup'}{path.suffix}"
        try:
            path.rename(target)
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]  could not move {path.name}: {e}[/yellow]")
            set_drafted(row["order_id"])
            continue
        set_drafted(row["order_id"], new_filename=target.name)
        console.print(f"[dim]  drafted {row['order_id']} -> {target.relative_to(PROCESSED.parent)}[/dim]")
    console.print(f"[green]Recorded {len(to_upload)} receipt(s) as drafted.[/green]")


@main.command()
@click.option("--verbose", is_flag=True, help="Print each adopted/orphan file.")
def reconcile(verbose: bool) -> None:
    """Sync the ledger with files on disk.

    Adds ledger rows for PDFs in inbox/ + processed/ that aren't tracked yet,
    and flags rows whose file has gone missing. Cheap and idempotent — runs
    at the start of every command from Phase 7 onwards."""
    summary = reconcile_inbox(verbose=verbose)
    table = Table(show_header=False)
    table.add_row("Adopted (new ledger rows)", str(summary["added"]))
    table.add_row("Already tracked", str(summary["already_tracked"]))
    table.add_row("Orphan ledger rows (file missing)", str(summary["missing_files"]))
    console.print(table)
    if summary["added_files"]:
        console.print("\n[cyan]Newly adopted:[/cyan]")
        for n in summary["added_files"]:
            console.print(f"  {n}")
    if summary["missing_rows"]:
        console.print("\n[yellow]Ledger rows with missing files:[/yellow]")
        for r in summary["missing_rows"]:
            console.print(f"  {r['order_id']}  {r['filename']}  status={r['status']}")
    # Quick status snapshot.
    by_status = {}
    for st in ("fetched", "parsed", "verified", "skipped", "drafted"):
        n = len(list_by_status(st))
        if n:
            by_status[st] = n
    if by_status:
        console.print("\n[cyan]Ledger status counts:[/cyan]")
        for st, n in by_status.items():
            console.print(f"  {st:<10} {n}")


@main.command()
@click.option("--dry-run", is_flag=True, help="Print proposed renames without applying.")
def rename(dry_run: bool) -> None:
    """Rename every 'verified' receipt to Blinkit_<ORD>_<DATE>_<HHMM>_<AMOUNT>.pdf
    using ledger fields. Date and HHMM come from the ledger (HHMM defaults to
    '0000' when order_time is not recorded — e.g. for reconcile-adopted rows).
    Amount uses chosen_amount (set by verify)."""
    import re as _re
    rows = list_by_status("verified")
    if not rows:
        console.print("[yellow]No 'verified' rows to rename.[/yellow]")
        return

    target_shape = _re.compile(r"^Blinkit_ORD[A-Za-z0-9]+_\d{4}-\d{2}-\d{2}_\d{4}_\d+\.pdf$")
    renamed = 0
    skipped = 0
    failed = 0
    for row in rows:
        order_id = row["order_id"]
        filename = row.get("filename") or ""
        if not filename:
            console.print(f"[yellow]  {order_id}: no filename in ledger, skipping[/yellow]")
            failed += 1
            continue
        # Resolve current path: inbox first, then processed.
        path = INBOX / filename
        if not path.exists():
            alt = PROCESSED / filename
            if alt.exists():
                path = alt
            else:
                console.print(f"[red]  {order_id}: file missing ({filename}), skipping[/red]")
                failed += 1
                continue

        if target_shape.match(path.name):
            skipped += 1
            continue

        date_part = row.get("date") or ""
        if not date_part:
            console.print(f"[yellow]  {order_id}: no date in ledger, skipping[/yellow]")
            failed += 1
            continue
        time_part = row.get("order_time") or "0000"
        chosen = row.get("chosen_amount")
        if chosen is None:
            console.print(f"[yellow]  {order_id}: no chosen_amount, skipping[/yellow]")
            failed += 1
            continue
        amount_part = str(int(round(chosen)))
        new_name = f"Blinkit_{order_id}_{date_part}_{time_part}_{amount_part}.pdf"
        target = path.with_name(new_name)
        if target.exists() and target != path:
            console.print(f"[yellow]  {order_id}: conflict, {new_name} exists[/yellow]")
            failed += 1
            continue
        if dry_run:
            console.print(f"  would rename: {path.name} -> {new_name}")
            renamed += 1
            continue
        try:
            path.rename(target)
            set_filename(order_id, new_name)
            console.print(f"[green]  {order_id}[/green]: {path.name} -> {new_name}")
            renamed += 1
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]  {order_id}: rename failed: {e}[/red]")
            failed += 1

    verb = "would rename" if dry_run else "renamed"
    console.print(f"\n[cyan]{verb}: {renamed}  already-shaped: {skipped}  failed: {failed}[/cyan]")


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


@main.command()
@click.option("--merchant", type=click.Choice(["blinkit"]), default="blinkit",
              help="Which merchant to fetch from.")
@click.option("--since", "since", default=None,
              help="ISO date/time cutoff. Overrides the last-run watermark.")
@click.option("--resume/--no-resume", default=True,
              help="Resume from the last successful fetch watermark when --since is not given.")
@click.option("--skip-weekends", is_flag=True,
              help="Skip orders placed on Saturday or Sunday.")
@click.option("--no-fetch", is_flag=True,
              help="Skip the fetch step. Use when re-running over an existing inbox.")
@click.option("--no-upload", is_flag=True,
              help="Stop after rename. Verify decisions stay in ledger; nothing reaches Chrome River.")
@click.option("--auto", is_flag=True,
              help="In verify, auto-resolve flagged receipts (prefer listing, fall back to PDF). "
                   "No prompts. Use for unattended runs.")
@click.pass_context
def run(ctx: click.Context, merchant: str, since: str | None, resume: bool,
        skip_weekends: bool, no_fetch: bool, no_upload: bool, auto: bool) -> None:
    """End-to-end pipeline: reconcile -> fetch -> parse -> verify -> rename -> upload.

    Each step writes its outcome to the ledger, so a partial run is resumable —
    re-run with --no-fetch to pick up from wherever the last attempt died."""
    console.print("\n[bold cyan]== Step 1: reconcile ==[/bold cyan]")
    ctx.invoke(reconcile, verbose=False)

    if not no_fetch:
        console.print("\n[bold cyan]== Step 2: fetch ==[/bold cyan]")
        ctx.invoke(fetch, merchant=merchant, since=since, resume=resume,
                   skip_weekends=skip_weekends)
    else:
        console.print("\n[dim]== Step 2: fetch skipped (--no-fetch) ==[/dim]")

    console.print("\n[bold cyan]== Step 3: parse pending ==[/bold cyan]")
    ctx.invoke(parse_pending)

    console.print("\n[bold cyan]== Step 4: verify ==[/bold cyan]")
    ctx.invoke(verify, auto=auto)

    console.print("\n[bold cyan]== Step 5: rename ==[/bold cyan]")
    ctx.invoke(rename, dry_run=False)

    if not no_upload:
        console.print("\n[bold cyan]== Step 6: upload ==[/bold cyan]")
        ctx.invoke(upload, dry_run=False)
    else:
        console.print("\n[dim]== Step 6: upload skipped (--no-upload) ==[/dim]")
        # Show what's queued so the user knows what re-run would do.
        v = len(list_by_status("verified"))
        na = len(list_by_status("needs_approval"))
        console.print(
            f"[cyan]  verified (ready to draft): {v}  needs_approval: {na}[/cyan]"
        )

    # Final snapshot.
    console.print("\n[bold green]== Run complete ==[/bold green]")
    snapshot = {}
    for st in ("fetched", "parsed", "verified", "skipped", "needs_approval", "drafted"):
        n = len(list_by_status(st))
        if n:
            snapshot[st] = n
    if snapshot:
        for st, n in snapshot.items():
            console.print(f"  {st:<16} {n}")


if __name__ == "__main__":
    main()
