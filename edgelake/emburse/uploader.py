from __future__ import annotations

from datetime import date as Date, timedelta
from pathlib import Path

from playwright.sync_api import Page
from rich.console import Console

from ..config import DEBUG_DIR, DEFAULT_CATEGORY, DEFAULT_LOCATION, DEFAULT_PROJECT_CODE
from ..parsers.pdf import Receipt

console = Console()


def report_name(merchant: str, latest_date: Date) -> str:
    iso_year, iso_week, iso_dow = latest_date.isocalendar()
    monday = latest_date - timedelta(days=iso_dow - 1)
    sunday = monday + timedelta(days=6)
    return (
        f"{merchant} - {iso_year}-W{iso_week:02d} "
        f"({monday.strftime('%d %b')} - {sunday.strftime('%d %b')})"
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
    latest = max(r.date for r in receipts)
    name = report_name(merchant, latest)

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

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1200)
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

    page.wait_for_timeout(500)
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

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    _snap(page, "03_after_save")

    # Step 3: add each receipt as a line item.
    for i, receipt in enumerate(receipts):
        console.print(f"\n[cyan]  Adding line item {i+1}/{len(receipts)}:[/cyan] {receipt.source_path.name}")
        if not _add_expense_line(page, receipt, i):
            console.print(f"[red]  Failed on line item {i+1}[/red]")
            return False

    _snap(page, "10_all_lines_done")
    console.print("\n[green]All line items added. Report saved as draft.[/green]")
    return True


def _add_expense_line(page: Page, receipt: Receipt, idx: int) -> bool:
    """Click Create New in the right panel, fill the expense form, attach PDF, save."""
    # Click "Create New" in the right-panel "Add Expenses" section.
    if not _click_first(
        page,
        [
            'text="Create New"',
            'a:has-text("Create New"):visible',
            'button:has-text("Create New"):visible',
        ],
        "Create New",
    ):
        _snap(page, f"99_create_new_not_found_{idx}")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(1000)

    # Click the MEALS / ENTERTAINMENT tile to expand sub-categories.
    if not _click_first(
        page,
        [
            'text="MEALS / ENTERTAINMENT"',
            'text="Meals / Entertainment"',
        ],
        "Meals/Entertainment tile",
    ):
        _snap(page, f"99_meals_tile_not_found_{idx}")
        return False

    page.wait_for_timeout(800)

    # Click MEALS / DRINKS sub-tile.
    if not _click_first(
        page,
        [
            'text="MEALS / DRINKS"',
            'text="Meals / Drinks"',
        ],
        "Meals/Drinks sub-tile",
    ):
        _snap(page, f"99_meals_drinks_not_found_{idx}")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
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
    amount_str = f"{receipt.amount:.2f}"
    try:
        loc = page.locator('input[name="amount"]').first
        loc.click(click_count=3, timeout=4000)
        loc.fill(amount_str, timeout=4000)
        page.keyboard.press("Tab")
        console.print(f"[green]  filled Spent:[/green] {amount_str}")
    except Exception as e:
        console.print(f"[yellow]  could not fill amount: {e}[/yellow]")
    page.wait_for_timeout(400)

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
    # Field name on the form is not yet pinned down; try common Chrome River
    # patterns. Skip silently if empty so users who don't need it aren't blocked.
    if DEFAULT_PROJECT_CODE:
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
    try:
        file_input = page.locator('input[type="file"][name="file"]')
        file_input.set_input_files(str(receipt.source_path), timeout=10000)
        page.wait_for_timeout(2000)
        console.print(f"[green]  attached receipt:[/green] {receipt.source_path.name}")
    except Exception as e:
        console.print(f"[yellow]  could not attach receipt: {e}[/yellow]")

    _snap(page, f"06_after_attach_{idx}")

    # --- Save the line item ---
    if not _click_first(
        page,
        ['button:has-text("Save"):visible', 'text=Save'],
        "Save line item",
    ):
        _snap(page, f"99_save_lineitem_not_found_{idx}")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
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
