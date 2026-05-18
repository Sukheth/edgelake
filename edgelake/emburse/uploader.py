from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date as Date, timedelta
from pathlib import Path

from playwright.sync_api import Page
from rich.console import Console

from ..config import DEBUG_DIR, DEFAULT_CATEGORY, DEFAULT_LOCATION, DEFAULT_PROJECT_CODE
from ..parsers.pdf import Receipt

console = Console()


@contextmanager
def _step(label: str, indent: int = 2):
    """Time a phase and print start/end. Helps spot slow steps without
    sprinkling time.monotonic() everywhere."""
    pad = " " * indent
    start = time.monotonic()
    console.print(f"{pad}[cyan]> {label}[/cyan]")
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        # Highlight slow steps; >3s is suspicious for SPA clicks.
        color = "yellow" if elapsed >= 3.0 else "dim"
        console.print(f"{pad}[{color}]< {label} ({elapsed:.2f}s)[/{color}]")


def _wait_for_loader(page: Page, timeout_ms: int = 15000) -> bool:
    """Wait for Chrome River's AJAX loader overlay to disappear. The loader
    div ([data-qa="appAjaxLoader"]) intercepts pointer events and silently
    breaks clicks — e.g. fields appear interactive but actually aren't.
    Returns True if the loader is gone (or never appeared)."""
    try:
        page.locator('[data-qa="appAjaxLoader"]').first.wait_for(
            state="hidden", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


def _is_visible(page: Page, selector: str, timeout_ms: int = 200) -> bool:
    """Cheap visibility probe used by _page_state."""
    try:
        return bool(page.locator(selector).first.is_visible(timeout=timeout_ms))
    except Exception:
        return False


def _page_state(page: Page, label: str) -> None:
    """Snapshot the page's current control state. Run this at decision
    points (entry to a line-item, after a save, before/after key clicks)
    so the console history shows exactly which Chrome River view we're on."""
    try:
        url = page.url
    except Exception:
        url = "?"
    probes = {
        "Create New": 'text="Create New"',
        "MEALS tile": 'text="MEALS / ENTERTAINMENT"',
        "Add New Expense (+)": '[aria-label="Add New Expense"]',
        "Date input": 'input[name="transactionDate"]',
        "Amount input": 'input[name="amount"]',
        "Save button": 'button:has-text("Save"):visible',
        "Edit button": 'button:has-text("Edit"):visible',
        "Validation banner": ':has-text("Please enter an amount greater than zero")',
    }
    found = {k: _is_visible(page, sel, timeout_ms=150) for k, sel in probes.items()}
    visible = [k for k, v in found.items() if v]
    hidden = [k for k, v in found.items() if not v]
    console.print(f"    [dim]page-state[/dim] @ {label}: url={url}")
    console.print(f"    [dim]  visible:[/dim] {visible}")
    console.print(f"    [dim]  hidden:[/dim] {hidden}")


def report_name(merchant: str, earliest_date: Date, latest_date: Date) -> str:
    """Name the report after the actual span of receipts in the batch.

    Single day:        'Blinkit - 04 May 2026'
    Same year span:    'Blinkit - 04 May - 15 May 2026'
    Cross-year span:   'Blinkit - 28 Dec 2026 - 03 Jan 2027'
    """
    if earliest_date == latest_date:
        return f"{merchant} - {earliest_date.strftime('%d %b %Y')}"
    if earliest_date.year == latest_date.year:
        return (
            f"{merchant} - {earliest_date.strftime('%d %b')} "
            f"- {latest_date.strftime('%d %b %Y')}"
        )
    return (
        f"{merchant} - {earliest_date.strftime('%d %b %Y')} "
        f"- {latest_date.strftime('%d %b %Y')}"
    )


def _snap(page: Page, label: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{label}.png"
    page.screenshot(path=str(path), full_page=True)
    console.print(f"[dim]  snap -> {path.name}[/dim]")


def _click_first(page: Page, selectors: list[str], label: str, timeout_ms: int = 5000) -> bool:
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=timeout_ms)
            console.print(f"[green]  clicked {label} via:[/green] {sel}")
            return True
        except Exception:
            continue
    console.print(f"[red]  could not find {label}[/red]")
    return False


def create_report(page: Page, receipts: list[Receipt]) -> bool:
    """Click + Create, fill the report header, click Save.
    Stops after Save so we can capture the next-page layout."""
    if not receipts:
        return False
    merchant = receipts[0].merchant
    earliest = min(r.date for r in receipts)
    latest = max(r.date for r in receipts)
    name = report_name(merchant, earliest, latest)

    console.print(f"\n[bold cyan]Creating report:[/bold cyan] {name}")
    console.print(f"  receipts in this report: {len(receipts)}")
    for r in receipts:
        console.print(f"    - {r.source_path.name}: {r.date} {r.currency} {r.amount:.2f}")

    _snap(page, "00_dashboard")

    # Click "+ Create" on the Expenses card.
    if not _click_first(
        page,
        [
            'button:has-text("Create"):visible',
            'a:has-text("Create"):visible',
            '[aria-label*="Create" i]:visible',
            'text=Create',
        ],
        "Create",
    ):
        _snap(page, "99_create_not_found")
        return False

    # Poll for the Report Name input — that's the first field on the report
    # form. As soon as it's visible, the form is ready; no blind wait.
    try:
        page.locator(
            'input[placeholder*="Report Name" i], '
            'label:has-text("Report Name") ~ * input'
        ).first.wait_for(state="visible", timeout=5000)
    except Exception:
        # Fall back to a short blind wait if the selector misses.
        page.wait_for_timeout(400)
    _snap(page, "01_report_form")

    # Fill Report Name. The visible input next to "Report Name" label.
    name_filled = False
    for sel in [
        'input[placeholder*="Report Name" i]',
        'label:has-text("Report Name") + * input',
        'label:has-text("Report Name") ~ * input',
        'xpath=//label[contains(.,"Report Name")]/following::input[1]',
        'input[type="text"]:visible >> nth=0',
    ]:
        try:
            page.locator(sel).first.fill(name, timeout=4000)
            console.print(f"[green]  filled Report Name via:[/green] {sel}")
            name_filled = True
            break
        except Exception:
            continue
    if not name_filled:
        console.print("[red]  could not fill Report Name[/red]")
        _snap(page, "99_name_not_filled")
        return False

    _snap(page, "02_report_name_filled")

    # Dump all visible buttons for diagnostics.
    buttons = page.locator("button:visible").all_text_contents()
    console.print(f"[dim]  visible buttons: {buttons}[/dim]")

    # Click Save.
    if not _click_first(
        page,
        [
            'button:has-text("Save"):visible',
            'button:has-text("Save")',
            'button >> text=Save',
            'text=Save',
            '[class*="save" i]',
        ],
        "Save",
    ):
        _snap(page, "99_save_not_found")
        return False

    # Poll for either 'Create New' or the MEALS tile to appear, whichever
    # comes first. Two short probes is faster than one long blind wait.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        ready = False
        for sel in ('text="Create New"', 'text="MEALS / ENTERTAINMENT"'):
            try:
                if page.locator(sel).first.is_visible(timeout=200):
                    ready = True
                    break
            except Exception:
                continue
        if ready:
            break
    _snap(page, "03_after_save")

    # Step 3: add each receipt as a line item.
    for i, receipt in enumerate(receipts):
        console.print(f"\n[bold cyan]== Line item {i+1}/{len(receipts)}: {receipt.source_path.name} ==[/bold cyan]")
        _page_state(page, f"start of line {i+1}")
        with _step(f"line item {i+1} total"):
            if not _add_expense_line(page, receipt, i):
                console.print(f"[red]  Failed on line item {i+1}[/red]")
                _page_state(page, f"after line {i+1} failure")
                return False

    _snap(page, "10_all_lines_done")
    console.print("\n[green]All line items added. Report saved as draft.[/green]")
    return True


def _add_expense_line(page: Page, receipt: Receipt, idx: int) -> bool:
    """Open the expense-type tile picker, pick the category, fill the form,
    attach PDF, save. On the first line item the picker is reached via
    'Create New'; on subsequent line items Chrome River drops you straight
    back into the picker after Save, so 'Create New' isn't an actionable
    button — we skip it whenever the tiles are already visible."""
    # Entry into the expense form:
    #   - First line item (idx == 0): the report-detail view is showing the
    #     tile picker on the right. We click 'Create New' (the highlighted
    #     left-rail item) just to be safe — on most tenants the tiles are
    #     already actionable but this preserves the original first-line flow.
    #   - Subsequent line items: after Save, Chrome River drops us on the
    #     line-item detail view WITHOUT the tile picker. We must click the
    #     circular blue '+' button labelled 'Add New Expense' to reopen it.
    #     That element is a <div role="button"> (not a real <button>), so
    #     get_by_role on the accessible name is the canonical selector.
    # Canonical, single-selector pins — no fallbacks. data-qa is Chrome
    # River's stable automation hook; if it isn't there we WANT to fail
    # loudly rather than risk clicking a breadcrumb or stale tile.
    tile_selectors = ['[data-qa~="mosaicMeals/EntertainmentDrawer"]']
    drinks_selectors = ['[data-qa~="mosaicMeals/DrinksTile"]']

    # Universal entry: PROBE FIRST, don't touch URL if picker is already open.
    #
    # Empirical Chrome River behavior after saving a line item: the URL may
    # still contain /lineitem/<uuid>, but the right pane already re-renders
    # to show the "Add Expenses → Create New" tile picker, with the tiles
    # FULLY INTERACTIVE. So URL-strip is destructive — it navigates away
    # from the live picker.
    #
    # Priority:
    #   1. MEALS tile already visible+interactive -> click it directly.
    #   2. "Create New" rail item visible -> click it (refreshes the picker).
    #   3. '+' button visible -> click it (opens a fresh picker).
    #   4. Nothing visible -> last-resort URL strip + '+'.
    with _step(f"entry into form (idx={idx})"):
        plus_selectors = ['[data-qa="addExpenseBtn"]:visible']

        # Step 1: is the MEALS tile already there and interactive?
        # If yes, we skip clicking any preamble — the picker is live.
        meals_already_visible = False
        try:
            loc = page.locator(tile_selectors[0]).first
            if loc.is_visible(timeout=500):
                # Make sure it's actually interactive, not a stale ghost.
                try:
                    bbox = loc.bounding_box(timeout=300)
                    if bbox and bbox["width"] > 5 and bbox["height"] > 5:
                        meals_already_visible = True
                except Exception:
                    meals_already_visible = True
        except Exception:
            pass

        if meals_already_visible:
            console.print("[green]    MEALS tile already visible — picker is live[/green]")
        else:
            # Step 2: try Create New (the rail item that refreshes the picker).
            clicked = False
            try:
                cn = page.locator('text="Create New"').first
                if cn.is_visible(timeout=500):
                    cn.click(timeout=3000)
                    console.print("[green]    clicked Create New[/green]")
                    clicked = True
            except Exception:
                pass

            # Step 3: try '+' button.
            if not clicked:
                for sel in plus_selectors:
                    try:
                        loc = page.locator(sel)
                        if loc.first.is_visible(timeout=500):
                            loc.first.click(timeout=3000)
                            console.print(f"[green]    clicked '+' via:[/green] {sel}")
                            clicked = True
                            break
                    except Exception:
                        continue

            # Step 4: last-resort URL strip + '+'.
            if not clicked:
                try:
                    cur = page.url
                    if "/lineitem/" in cur:
                        new_url = cur.split("/lineitem/")[0]
                        console.print(f"[dim]    last resort: URL strip to:[/dim] {new_url}")
                        page.goto(new_url, wait_until="domcontentloaded", timeout=8000)
                        page.wait_for_timeout(800)
                        for sel in plus_selectors:
                            try:
                                loc = page.locator(sel)
                                if loc.first.is_visible(timeout=1500):
                                    loc.first.click(timeout=3000)
                                    console.print(f"[green]    clicked '+' (post-strip) via:[/green] {sel}")
                                    clicked = True
                                    break
                            except Exception:
                                continue
                except Exception as e:
                    console.print(f"[yellow]    URL strip failed: {e}[/yellow]")

            if not clicked:
                _page_state(page, f"no entry control found (idx={idx})")
                _snap(page, f"99_entry_not_found_{idx}")
                return False

            # Wait for the tile picker to be ready.
            try:
                page.locator(tile_selectors[0]).first.wait_for(
                    state="visible", timeout=5000
                )
            except Exception:
                page.wait_for_timeout(800)

    # Click the MEALS / ENTERTAINMENT tile to expand sub-categories.
    with _step(f"click MEALS tile (idx={idx})"):
        if not _click_first(
            page,
            tile_selectors,
            "Meals/Entertainment tile",
        ):
            _page_state(page, f"MEALS tile not found (idx={idx})")
            _snap(page, f"99_meals_tile_not_found_{idx}")
            return False

    # Wait for the parent drawer to actually open before clicking Drinks.
    # The parent's aria-expanded flips to "true" when clicked. Without this
    # guard, the Drinks selector might match a tile that exists in the DOM
    # but isn't the active sub-tile of the just-opened drawer.
    parent_expanded = False
    try:
        page.locator(
            '[data-qa~="mosaicMeals/EntertainmentDrawer"][aria-expanded="true"]'
        ).first.wait_for(state="visible", timeout=3000)
        parent_expanded = True
    except Exception:
        page.wait_for_timeout(400)
    console.print(f"    [dim]parent drawer expanded: {parent_expanded}[/dim]")
    if not parent_expanded:
        console.print(
            "[yellow]    MEALS parent did not expand — Drinks click would "
            "match the wrong tile. Bailing.[/yellow]"
        )
        _page_state(page, f"parent not expanded (idx={idx})")
        _snap(page, f"99_parent_not_expanded_{idx}")
        return False

    # Poll for the MEALS / DRINKS sub-tile.
    try:
        page.locator(drinks_selectors[0]).first.wait_for(
            state="visible", timeout=2500
        )
    except Exception:
        page.wait_for_timeout(400)

    # Click MEALS / DRINKS sub-tile.
    if not _click_first(
        page,
        drinks_selectors,
        "Meals/Drinks sub-tile",
    ):
        _snap(page, f"99_meals_drinks_not_found_{idx}")
        return False

    # Wait for the AJAX loader (form load triggered by Drinks click) to clear.
    _wait_for_loader(page)
    # Poll for the Date input becoming both visible AND editable. After
    # clicking the Drinks sub-tile, Chrome River shows a loading overlay
    # while Kendo widgets initialize — fields start disabled and flip to
    # enabled when ready. Wait for the enabled state instead of bailing
    # on the transient disabled state.
    form_ready = False
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            loc = page.locator('input[name="transactionDate"]').first
            if loc.is_visible(timeout=200):
                disabled = loc.get_attribute("disabled", timeout=200)
                readonly = loc.get_attribute("readonly", timeout=200)
                if not disabled and not readonly:
                    form_ready = True
                    break
        except Exception:
            pass
        page.wait_for_timeout(250)
    console.print(f"    [dim]expense form ready: {form_ready}[/dim]")
    _snap(page, f"04_expense_form_{idx}")

    # --- Fill Transaction Date (name=transactionDate) ---
    date_str = receipt.date.strftime("%m/%d/%Y")
    try:
        loc = page.locator('input[name="transactionDate"]').first
        loc.click(click_count=3, timeout=4000)
        loc.fill(date_str, timeout=4000)
        page.keyboard.press("Tab")
        console.print(f"[green]  filled Date:[/green] {date_str}")
    except Exception as e:
        console.print(f"[yellow]  could not fill date: {e}[/yellow]")
    page.wait_for_timeout(400)

    # --- Fill Spent amount (name=amount) ---
    # The AJAX loader can intercept clicks for several seconds after the
    # Drinks-tile triggers form load. Wait, fill, then VERIFY — if the value
    # is empty, the loader was intercepting during fill; retry up to 3x.
    amount_str = f"{receipt.amount:.2f}"
    amount_ok = False
    for attempt in range(3):
        _wait_for_loader(page)
        try:
            loc = page.locator('input[name="amount"]').first
            loc.click(click_count=3, timeout=5000)
            loc.fill(amount_str, timeout=5000)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            # Verify the value stuck. Chrome River reformats the value
            # (e.g. "262.00" -> "262.00"), so we compare the numeric part.
            actual = loc.input_value(timeout=1000)
            if actual and actual.replace(",", "").strip() not in ("", "0", "0.00"):
                console.print(f"[green]  filled Spent:[/green] {amount_str} (got: {actual})")
                amount_ok = True
                break
            console.print(f"[yellow]  amount empty after fill (attempt {attempt+1}); retrying[/yellow]")
        except Exception as e:
            console.print(f"[yellow]  amount fill error (attempt {attempt+1}): {e}[/yellow]")
        page.wait_for_timeout(500)
    if not amount_ok:
        console.print("[red]  FAILED to enter amount after 3 attempts — aborting line[/red]")
        _snap(page, f"99_amount_blank_{idx}")
        return False
    page.wait_for_timeout(300)

    # --- Location (name=VATLocation, ComboBox) — value from DEFAULT_LOCATION ---
    try:
        loc = page.locator('input[name="VATLocation"]').first
        loc.click(timeout=4000)
        loc.fill(DEFAULT_LOCATION, timeout=4000)
        page.wait_for_timeout(600)
        page.locator(
            f'[role="option"]:has-text("{DEFAULT_LOCATION}"), li:has-text("{DEFAULT_LOCATION}"):visible'
        ).first.click(timeout=5000)
        console.print(f"[green]  set Location:[/green] {DEFAULT_LOCATION}")
    except Exception as e:
        console.print(f"[yellow]  could not set Location: {e}[/yellow]")

    page.wait_for_timeout(400)

    # --- Type of Meal (name=TypeFoodBeverage) → Chocolate/Dessert/Snacks ---
    try:
        loc = page.locator('input[name="TypeFoodBeverage"]').first
        loc.click(timeout=4000)
        loc.fill("Chocolate", timeout=4000)
        page.wait_for_timeout(800)
        page.locator('[role="option"]:has-text("Chocolate"), li:has-text("Chocolate"):visible').first.click(timeout=5000)
        console.print("[green]  set Type of Meal: Chocolate/Dessert/Snacks[/green]")
    except Exception as e:
        console.print(f"[yellow]  could not set Type of Meal: {e}[/yellow]")

    page.wait_for_timeout(400)

    # --- Project / Expense Code — value from DEFAULT_PROJECT_CODE ---
    # ONLY on the first line item. Chrome River auto-fills this from the
    # previous line in the same report, so re-filling is just expensive
    # noise. Skip it whenever idx > 0.
    if DEFAULT_PROJECT_CODE and idx == 0:
        project_selectors = [
            'input[name="ProjectCode"]',
            'input[name="projectCode"]',
            'input[name="BusinessUnit"]',
            'input[name="allocation"]',
            'input[name="Allocation"]',
            'input[aria-label*="Project" i]',
            'input[aria-label*="Expense Code" i]',
            'input[placeholder*="Project" i]',
            'input[placeholder*="Code" i]',
            'label:has-text("Project") ~ * input',
            'label:has-text("Expense Code") ~ * input',
            'label:has-text("Allocation") ~ * input',
        ]
        filled = False
        for sel in project_selectors:
            try:
                loc = page.locator(sel).first
                loc.click(timeout=2500)
                loc.fill(DEFAULT_PROJECT_CODE, timeout=2500)
                page.wait_for_timeout(700)
                try:
                    page.locator(
                        f'[role="option"]:has-text("{DEFAULT_PROJECT_CODE}"), '
                        f'li:has-text("{DEFAULT_PROJECT_CODE}"):visible'
                    ).first.click(timeout=3000)
                except Exception:
                    page.keyboard.press("Tab")
                console.print(f"[green]  set Project Code:[/green] {DEFAULT_PROJECT_CODE} (via {sel})")
                filled = True
                break
            except Exception:
                continue
        if not filled:
            console.print(
                f"[yellow]  could not find Project Code field — "
                f"check 05_form_filled_{idx}.png and add the right selector[/yellow]"
            )
        page.wait_for_timeout(400)

    _snap(page, f"05_form_filled_{idx}")

    # --- Attach the PDF receipt ---
    # File input is input[type="file"][name="file"] — hidden, set via set_input_files.
    with _step(f"attach PDF (idx={idx})"):
        try:
            file_input = page.locator('input[type="file"][name="file"]')
            file_input.set_input_files(str(receipt.source_path), timeout=10000)
            console.print(f"[green]    attached receipt:[/green] {receipt.source_path.name}")
        except Exception as e:
            console.print(f"[yellow]    could not attach receipt: {e}[/yellow]")

    # Wait for the upload to complete. The visible signals are:
    #   - The circular progress spinner (k-loading-image, or [data-qa
    #     containing 'progress'/'spinner']) disappears.
    #   - The Attachments section shows a thumbnail image / preview tile.
    # We try a few selectors and accept whichever fires first. Cap at 45s
    # for slow PDFs.
    with _step(f"wait for PDF upload (idx={idx})"):
        upload_done = False
        deadline = time.monotonic() + 45.0
        # Signals that the upload finished. Any one of these matching means
        # we're safe to click Save.
        done_selectors = [
            'text=/Attachments\\s*\\(\\s*[1-9]\\d*\\s*\\)/',
            '.attachments-section img:visible',
            '.attachment-thumbnail:visible',
            '[data-qa~="attachmentThumb"]:visible',
            'img[src*="attachment"]:visible',
        ]
        # Signal that an upload is in progress. As long as ANY of these
        # is visible, we keep waiting.
        progress_selectors = [
            '.k-loading-image:visible',
            '.cr-loader:visible',
            '[class*="progress"]:visible',
            '[role="progressbar"]:visible',
        ]
        # First poll for any done-signal becoming true.
        while time.monotonic() < deadline:
            for sel in done_selectors:
                try:
                    if page.locator(sel).first.is_visible(timeout=200):
                        upload_done = True
                        break
                except Exception:
                    continue
            if upload_done:
                break
            # Otherwise: if no progress indicator is visible AND we've
            # waited at least 2s, assume the upload completed too fast to
            # observe and move on.
            any_progress = False
            for sel in progress_selectors:
                try:
                    if page.locator(sel).first.is_visible(timeout=100):
                        any_progress = True
                        break
                except Exception:
                    continue
            if not any_progress and (time.monotonic() - (deadline - 45.0)) > 2.0:
                upload_done = True
                console.print("    [dim]    no progress signal — assuming upload complete[/dim]")
                break
            page.wait_for_timeout(300)
        console.print(f"    [dim]upload done: {upload_done}[/dim]")

    _snap(page, f"06_after_attach_{idx}")

    # Probe state right before Save — tells us whether Save is even on screen.
    _page_state(page, f"pre-Save (idx={idx})")

    # --- Save the line item ---
    # Wait for the AJAX loader from the PDF attach to clear before Save —
    # otherwise the click is intercepted and the locator times out.
    _wait_for_loader(page)
    # Scope Save to the visible header-bar Save button. Chrome River has
    # multiple "Save" texts in the DOM (mobile menus, hidden side panels);
    # the actionable one is the primary button in the top-right header.
    with _step(f"save line item (idx={idx})"):
        if not _click_first(
            page,
            [
                'button.btn-primary:has-text("Save"):visible',
                'button:has-text("Save"):visible',
                'header button:has-text("Save")',
                '[data-qa="saveBtn"]:visible',
                'text=Save',
            ],
            "Save line item",
        ):
            _snap(page, f"99_save_lineitem_not_found_{idx}")
            return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    # After save, Chrome River re-renders. Wait for either the tile picker
    # (next-line common case) or the expense row in the report list to be
    # visible, replacing a blind 2s wait.
    try:
        page.locator(
            'text="MEALS / ENTERTAINMENT", text="Meals / Drinks"'
        ).first.wait_for(state="visible", timeout=4000)
    except Exception:
        page.wait_for_timeout(1000)
    _snap(page, f"07_after_lineitem_save_{idx}")

    console.print(f"[green]  line item {idx+1} saved.[/green]")
    return True


def upload_draft(page: Page, receipt: Receipt, dry_run: bool = False) -> str | None:
    """Single-receipt entry point (legacy). Prefer the batch flow via cli.upload."""
    if dry_run:
        console.print(f"[dim]  (dry-run) {receipt.source_path.name}[/dim]")
        return None
    create_report(page, [receipt])
    return None
