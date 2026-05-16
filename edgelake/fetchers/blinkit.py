from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright
from rich.console import Console

from ..config import BLINKIT_PROFILE_DIR, BLINKIT_URL, DEBUG_DIR, INBOX, PROCESSED

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
            rows.push({
              id: idMatch[1],
              amount: amtMatch ? amtMatch[1] : '',
              date: dateMatch ? dateMatch[1] : '',
              x: Math.round(r.x + r.width / 2),
              y: Math.round(r.y + r.height / 2),
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


def _try_download_invoice(page: Page, dest_dir: Path, order_id: str) -> Path | None:
    """Best-effort: look for an Invoice/Download Invoice control and capture
    the resulting download. Returns the saved path or None."""
    selectors = [
        'a:has-text("Download Invoice")',
        'button:has-text("Download Invoice")',
        'a:has-text("Invoice")',
        'button:has-text("Invoice")',
        'a[href*="invoice" i]',
        'a[href$=".pdf"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.is_visible(timeout=1500):
                continue
        except Exception:
            continue
        try:
            with page.expect_download(timeout=15000) as dl_info:
                loc.click(timeout=4000)
            dl = dl_info.value
            suggested = dl.suggested_filename or f"blinkit_{order_id}.pdf"
            target = dest_dir / suggested
            if target.exists():
                target = dest_dir / f"{Path(suggested).stem}_{order_id}{Path(suggested).suffix}"
            dl.save_as(str(target))
            console.print(f"[green]  downloaded:[/green] {target.name}")
            return target
        except Exception as e:
            console.print(f"[yellow]  invoice click via {sel} failed: {e}[/yellow]")
            continue
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


def _existing_long_ids() -> set[str]:
    """Return the set of long order ids already on disk (inbox + processed)
    so we can skip re-downloading."""
    ids: set[str] = set()
    for d in (INBOX, PROCESSED):
        if not d.exists():
            continue
        for p in d.glob("*.pdf"):
            # Filenames look like ForwardInvoice_ORD<digits>.pdf
            stem = p.stem
            if "ORD" in stem:
                token = stem[stem.index("ORD"):].split("_")[0]
                ids.add(token)
    return ids


def _process_one_order(page: Page, row: dict, dest_dir: Path, have: set[str]) -> str | None:
    """Click into a row, expand summary, download invoice. Returns the long
    order id if a NEW invoice was downloaded; returns '' if order was already
    on disk; returns None on any failure."""
    # Click row by center coords (the row isn't an anchor).
    try:
        page.mouse.click(row["x"], row["y"])
    except Exception as e:
        console.print(f"[yellow]  could not click row @({row['x']},{row['y']}): {e}[/yellow]")
        return None

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1200)

    long_id = page.evaluate(
        "() => { const t=(document.body&&document.body.innerText)||''; const m=t.match(/ORD\\d{8,}/); return m?m[0]:''; }"
    )
    if not long_id:
        console.print("[yellow]  could not read long order id from detail page[/yellow]")
        return None
    console.print(f"[dim]  long order id: {long_id}[/dim]")

    # Dedup check now that we know the long id.
    if long_id in have:
        console.print(f"[dim]  already have {long_id}, skipping[/dim]")
        return ""

    # Expand the summary view.
    expanded = False
    for sel in [
        'text="View order summary"',
        'a:has-text("View order summary")',
        'button:has-text("View order summary")',
    ]:
        try:
            page.locator(sel).first.click(timeout=3000)
            expanded = True
            break
        except Exception:
            continue
    if not expanded:
        console.print("[yellow]  no 'View order summary' link found[/yellow]")
    else:
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

    saved = _try_download_invoice(page, dest_dir, long_id)
    return long_id if saved else None


def _back_to_orders(page: Page) -> None:
    """Return to the orders listing. Use direct nav for reliability."""
    try:
        page.goto(BLINKIT_URL, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def iterate_orders(page: Page, since_iso: str | None) -> int:
    """Walk every order on the listing, downloading invoices for the ones
    we don't already have and that fall after the since cutoff. Returns
    the number of new invoices downloaded."""
    _dismiss_address_modal(page)
    page.wait_for_timeout(600)
    _snap(page, "00_orders_page")

    orders = _enumerate_orders(page)
    console.print(f"[cyan]Order rows found: {len(orders)}[/cyan]")
    if not orders:
        return 0

    # Parse since cutoff.
    since_dt: datetime | None = None
    if since_iso:
        try:
            since_dt = datetime.fromisoformat(since_iso.replace("Z", ""))
        except Exception:
            console.print(f"[yellow]  could not parse since={since_iso}; ignoring cutoff[/yellow]")

    have = _existing_long_ids()
    if have:
        console.print(f"[dim]  already on disk: {sorted(have)}[/dim]")

    ref_year = datetime.now().year
    downloaded = 0
    for i, row in enumerate(orders):
        # Skip by date.
        row_dt = _parse_row_date(row.get("date") or "", ref_year)
        if since_dt and row_dt and row_dt < since_dt:
            console.print(f"  [dim]skip #{row.get('id', '?')} ({row.get('date')}) — before cutoff[/dim]")
            continue

        console.print(
            _ascii(f"\n[cyan]Order {i+1}/{len(orders)}:[/cyan] row text={row['text_sample']!r}")
        )
        result = _process_one_order(page, row, INBOX, have)
        if result:
            have.add(result)
            downloaded += 1

        _back_to_orders(page)
        # Re-enumerate is overkill; listings are stable within a session, so
        # we trust the original row list. Coordinates may have shifted if
        # layout changed, so re-fetch them.
        fresh = _enumerate_orders(page)
        if len(fresh) == len(orders):
            orders = fresh  # refresh coords in place

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


def fetch(since_iso: str | None) -> int:
    """Entry point. Returns count of PDFs downloaded.

    Phase 0 right now: opens browser, waits for login, dumps page structure
    so we can wire up the actual download loop next."""
    INBOX.mkdir(parents=True, exist_ok=True)
    if since_iso:
        console.print(f"[cyan]Fetching Blinkit orders since:[/cyan] {since_iso}")
    else:
        console.print("[cyan]Fetching all available Blinkit orders (no since cutoff).[/cyan]")

    n = 0
    with blinkit_session() as page:
        n = iterate_orders(page, since_iso)
        page.wait_for_timeout(1500)
    return n
