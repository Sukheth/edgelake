from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright
from rich.console import Console

from ..config import BLINKIT_PROFILE_DIR, BLINKIT_URL, DEBUG_DIR, INBOX, PROCESSED
from ..ledger import (
    get_by_order_id,
    hash_file,
    known_order_ids,
    upsert_fetched,
)

console = Console()

LOGIN_HINTS = ("login", "otp", "verify", "phone")


def _safe_url(page: Page) -> str:
    try:
        live = page.evaluate("() => window.location.href")
        if isinstance(live, str) and live:
            return live
    except Exception:
        pass
    try:
        return page.url
    except Exception:
        return ""


def _snap(page: Page, label: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"blinkit_{label}.png"
    page.screenshot(path=str(path), full_page=True)
    console.print(f"[dim]  snap -> {path.name}[/dim]")
    return path


def _is_orders_page(url: str) -> bool:
    u = url.lower()
    return "blinkit.com" in u and "/account/orders" in u


def _login_modal_visible(page: Page) -> bool:
    """Blinkit overlays a login modal on top of /account/orders without
    changing the URL, so URL alone is not enough to know we're 'in'."""
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const body = (document.body && document.body.innerText) || '';
                  // Phrases unique to the login/OTP modal.
                  const hits = [
                    "India's last minute app",
                    "Log in or Sign up",
                    "Enter OTP",
                    "Verify",
                  ];
                  return hits.some(h => body.includes(h));
                }
                """
            )
        )
    except Exception:
        return False


def _orders_list_rendered(page: Page) -> bool:
    """Heuristic: real orders page has 'Order' / 'Invoice' / order-id-like
    text repeated, AND the login modal isn't on top."""
    if _login_modal_visible(page):
        return False
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const t = (document.body && document.body.innerText) || '';
                  // 'Order ID' or 'ORD' codes are reliable order-list markers.
                  if (/order id/i.test(t)) return true;
                  if (/\\bORD\\d{6,}\\b/.test(t)) return true;
                  // Fallback: 'My Orders' header + at least one date-looking line.
                  if (/my orders/i.test(t) && /\\b\\d{1,2}\\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(t)) {
                    return true;
                  }
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


def _wait_for_orders(context: BrowserContext, timeout_s: int = 600) -> Page:
    """Poll until any page is on /account/orders AND the login modal is gone
    AND order-list markers are visible in the DOM. First run requires manual
    phone+OTP login; the script politely waits without spamming."""
    console.print(
        "\n[bold yellow]>>> Log in to Blinkit (phone + OTP) in the Chromium window.[/bold yellow]"
    )
    console.print(
        f"[yellow]    Waiting up to {timeout_s}s for the orders list to render.[/yellow]\n"
    )
    deadline = time.monotonic() + timeout_s
    last_state = ""
    while time.monotonic() < deadline:
        pages = [p for p in context.pages if not p.is_closed()]
        if not pages:
            raise RuntimeError("All browser tabs were closed before orders page loaded.")
        for p in pages:
            url = _safe_url(p)
            if not _is_orders_page(url):
                continue
            if _login_modal_visible(p):
                state = "waiting: login modal still visible"
                if state != last_state:
                    console.print(f"[dim]  {state}[/dim]")
                    last_state = state
                continue
            if _orders_list_rendered(p):
                console.print(f"[green]Orders list rendered:[/green] {url}")
                try:
                    p.bring_to_front()
                except Exception:
                    pass
                try:
                    p.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                time.sleep(2)
                return p
            state = "waiting: orders list not yet in DOM"
            if state != last_state:
                console.print(f"[dim]  {state}[/dim]")
                last_state = state
        time.sleep(1.5)
    raise TimeoutError(f"Did not reach a rendered Blinkit orders page within {timeout_s}s.")


@contextmanager
def blinkit_session(headless: bool = False) -> Iterator[Page]:
    BLINKIT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(BLINKIT_PROFILE_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 900},
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            console.print(f"[cyan]Navigating to:[/cyan] {BLINKIT_URL}")
            try:
                page.goto(BLINKIT_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                console.print(f"[yellow]Initial nav note: {e}[/yellow]")
            orders_page = _wait_for_orders(context)
            yield orders_page
        finally:
            context.close()


def _dismiss_address_modal(page: Page) -> None:
    """Blinkit shows a 'Welcome to blinkit / provide your delivery location'
    modal on top of /account/orders. Clicking a saved address opens a
    secondary 'Change Location' modal that DOES have an explicit close (x)
    button at the top-right. Strategy: open one then close the other so
    we end up with no modal at all and the orders list is interactive."""
    # Skip entirely if no modal is up.
    try:
        modal_present = page.evaluate(
            "() => /provide your delivery location|Change Location/i.test((document.body&&document.body.innerText)||'')"
        )
        if not modal_present:
            console.print("[dim]  no address modal present, skipping dismiss[/dim]")
            return
    except Exception:
        pass

    # Step 1: trigger transition to 'Change Location' by picking any saved address.
    saved_labels = [
        'ITC Grand Chola', 'Work', 'Pullman Chennai', 'Courtyard Chennai',
        'Hours Gurgaon', 'House Gurgaon', 'Octolife',
    ]
    transitioned = False
    for label in saved_labels:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=1200)
            console.print(f"[dim]  picked saved address '{label}'[/dim]")
            transitioned = True
            page.wait_for_timeout(700)
            break
        except Exception:
            continue
    # Step 2: close the 'Change Location' modal. Blinkit's x icon isn't a
    # real button — try selectors first, then fall back to a pixel click
    # at the location we observed in the screenshot.
    closers = [
        'text="Change Location" >> xpath=ancestor::*[position()<=4]//*[@aria-label="close" or contains(@class,"close")]',
        '[role="dialog"] [aria-label*="close" i]',
        '[role="dialog"] button:has(svg)',
        '[class*="modal" i] [class*="close" i]',
    ]
    closed = False
    for sel in closers:
        try:
            page.locator(sel).first.click(timeout=1200)
            console.print(f"[dim]  closed modal via {sel}[/dim]")
            page.wait_for_timeout(600)
            closed = True
            break
        except Exception:
            continue
    if not closed:
        # Pixel-click the x at the modal's top-right corner.
        # From the screenshot the modal spans roughly x=58..290, y=43..330.
        # The x icon sits at ~(285, 53).
        try:
            page.mouse.click(285, 53)
            console.print("[dim]  closed modal via pixel click @(285,53)[/dim]")
            page.wait_for_timeout(600)
            closed = True
        except Exception as e:
            console.print(f"[yellow]  pixel close failed: {e}[/yellow]")

    # Step 3: Escape as last resort.
    if not closed:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass

    # Verify modal is gone. 'Welcome to blinkit' appears in the page logo too
    # so it's an unreliable signal; check for the more distinctive phrases.
    try:
        residual = page.evaluate(
            """
            () => {
              const t = (document.body && document.body.innerText) || '';
              return /provide your delivery location|Change Location/i.test(t);
            }
            """
        )
        if residual:
            console.print("[yellow]  modal STILL present after all dismiss attempts[/yellow]")
        else:
            console.print("[dim]  modal cleared[/dim]")
    except Exception:
        pass


def _modal_still_visible(page: Page) -> bool:
    try:
        return bool(page.evaluate(
            "() => (document.body && document.body.innerText || '').includes('provide your delivery location')"
        ))
    except Exception:
        return False


def _dump_all_anchors(page: Page) -> list[dict]:
    """Honest dump: every anchor on the page with its href + short text.
    Used to find the real order-row link shape before guessing."""
    return page.evaluate(
        """
        () => {
          const out = [];
          for (const a of document.querySelectorAll('a')) {
            const href = a.getAttribute('href') || '';
            const text = (a.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
            out.push({href, text});
          }
          return out;
        }
        """
    )


def _debug_dump_phrase(page: Page) -> None:
    """Diagnostic: count 'Arrived in N minutes' and '#<id>' occurrences in
    the live DOM so we know whether the JS is matching the right text."""
    info = page.evaluate(
        """
        () => {
          const body = document.body ? (document.body.innerText || '') : '';
          const phraseMatches = body.match(/Arrived in\\s+\\d+\\s+minute/gi) || [];
          const idMatches = body.match(/#\\d{2,}/g) || [];
          // Sample a slice around the first phrase.
          let snippet = '';
          const idx = body.search(/Arrived in/i);
          if (idx >= 0) snippet = body.slice(idx, idx + 120);
          return {
            body_len: body.length,
            phrase_count: phraseMatches.length,
            id_count: idMatches.length,
            id_sample: idMatches.slice(0, 8),
            snippet,
          };
        }
        """
    )
    console.print(
        _ascii(
            f"  body_len={info['body_len']}  phrases={info['phrase_count']}  "
            f"ids={info['id_count']}  id_sample={info['id_sample']}  "
            f"snippet={info['snippet']!r}"
        )
    )


def _load_all_orders(
    page: Page,
    since_dt: datetime | None = None,
    ref_year: int | None = None,
    max_passes: int = 25,
) -> list[dict]:
    """Scroll the orders listing and click any 'Load more' / 'Show more' button
    until the rendered row count stops growing. Blinkit paginates the listing,
    so without this we only see ~10 most-recent orders.

    If `since_dt` is given, stop early once the oldest visible row is already
    before the cutoff — no point loading history we'd skip anyway."""
    last_count = -1
    stable_passes = 0
    rows: list[dict] = []
    for pass_i in range(max_passes):
        rows = _enumerate_orders(page)
        count = len(rows)

        # Early stop: if we have at least one row older than the cutoff, all
        # further pagination is wasted work — rows load newest-first.
        if since_dt and ref_year is not None and rows:
            oldest = None
            for r in rows:
                rd = _parse_row_date(r.get("date") or "", ref_year)
                if rd and (oldest is None or rd < oldest):
                    oldest = rd
            if oldest and oldest < since_dt:
                console.print(
                    f"[dim]  load: oldest visible {oldest.isoformat()} < cutoff, stopping at {count} rows[/dim]"
                )
                return rows

        if count == last_count:
            stable_passes += 1
            if stable_passes >= 2:
                console.print(f"[dim]  load: stable at {count} rows after {pass_i+1} passes[/dim]")
                return rows
        else:
            stable_passes = 0
            console.print(f"[dim]  load: pass {pass_i+1} -> {count} rows[/dim]")
        last_count = count

        # Try a "Load more" / "Show more" / "View more" button first.
        clicked_more = False
        for sel in [
            'button:has-text("Load more")',
            'button:has-text("Show more")',
            'button:has-text("View more")',
            'a:has-text("Load more")',
            'a:has-text("Show more")',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=300):
                    loc.click(timeout=2000)
                    clicked_more = True
                    page.wait_for_timeout(800)
                    break
            except Exception:
                continue

        # Always also scroll to the bottom to trigger infinite-scroll loaders.
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        # Brief wait for new rows to render.
        page.wait_for_timeout(600 if clicked_more else 900)

    console.print(f"[yellow]  load: hit max_passes={max_passes} without stabilizing[/yellow]")
    return rows


def _enumerate_orders(page: Page) -> list[dict]:
    """Each order row contains 'Arrived in N minutes' as a small header.
    Find the LEAF element that has that exact phrase (no children with the
    phrase), then walk up to the smallest ancestor that ALSO contains an
    order id like '#778'. Cap the row width to avoid grabbing the whole page."""
    return page.evaluate(
        """
        () => {
          const PHRASE = /Arrived in\\s+\\d+\\s+minute/i;
          // Step 1: leaf elements containing the phrase. A 'leaf' here means
          // no descendant element also contains the phrase, so we pick the
          // tightest match (the actual header text node's parent).
          const all = Array.from(document.querySelectorAll('*'));
          const leaves = all.filter(el => {
            if (!PHRASE.test(el.textContent || '')) return false;
            for (const c of el.children) {
              if (PHRASE.test(c.textContent || '')) return false;
            }
            return true;
          });
          const rows = [];
          // Diagnostic counters.
          let dbg = {leaves: leaves.length, broke_no_row: 0, broke_no_rect: 0,
                     broke_multi_header: 0, broke_no_idmatch: 0, ok: 0};
          window.__edgelake_dbg = dbg;
          for (const leaf of leaves) {
            // ID regex: short order number sits on its own line right after
            // "Arrived in N minutes". Blinkit prefixes it with a glyph that
            // is NOT '#' (renders as '?' in cp1252 dump). Capture the entire
            // digit run (incl. commas like '1,599') so we don't truncate.
            const ID_RE = /Arrived in\\s+\\d+\\s+minute[s]?[\\s\\S]{0,12}?([\\d,]+)/i;
            // Walk up until the row contains both the header AND
            // matches ID_RE (id is on a sibling line). Stop before we
            // climb into a list that holds multiple headers.
            let row = leaf;
            for (let i = 0; i < 12 && row && row.parentElement; i++) {
              const t = (row.innerText || '').replace(/[ \\t]+/g, ' ');
              const headerMatches = (t.match(/Arrived in\\s+\\d+\\s+minute/gi) || []).length;
              if (headerMatches > 1) break;  // climbed too far
              if (headerMatches === 1 && ID_RE.test(t)) break;
              row = row.parentElement;
            }
            if (!row) { dbg.broke_no_row++; continue; }
            const r = row.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) { dbg.broke_no_rect++; continue; }
            const rowText = (row.innerText || '').replace(/[ \\t]+/g, ' ');
            const headerMatches = (rowText.match(/Arrived in\\s+\\d+\\s+minute/gi) || []).length;
            if (headerMatches !== 1) { dbg.broke_multi_header++; continue; }
            const idMatch = rowText.match(ID_RE);
            if (!idMatch) { dbg.broke_no_idmatch++; continue; }
            dbg.ok++;
            const full = rowText.replace(/\\s+/g, ' ').trim();
            // Amount: a number after the rupee glyph (which displays as '?'
            // in the cp1252 dump) — accept any character that isn't a digit
            // before a number that's separated from the date.
            const amtMatch = full.match(/(?:\\u20B9|Rs\\.?|\\?|\\u00A3)\\s*([0-9]+(?:,[0-9]{3})*(?:\\.[0-9]{1,2})?)/);
            const dateMatch = full.match(/(\\d{1,2}\\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[^,]*(?:,\\s*\\d{1,2}:\\d{2}\\s*(?:am|pm))?)/i);
            // r.x / r.y are VIEWPORT-relative. Store document-relative y so
            // we can scroll the row back into view later (page.goto resets
            // scroll, but the row's document position is stable).
            rows.push({
              id: idMatch[1],
              amount: amtMatch ? amtMatch[1] : '',
              date: dateMatch ? dateMatch[1] : '',
              x: Math.round(r.x + r.width / 2),
              y: Math.round(r.y + window.scrollY + r.height / 2),
              w: Math.round(r.width),
              h: Math.round(r.height),
              text_sample: full.slice(0, 160),
            });
          }
          // Dedup by id, keep first.
          const seen = new Set();
          return rows.filter(r => {
            if (seen.has(r.id)) return false;
            seen.add(r.id);
            return true;
          });
        }
        """
    )


def _ascii(s: str) -> str:
    """Strip non-ASCII for cp1252 Windows consoles that can't print '₹'."""
    return (s or "").encode("ascii", "replace").decode("ascii")


def _dump_invoice_candidates(page: Page) -> list[dict]:
    return page.evaluate(
        """
        () => {
          const out = [];
          for (const el of document.querySelectorAll('a, button, div, span')) {
            const t = (el.innerText || '').trim();
            const h = el.getAttribute('href') || '';
            if (!t && !h) continue;
            const hay = (t + ' ' + h).toLowerCase();
            if (hay.includes('invoice') || hay.includes('download') ||
                hay.includes('.pdf') || hay.includes('receipt') || hay.includes('bill')) {
              out.push({tag: el.tagName, text: t.slice(0, 100), href: h.slice(0, 200)});
            }
          }
          // Dedup by (tag,text,href).
          const seen = new Set();
          return out.filter(c => {
            const k = c.tag + '|' + c.text + '|' + c.href;
            if (seen.has(k)) return false;
            seen.add(k);
            return true;
          }).slice(0, 60);
        }
        """
    )


def _try_download_invoice(
    page: Page,
    dest_dir: Path,
    order_id: str,
    have: set[str] | None = None,
) -> Path | None:
    """Best-effort: look for an Invoice/Download Invoice control and capture
    the resulting download. Returns the saved path or None.

    If `have` is supplied, the suggested filename is checked against it
    BEFORE saving — so if the same invoice file already exists on disk we
    don't write a duplicate copy with a `_<order_id>` suffix."""
    selectors = [
        'a:has-text("Download Invoice")',
        'button:has-text("Download Invoice")',
        'a:has-text("Invoice")',
        'button:has-text("Invoice")',
        'a[href*="invoice" i]',
        'a[href$=".pdf"]',
    ]
    # Poll until ANY of the selectors appears (up to 4s total) — much faster
    # than serially asking each one with a 1.5s timeout.
    deadline = time.monotonic() + 4.0
    matched_sel: str | None = None
    while time.monotonic() < deadline and matched_sel is None:
        for sel in selectors:
            try:
                if page.locator(sel).first.is_visible(timeout=200):
                    matched_sel = sel
                    break
            except Exception:
                continue
        if matched_sel is None:
            page.wait_for_timeout(150)
    if matched_sel is None:
        console.print("[yellow]  no Download Invoice control found on detail page[/yellow]")
        return None
    try:
        with page.expect_download(timeout=15000) as dl_info:
            page.locator(matched_sel).first.click(timeout=3000)
        dl = dl_info.value
        suggested = dl.suggested_filename or f"blinkit_{order_id}.pdf"

        # If the suggested filename is already on disk OR in our `have` set,
        # skip without writing — same invoice, different short id on listing.
        if have is not None and suggested in have:
            try:
                dl.cancel()
            except Exception:
                pass
            console.print(f"[dim]  already have {suggested}, skipping save[/dim]")
            return None

        target = dest_dir / suggested
        if target.exists():
            # File on disk but not in `have` (somehow). Treat as dedup.
            try:
                dl.cancel()
            except Exception:
                pass
            console.print(f"[dim]  {suggested} already on disk, skipping save[/dim]")
            return None

        dl.save_as(str(target))
        console.print(f"[green]  downloaded:[/green] {target.name}")
        return target
    except Exception as e:
        console.print(f"[yellow]  invoice click via {matched_sel} failed: {e}[/yellow]")
        return None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_row_date(date_str: str, ref_year: int) -> datetime | None:
    """Parse 'DD MMM, HH:MM am/pm' into a datetime. Year is the reference
    year passed in (Blinkit doesn't include year in the listing)."""
    if not date_str:
        return None
    import re
    m = re.match(
        r"\s*(\d{1,2})\s+([A-Za-z]{3})(?:[A-Za-z]*),?\s*(\d{1,2}):(\d{2})\s*(am|pm)",
        date_str.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    day, mon, hh, mm, ampm = m.groups()
    mon_i = _MONTHS.get(mon.lower()[:3])
    if not mon_i:
        return None
    hour = int(hh) % 12
    if ampm.lower() == "pm":
        hour += 12
    try:
        return datetime(ref_year, mon_i, int(day), hour, int(mm))
    except ValueError:
        return None


def _parse_row_time(date_str: str) -> str:
    """Extract HHMM (24h) from a row date like '14 May, 2:30 pm'. Empty on miss."""
    if not date_str:
        return ""
    import re
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", date_str, re.IGNORECASE)
    if not m:
        return ""
    hh, mm, ampm = m.group(1), m.group(2), m.group(3).lower()
    hour = int(hh) % 12
    if ampm == "pm":
        hour += 12
    return f"{hour:02d}{mm}"


def _row_amount_float(row: dict) -> float | None:
    """Listing amount comes back as a string like '1,006' or '1,006.50'. Convert
    to float for comparison. Returns None if the row didn't yield an amount."""
    raw = (row.get("amount") or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _rename_blinkit_pdf(saved: Path, row: dict, ref_year: int, page: Page | None = None) -> Path:
    """Rename a freshly-downloaded Blinkit invoice to:
        Blinkit_<ORDERID>_<YYYY-MM-DD>_<HHMM>_<AMOUNT>.pdf
    Order ID comes from the existing filename (which Blinkit names ForwardInvoice_ORD<digits>.pdf).
    Date+time come from the row text; amount is parsed from the PDF and
    cross-checked against the listing row amount. On mismatch (>= Rs.1) we
    log a loud warning, snap a screenshot, and use the LISTING amount in the
    filename — the listing is what the user saw and treats as authoritative
    for this run. PDF file itself is kept either way.
    Falls back to leaving the file alone if anything required is missing."""
    # 1. Pull ORD<digits> out of the current filename — that's our canonical id.
    stem = saved.stem
    order_id = ""
    if "ORD" in stem:
        order_id = stem[stem.index("ORD"):].split("_")[0]
    if not order_id:
        return saved  # nothing to anchor on; leave as-is

    # 2. Date from the row.
    row_dt = _parse_row_date(row.get("date") or "", ref_year)
    if not row_dt:
        return saved
    date_part = row_dt.strftime("%Y-%m-%d")
    time_part = _parse_row_time(row.get("date") or "") or f"{row_dt.hour:02d}{row_dt.minute:02d}"

    # 3. Amount. Parse the PDF, then cross-check against the listing row.
    pdf_amount: float | None = None
    try:
        from ..parsers.pdf import parse_pdf
        r = parse_pdf(saved)
        pdf_amount = float(r.amount)
    except Exception as e:
        console.print(f"[yellow]  rename: could not parse amount from {saved.name}: {e}[/yellow]")
        # Fall through — try to use the listing amount if available.

    row_amount = _row_amount_float(row)
    chosen: float | None
    if pdf_amount is None and row_amount is None:
        console.print(f"[yellow]  rename: no amount from PDF or listing for {saved.name}, leaving as-is[/yellow]")
        return saved
    elif pdf_amount is None:
        console.print(f"[yellow]  rename: PDF parse failed, using listing amount Rs.{row_amount:.2f}[/yellow]")
        chosen = row_amount
    elif row_amount is None:
        # No listing amount to cross-check — trust the PDF silently.
        chosen = pdf_amount
    elif abs(pdf_amount - row_amount) >= 1.0:
        # Mismatch: warn LOUDLY and prefer the listing.
        console.print(
            f"[bold yellow]  AMOUNT MISMATCH for {order_id}: "
            f"listing=Rs.{row_amount:.2f}  PDF=Rs.{pdf_amount:.2f}  "
            f"-> using listing in filename[/bold yellow]"
        )
        if page is not None:
            try:
                _snap(page, f"amount_mismatch_{order_id}")
            except Exception:
                pass
        chosen = row_amount
    else:
        # Within tolerance — listing rounds, PDF has decimals. Use PDF (more precise).
        chosen = pdf_amount

    amount_part = str(int(round(chosen)))
    new_name = f"Blinkit_{order_id}_{date_part}_{time_part}_{amount_part}.pdf"
    target = saved.with_name(new_name)
    if target.exists() and target != saved:
        console.print(f"[dim]  rename target {new_name} already exists, leaving {saved.name}[/dim]")
        return saved
    try:
        saved.rename(target)
        console.print(f"[dim]  renamed -> {new_name}[/dim]")
        return target
    except Exception as e:
        console.print(f"[yellow]  rename failed: {e}[/yellow]")
        return saved


def _existing_long_ids() -> set[str]:
    """Return identifiers we've already seen (ledger + on-disk fallback).

    The ledger is the source of truth — it tracks every order_id and the
    filename Blinkit gave us. We still glob disk as a belt-and-suspenders
    check for files that exist but weren't ledgered (which `reconcile`
    should have caught upstream, but better safe than re-downloading)."""
    ids: set[str] = set()
    # 1. Ledger: all known order_ids, plus any filenames we've persisted.
    ids.update(known_order_ids())
    try:
        import sqlite3
        from ..config import LEDGER_PATH
        with sqlite3.connect(LEDGER_PATH) as conn:
            for (fn,) in conn.execute(
                "SELECT filename FROM receipts WHERE filename IS NOT NULL"
            ):
                ids.add(fn)
    except Exception:
        pass
    # 2. Disk fallback (reconcile should have ledgered these; this is paranoia).
    for d in (INBOX, PROCESSED):
        if not d.exists():
            continue
        for p in d.glob("*.pdf"):
            ids.add(p.name)
            stem = p.stem
            if "ORD" in stem:
                token = stem[stem.index("ORD"):].split("_")[0]
                ids.add(token)
    return ids


def _canonical_order_id(extracted: str, suggested_filename: str | None, sha: str | None) -> str:
    """Settle on a stable order_id given best-effort extraction results.

    Priority (most-stable first):
      1. ORD<digits> extracted from detail page innerText
      2. ORD<digits> parsed out of Blinkit's suggested download filename
      3. Synthetic 'manual_<sha[:12]>' when neither is available
    """
    if extracted and extracted.startswith("ORD") and extracted[3:].isdigit():
        return extracted
    if suggested_filename and "ORD" in suggested_filename:
        stem = Path(suggested_filename).stem
        token = stem[stem.index("ORD"):].split("_")[0].split(".")[0]
        if token.startswith("ORD") and token[3:].isdigit():
            return token
    if sha:
        return f"manual_{sha[:12]}"
    return extracted or f"unknown_{int(time.time())}"


def _on_detail_page(page: Page) -> bool:
    """Heuristic: we're on an order detail page if any of these markers appear.
    More robust than gating on ORD<digits> alone — some detail pages render
    the long order id late, inside a clipped element, or not at all in
    innerText (shadow DOM / lazy section)."""
    try:
        return bool(page.evaluate(
            """
            () => {
              const t = (document.body && document.body.innerText) || '';
              if (/order\\s+summary/i.test(t)) return true;
              if (/download\\s+invoice/i.test(t)) return true;
              if (/bill\\s+total/i.test(t)) return true;
              if (/items?\\s+in\\s+this\\s+order/i.test(t)) return true;
              return false;
            }
            """
        ))
    except Exception:
        return False


def _extract_order_id(page: Page) -> str:
    """Best-effort extraction of a stable order identifier. Tries, in order:
      1. ORD<digits> token anywhere in innerText
      2. Long digit run after 'Order id' / 'Order ID' label
      3. ID-shaped path segment in the URL
    Returns '' if nothing usable found — caller should still proceed since
    the invoice download itself provides a filename."""
    try:
        return page.evaluate(
            """
            () => {
              const t = (document.body && document.body.innerText) || '';
              // 1. ORD prefix anywhere
              let m = t.match(/ORD\\d{8,}/);
              if (m) return m[0];
              // 2. After 'Order id' / 'Order ID' label
              m = t.match(/order\\s*id[:\\s]+([A-Za-z0-9_-]{6,})/i);
              if (m) return m[1];
              // 3. URL slug (Blinkit sometimes uses /account/orders/<id>)
              const path = (location.pathname || '');
              const seg = path.split('/').filter(Boolean).pop() || '';
              if (/^[A-Za-z0-9_-]{6,}$/.test(seg) && seg.toLowerCase() !== 'orders') {
                return seg;
              }
              return '';
            }
            """
        ) or ""
    except Exception:
        return ""


def _process_one_order(page: Page, row: dict, dest_dir: Path, have: set[str]) -> str | None:
    """Click into a row, download invoice. Returns the order id (or filename)
    if a NEW invoice was downloaded; returns '' if order was already on disk;
    returns None on any failure."""
    # Scroll the row into view before clicking — page.goto() between rows
    # resets scroll to top, so the originally-captured y-coords are wrong
    # for rows that were below the fold at enumeration time.
    try:
        page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 200))", row["y"])
        page.wait_for_timeout(150)
    except Exception:
        pass

    # Click row by center coords, using the post-scroll viewport position.
    try:
        click_y = page.evaluate(
            "(y) => y - window.scrollY", row["y"]
        )
        page.mouse.click(row["x"], click_y)
    except Exception as e:
        console.print(f"[yellow]  could not click row @({row['x']},{row['y']}): {e}[/yellow]")
        return None

    # Wait for ANY detail-page signal (not just ORD<digits>). The long order
    # id sometimes never appears in innerText — but as long as we landed on
    # a detail page, the Download Invoice link is still actionable.
    def _wait_landed(timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if _on_detail_page(page):
                return True
            page.wait_for_timeout(200)
        return False

    landed = _wait_landed(5.0)
    if not landed:
        # Click may have missed — row coords could be stale after scroll
        # or a soft layout shift. Re-locate the row by its text and retry once.
        retry_target = row.get("text_sample") or ""
        retried = False
        if retry_target:
            try:
                # Find a fresh row matching this text sample and click its center.
                coords = page.evaluate(
                    """
                    (sample) => {
                      const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                      const want = norm(sample).slice(0, 80);
                      if (!want) return null;
                      for (const el of document.querySelectorAll('*')) {
                        const t = norm(el.innerText || '');
                        if (t.length > 60 && t.length < 400 && t.includes(want)) {
                          const r = el.getBoundingClientRect();
                          if (r.width > 100 && r.height > 30) {
                            return {x: Math.round(r.x + r.width/2),
                                    y: Math.round(r.y + r.height/2)};
                          }
                        }
                      }
                      return null;
                    }
                    """,
                    retry_target,
                )
                if coords and isinstance(coords, dict):
                    page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 200))", coords["y"])
                    page.wait_for_timeout(150)
                    click_y2 = page.evaluate("(y) => y - window.scrollY", coords["y"])
                    page.mouse.click(coords["x"], click_y2)
                    retried = True
                    landed = _wait_landed(5.0)
            except Exception as e:
                console.print(f"[dim]  retry-click failed: {e}[/dim]")
        if not landed:
            try:
                here = _safe_url(page)
                console.print(
                    f"[yellow]  click did not open a detail page "
                    f"(retried={retried}, URL: {here})[/yellow]"
                )
                _snap(page, f"miss_click_{row.get('id','?')}")
            except Exception:
                console.print("[yellow]  click did not open a detail page[/yellow]")
            return None

    # Best-effort id extraction. May be empty — that's OK, the download itself
    # carries a unique filename which usually contains the ORD token.
    extracted_id = _extract_order_id(page)
    if extracted_id:
        console.print(f"[dim]  order id: {extracted_id}[/dim]")
        if extracted_id in have or get_by_order_id(extracted_id):
            console.print(f"[dim]  already have {extracted_id}, skipping[/dim]")
            return ""

    # The default detail page is the order-tracking view; the Download Invoice
    # control lives inside the "View order summary" expansion. Click it before
    # the invoice search — discover() does the same.
    for sel in (
        'text="View order summary"',
        'a:has-text("View order summary")',
        'button:has-text("View order summary")',
    ):
        try:
            page.locator(sel).first.click(timeout=2000)
            page.wait_for_timeout(1200)
            break
        except Exception:
            continue

    saved = _try_download_invoice(page, dest_dir, extracted_id or "unknown", have)
    if not saved:
        return None

    # Settle on a canonical order_id now that we have the suggested filename.
    try:
        sha = hash_file(saved)
    except Exception:
        sha = None
    order_id = _canonical_order_id(extracted_id, saved.name, sha)

    # If the canonical id was already in the ledger (e.g. extracted_id was
    # empty but the filename revealed an ORD we've seen), we still wrote a
    # new copy of the PDF. Skip ledgering a duplicate — but don't delete the
    # file since that's the user's data. Reconcile will sort it out.
    if order_id != extracted_id and order_id in have:
        console.print(f"[dim]  recovered id {order_id} already in ledger, leaving file as-is[/dim]")
        return ""

    # Parse the listing-row amount + date + time, then write to ledger.
    listing_amount = _row_amount_float(row)
    ref_year = datetime.now().year
    row_dt = _parse_row_date(row.get("date") or "", ref_year)
    date_iso = row_dt.date().isoformat() if row_dt else None
    order_time = _parse_row_time(row.get("date") or "") or None
    upsert_fetched(
        order_id=order_id,
        sha=sha,
        filename=saved.name,
        merchant="Blinkit",
        listing_amount=listing_amount,
        date=date_iso,
        order_time=order_time,
    )
    if listing_amount is not None:
        console.print(_ascii(
            f"[dim]  ledgered {order_id}  listing=Rs.{listing_amount:.2f}  "
            f"file={saved.name}[/dim]"
        ))
    else:
        console.print(f"[dim]  ledgered {order_id}  listing=?  file={saved.name}[/dim]")
    return order_id


def _back_to_orders(page: Page) -> None:
    """Return to the orders listing. Use direct nav, but don't block on
    networkidle — the SPA rarely reaches it within timeout."""
    try:
        page.goto(BLINKIT_URL, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass
    # Poll for the orders list marker instead of waiting blindly.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if page.evaluate(
                "() => /\\bORD\\d{6,}\\b|Order ID/i.test((document.body&&document.body.innerText)||'')"
            ):
                return
        except Exception:
            pass
        page.wait_for_timeout(200)


def iterate_orders(page: Page, since_iso: str | None, skip_weekends: bool = False) -> int:
    """Walk every order on the listing, downloading invoices for the ones
    we don't already have and that fall after the since cutoff. Returns
    the number of new invoices downloaded."""
    _dismiss_address_modal(page)
    page.wait_for_timeout(600)

    # Parse since cutoff.
    since_dt: datetime | None = None
    if since_iso:
        try:
            since_dt = datetime.fromisoformat(since_iso.replace("Z", ""))
        except Exception:
            console.print(f"[yellow]  could not parse since={since_iso}; ignoring cutoff[/yellow]")

    ref_year = datetime.now().year

    # Paginate the listing first so older orders are surfaced. Stops early
    # once any visible row is older than the cutoff.
    orders = _load_all_orders(page, since_dt=since_dt, ref_year=ref_year)
    _snap(page, "00_orders_page")
    console.print(f"[cyan]Order rows found: {len(orders)}[/cyan]")
    if not orders:
        return 0

    have = _existing_long_ids()
    if have:
        console.print(f"[dim]  already on disk: {sorted(have)}[/dim]")
    downloaded = 0
    for i, row in enumerate(orders):
        # Skip by date.
        row_dt = _parse_row_date(row.get("date") or "", ref_year)
        if since_dt and row_dt and row_dt < since_dt:
            console.print(f"  [dim]skip #{row.get('id', '?')} ({row.get('date')}) — before cutoff[/dim]")
            continue
        # Skip weekends if requested. weekday(): Mon=0..Sun=6, so 5/6 are Sat/Sun.
        # If we couldn't parse the row date, keep the order (better to download
        # than silently drop a Friday-night purchase that we misread).
        if skip_weekends and row_dt and row_dt.weekday() >= 5:
            day_name = "Saturday" if row_dt.weekday() == 5 else "Sunday"
            console.print(f"  [dim]skip #{row.get('id', '?')} ({row.get('date')}) — {day_name}[/dim]")
            continue

        console.print(
            _ascii(f"\n[cyan]Order {i+1}/{len(orders)}:[/cyan] row text={row['text_sample']!r}")
        )
        result = _process_one_order(page, row, INBOX, have)
        if result:
            have.add(result)
            downloaded += 1

        _back_to_orders(page)
        # page.goto() re-collapses the paginated list back to the first page
        # (~10 rows). For rows beyond that, we must re-paginate to surface
        # them again before the next click can land. Skip the re-paginate
        # work for the trivial first-page case.
        fresh = _enumerate_orders(page)
        if len(fresh) < len(orders):
            fresh = _load_all_orders(page, since_dt=since_dt, ref_year=ref_year)
        if fresh:
            # Match rows by text_sample (most unique key across re-renders).
            # Short id is unreliable — amounts like '1,006' collide.
            by_sample = {r["text_sample"]: r for r in fresh}
            orders = [by_sample.get(r["text_sample"], r) for r in orders]

    return downloaded


def discover(page: Page) -> int:
    """Discovery + best-effort first download. Returns count downloaded."""
    _dismiss_address_modal(page)
    if _modal_still_visible(page):
        console.print("[yellow]  modal STILL visible after dismiss attempts[/yellow]")
    page.wait_for_timeout(800)
    _snap(page, "00_orders_page")

    _debug_dump_phrase(page)
    orders = _enumerate_orders(page)
    try:
        dbg = page.evaluate("() => window.__edgelake_dbg")
        console.print(_ascii(f"  enumeration dbg: {dbg}"))
    except Exception:
        pass
    console.print(f"[cyan]Order rows found: {len(orders)}[/cyan]")
    for o in orders[:15]:
        console.print(_ascii(
            f"  #{o['id'] or '?':<8}  Rs.{o['amount'] or '?':<10}  "
            f"{o['date'] or '-':<28}  @({o['x']},{o['y']})  w={o['w']}  "
            f"text={o['text_sample']!r}"
        ))
    if not orders:
        console.print("[yellow]  no order rows parsed — DOM shape changed; check 00_orders_page.png[/yellow]")
        return 0

    # Open the first order's detail page by clicking the row.
    first = orders[0]
    order_id = first["id"] or "unknown"
    console.print(f"\n[cyan]Opening order detail (#{order_id}) by clicking row...[/cyan]")
    opened = False
    if first["id"]:
        try:
            page.locator(f'text=#{first["id"]}').first.click(timeout=5000)
            opened = True
        except Exception as e:
            console.print(f"[yellow]  text-click failed: {e}[/yellow]")
    if not opened:
        try:
            page.mouse.click(first["x"], first["y"])
            opened = True
            console.print(f"[dim]  fallback: clicked row center @({first['x']},{first['y']})[/dim]")
        except Exception as e:
            console.print(f"[red]  could not click row: {e}[/red]")
            return 0

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    _snap(page, f"02_order_detail_{order_id}")
    console.print(f"[dim]  detail URL: {_safe_url(page)}[/dim]")

    # Pull the long order id out of the detail page so we don't depend on
    # the short, sometimes-missing display id.
    long_id = page.evaluate(
        """
        () => {
          const t = (document.body && document.body.innerText) || '';
          const m = t.match(/ORD\\d{8,}/);
          return m ? m[0] : '';
        }
        """
    )
    if long_id:
        console.print(f"[dim]  long order id: {long_id}[/dim]")
        order_id = long_id

    cands_before = _dump_invoice_candidates(page)
    console.print(f"[cyan]Invoice candidates on track page: {len(cands_before)}[/cyan]")
    for c in cands_before:
        console.print(_ascii(f"  {c['tag']} text={c['text']!r} href={c['href']!r}"))

    # The track page hides the invoice behind 'View order summary'. Click it.
    expanded = False
    for sel in [
        'text="View order summary"',
        'a:has-text("View order summary")',
        'button:has-text("View order summary")',
    ]:
        try:
            page.locator(sel).first.click(timeout=3000)
            console.print(f"[dim]  clicked View order summary via {sel}[/dim]")
            expanded = True
            break
        except Exception:
            continue
    if expanded:
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        _snap(page, f"03_order_summary_{order_id}")

        cands_after = _dump_invoice_candidates(page)
        console.print(
            f"[cyan]Invoice candidates on summary page: {len(cands_after)}[/cyan]"
        )
        for c in cands_after:
            console.print(_ascii(f"  {c['tag']} text={c['text']!r} href={c['href']!r}"))

    saved = _try_download_invoice(page, INBOX, order_id)
    return 1 if saved else 0


def fetch(since_iso: str | None, skip_weekends: bool = False) -> int:
    """Entry point. Returns count of PDFs downloaded."""
    INBOX.mkdir(parents=True, exist_ok=True)
    if since_iso:
        console.print(f"[cyan]Fetching Blinkit orders since:[/cyan] {since_iso}")
    else:
        console.print("[cyan]Fetching all available Blinkit orders (no since cutoff).[/cyan]")

    n = 0
    with blinkit_session() as page:
        n = iterate_orders(page, since_iso, skip_weekends=skip_weekends)
        page.wait_for_timeout(1500)
    return n
