from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright
from rich.console import Console

from ..config import CHROMERIVER_URL, PROFILE_DIR

console = Console()

LOGIN_HOST_FRAGMENTS = ("okta", "logon.bcg.com", "saml", "/sso")


def _is_dashboard_url(url: str) -> bool:
    """
    Dashboard = URL is on a Chrome River / Emburse host AND we are no longer
    on an SSO/IdP host. The Chrome River app shell post-SAML lands on
    app.eu1.chromeriver.com (any path).
    """
    u = url.lower()
    if not u.startswith("http"):
        return False
    if "chromeriver.com" not in u and "emburse" not in u:
        return False
    if any(f in u for f in LOGIN_HOST_FRAGMENTS):
        return False
    # /login/sso/saml is the entry redirect, but the post-auth Chrome River
    # URL is just app.eu1.chromeriver.com/<path>. Accept anything that lands
    # on the host without the login-flow markers above.
    return True


def _is_blank(url: str) -> bool:
    return (not url) or url in ("about:blank", "chrome://newtab/", "edge://newtab/")


def _safe_url(page: Page) -> str:
    """Force a fresh URL read by evaluating in the live page, since
    page.url can be stale after SAML form-POST navigations."""
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


def _kick_navigation(page: Page) -> None:
    """If a page is sitting on about:blank, push it to the SSO URL."""
    try:
        page.goto(CHROMERIVER_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        console.print(f"[dim]  (re-nav attempt: {e})[/dim]")


def _wait_for_dashboard(context: BrowserContext, timeout_s: int = 900) -> Page:
    console.print(
        "\n[bold yellow]>>> Complete login (Okta / SSO) in the Chromium window.[/bold yellow]"
    )
    console.print(
        f"[yellow]    Waiting up to {timeout_s}s. Do NOT close the window."
        "\n    Press Ctrl+C in this terminal to abort.[/yellow]\n"
    )

    deadline = time.monotonic() + timeout_s
    seen_urls: set[str] = set()
    kicked_blank = False

    while time.monotonic() < deadline:
        pages = [p for p in context.pages if not p.is_closed()]
        if not pages:
            raise RuntimeError(
                "All browser tabs were closed before login completed."
            )
        for p in pages:
            url = _safe_url(p)
            if _is_dashboard_url(url):
                console.print(f"[green]Login detected on:[/green] {url}")
                console.print("[dim]  waiting for app shell to finish loading...[/dim]")
                try:
                    p.bring_to_front()
                except Exception:
                    pass
                # Let the SPA finish rendering before we hand it to the uploader.
                try:
                    p.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass
                # Wait until at least one nav/action element is visible.
                hints = [
                    'text="Expenses"',
                    'text="Inquiry"',
                    'text="Create"',
                    'text="eWallet"',
                    'text="Dashboard"',
                    'nav',
                ]
                for hint in hints:
                    try:
                        p.wait_for_selector(hint, timeout=10000, state="visible")
                        break
                    except Exception:
                        continue
                # Final cushion for any late-rendering widgets.
                time.sleep(2)
                console.print("[green]Dashboard ready.[/green]\n")
                return p
            if url and url not in seen_urls:
                console.print(f"[dim]  current: {url}[/dim]")
                seen_urls.add(url)

        # If every surviving page is on about:blank, kick the first one once.
        if not kicked_blank and all(_is_blank(_safe_url(p)) for p in pages):
            console.print("[yellow]  page is blank — re-navigating to SSO URL...[/yellow]")
            _kick_navigation(pages[0])
            kicked_blank = True

        time.sleep(1.5)

    raise TimeoutError(f"Did not detect Chrome River dashboard within {timeout_s}s.")


@contextmanager
def chromeriver_session(headless: bool = False) -> Iterator[Page]:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 900},
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            console.print(f"[cyan]Navigating to:[/cyan] {CHROMERIVER_URL}")
            try:
                page.goto(CHROMERIVER_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                console.print(f"[yellow]Initial navigation note: {e}[/yellow]")
            dashboard_page = _wait_for_dashboard(context)
            yield dashboard_page
        finally:
            context.close()
