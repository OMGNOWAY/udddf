"""
Google sign-in -> YouTube cookies.txt.

Uses a normal installed Google Chrome instance under Xvfb when available.
Google CAPTCHA, passkey, phone verification, or other interactive security
checks are NOT bypassed. If Google requires one, the login fails with a
useful diagnostic so the verification can be completed manually.

Environment variables:
  GOOGLE_EMAIL                    required
  GOOGLE_PASSWORD                 required
  GOOGLE_TOTP_SECRET              optional
                                  base32 authenticator-app secret
  GOOGLE_LOGIN_COOLDOWN_SECONDS   minimum gap between attempts
                                  default: 900
  GOOGLE_LOGIN_DEBUG_DIR          optional screenshot directory
  GOOGLE_LOGIN_TIMEZONE           optional
                                  default: America/New_York
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse


class LoginError(Exception):
    """Expected Google login failure."""


LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin"
    "?service=youtube"
    "&continue=https%3A%2F%2Fwww.youtube.com%2F"
)

YT_HOSTS = {
    "www.youtube.com",
    "youtube.com",
    "m.youtube.com",
}

SESSION_COOKIES = {
    "SAPISID",
    "__Secure-3PAPISID",
    "__Secure-3PSID",
    "SID",
}

CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--window-size=1280,900",
]

EMAIL_SELECTORS = (
    'input[type="email"]',
    "input#identifierId",
    'input[name="identifier"]',
    'input[autocomplete="username"]',
)

PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="Passwd"]',
)


try:
    DEFAULT_COOLDOWN = max(
        0,
        int(os.getenv("GOOGLE_LOGIN_COOLDOWN_SECONDS", "900")),
    )
except ValueError:
    DEFAULT_COOLDOWN = 900


_lock = threading.Lock()
_last_attempt = 0.0
_last_error: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Return True when the required credentials are configured."""
    return bool(
        os.getenv("GOOGLE_EMAIL", "").strip()
        and os.getenv("GOOGLE_PASSWORD", "")
    )


def status() -> dict:
    """Return basic login status."""
    return {
        "configured": is_configured(),
        "lastError": _last_error,
    }


def fetch_cookies(cooldown: int | None = None) -> str:
    """
    Sign into Google and return Netscape-format cookies.txt text.

    Raises:
        LoginError: if credentials are missing, Google requires an
                    unsupported verification step, or the browser fails.
    """
    global _last_attempt, _last_error

    email = os.getenv("GOOGLE_EMAIL", "").strip()
    password = os.getenv("GOOGLE_PASSWORD", "")

    if not email or not password:
        raise LoginError(
            "GOOGLE_EMAIL and GOOGLE_PASSWORD are not configured."
        )

    if cooldown is None:
        cooldown = DEFAULT_COOLDOWN

    if not _lock.acquire(blocking=False):
        raise LoginError(
            "A Google login is already running."
        )

    try:
        elapsed = time.time() - _last_attempt
        wait = cooldown - elapsed

        if wait > 0:
            raise LoginError(
                "A Google login was attempted recently. "
                f"Wait {int(wait)} seconds before trying again."
            )

        _last_attempt = time.time()

        try:
            cookies = _browser_login(
                email=email,
                password=password,
                totp_secret=os.getenv(
                    "GOOGLE_TOTP_SECRET",
                    "",
                ).strip(),
            )

        except LoginError as exc:
            _last_error = str(exc)
            raise

        except Exception as exc:
            _last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            raise LoginError(
                f"Browser login failed: {_last_error}"
            ) from exc

        _last_error = None
        return cookies

    finally:
        _lock.release()


# ---------------------------------------------------------------------------
# Xvfb
# ---------------------------------------------------------------------------

def _start_xvfb():
    """
    Start Xvfb when DISPLAY isn't already configured.

    Returns:
        (process, display)
        or
        (None, None)
    """
    if os.environ.get("DISPLAY"):
        return None, None

    xvfb = shutil.which("Xvfb")

    if not xvfb:
        return None, None

    for display in (":99", ":98", ":97"):
        try:
            process = subprocess.Popen(
                [
                    xvfb,
                    display,
                    "-screen",
                    "0",
                    "1280x900x24",
                    "-nolisten",
                    "tcp",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        except OSError:
            continue

        time.sleep(1)

        if process.poll() is None:
            return process, display

    return None, None


def _stop_xvfb(process) -> None:
    """Stop Xvfb safely."""
    if process is None:
        return

    process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def _launch(playwright, display):
    """
    Launch ordinary Chrome.

    No webdriver spoofing, user-agent spoofing, or automation hiding is used.
    """
    if display:
        try:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=False,
                args=CHROME_ARGS,
                env={
                    **os.environ,
                    "DISPLAY": display,
                },
            )

            return browser

        except Exception as exc:
            first_line = str(exc).strip().splitlines()

            reason = (
                first_line[0]
                if first_line
                else type(exc).__name__
            )

            print(
                "[google_login] Installed Chrome could not be "
                f"started: {reason}",
                flush=True,
            )

    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=CHROME_ARGS + [
                "--disable-gpu",
            ],
        )

        return browser

    except Exception as exc:
        raise LoginError(
            "No usable browser was found. Install Google Chrome "
            "or run `playwright install chromium`."
        ) from exc


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _browser_login(
    email: str,
    password: str,
    totp_secret: str,
) -> str:
    try:
        from playwright.sync_api import (
            TimeoutError as PlaywrightTimeout,
            sync_playwright,
        )

    except ImportError as exc:
        raise LoginError(
            "Playwright is not installed. "
            "Run `pip install playwright` and install a browser."
        ) from exc

    xvfb_process = None
    browser = None
    context = None
    page = None
    cookies = None

    try:
        xvfb_process, display = _start_xvfb()

        with sync_playwright() as playwright:
            browser = _launch(
                playwright,
                display,
            )

            context = browser.new_context(
                locale="en-US",
                timezone_id=os.getenv(
                    "GOOGLE_LOGIN_TIMEZONE",
                    "America/New_York",
                ),
                viewport={
                    "width": 1280,
                    "height": 900,
                },
            )

            page = context.new_page()

            page.set_default_timeout(
                20_000
            )

            try:
                _do_login(
                    page=page,
                    email=email,
                    password=password,
                    totp_secret=totp_secret,
                    PlaywrightTimeout=PlaywrightTimeout,
                )

                # Visit YouTube after authentication so the browser
                # has the relevant Google/YouTube cookies.
                page.goto(
                    "https://www.youtube.com/robots.txt",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                cookies = context.cookies()

            except Exception:
                _debug_screenshot(page)
                raise

            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass

                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    finally:
        _stop_xvfb(xvfb_process)

    if not cookies:
        raise LoginError(
            "The browser returned no cookies."
        )

    if not any(
        cookie.get("name") in SESSION_COOKIES
        for cookie in cookies
    ):
        raise LoginError(
            "YouTube was reached, but no Google session cookie "
            "was found. The account may not be signed in."
        )

    return _to_netscape(cookies)


# ---------------------------------------------------------------------------
# Google login flow
# ---------------------------------------------------------------------------

def _wait_for_input(
    page,
    selectors,
    label_regex: str,
    timeout_ms: int,
    PlaywrightTimeout,
):
    """
    Find a visible input using CSS selectors or its accessible label.
    """
    locator = page.locator(
        ", ".join(selectors)
    ).first

    try:
        locator.wait_for(
            state="visible",
            timeout=timeout_ms,
        )

        return locator

    except PlaywrightTimeout:
        pass

    try:
        locator = page.get_by_label(
            re.compile(
                label_regex,
                re.IGNORECASE,
            )
        ).first

        locator.wait_for(
            state="visible",
            timeout=5_000,
        )

        return locator

    except PlaywrightTimeout:
        return None


def _do_login(
    page,
    email: str,
    password: str,
    totp_secret: str,
    PlaywrightTimeout,
):
    # ---------------------------------------------------------------
    # Open Google login
    # ---------------------------------------------------------------

    page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=30_000,
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=15_000,
        )
    except PlaywrightTimeout:
        pass

    # ---------------------------------------------------------------
    # Email
    # ---------------------------------------------------------------

    email_box = _wait_for_input(
        page,
        EMAIL_SELECTORS,
        r"email or phone|email",
        45_000,
        PlaywrightTimeout,
    )

    if email_box is None:
        raise LoginError(
            "Google's sign-in page did not show an email field. "
            + _where(page)
        )

    email_box.fill(email)
    email_box.press("Enter")

    # ---------------------------------------------------------------
    # Password
    # ---------------------------------------------------------------

    password_box = _wait_for_input(
        page,
        PASSWORD_SELECTORS,
        r"password|enter your password",
        30_000,
        PlaywrightTimeout,
    )

    if password_box is None:
        _raise_interactive_verification_error(page)

    password_box.fill(password)
    password_box.press("Enter")

    # ---------------------------------------------------------------
    # Post-password authentication
    # ---------------------------------------------------------------

    deadline = time.monotonic() + 90
    totp_used = False

    while time.monotonic() < deadline:
        url = page.url.lower()
        host = (
            urlparse(page.url).hostname
            or ""
        ).lower()

        # Successful authentication.
        if host in YT_HOSTS:
            try:
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=10_000,
                )
            except PlaywrightTimeout:
                pass

            return

        # -----------------------------------------------------------
        # Wrong password
        # -----------------------------------------------------------

        if _visible_text(
            page,
            r"wrong password|incorrect password",
        ):
            raise LoginError(
                "Google reported that the password is incorrect."
            )

        # -----------------------------------------------------------
        # CAPTCHA / human verification
        # -----------------------------------------------------------

        if _visible_text(
            page,
            (
                r"captcha"
                r"|verify you're human"
                r"|verify you are human"
                r"|confirm you're not a robot"
            ),
        ):
            raise LoginError(
                "Google requires CAPTCHA or human verification. "
                "Complete it manually in Chrome and retry."
            )

        # -----------------------------------------------------------
        # TOTP
        # -----------------------------------------------------------

        totp_input = page.locator(
            'input[name="totpPin"]'
        ).first

        if _is_visible(totp_input):
            if not totp_secret:
                raise LoginError(
                    "Google requested an authenticator code. "
                    "Set GOOGLE_TOTP_SECRET or complete the "
                    "verification manually."
                )

            if totp_used:
                raise LoginError(
                    "The authenticator code was rejected."
                )

            try:
                import pyotp

            except ImportError as exc:
                raise LoginError(
                    "GOOGLE_TOTP_SECRET is configured but pyotp "
                    "is not installed. Run `pip install pyotp`."
                ) from exc

            code = pyotp.TOTP(
                totp_secret.replace(" ", "")
            ).now()

            totp_input.fill(code)
            totp_input.press("Enter")

            totp_used = True

            page.wait_for_timeout(
                2_000
            )

            continue

        # -----------------------------------------------------------
        # Additional Google challenge
        # -----------------------------------------------------------

        if "/challenge/" in url:
            raise LoginError(
                "Google requires an additional verification step "
                "that this script does not automate. Complete the "
                "verification manually and retry."
                + _where(page)
            )

        # -----------------------------------------------------------
        # Account/security prompts
        # -----------------------------------------------------------

        if _visible_text(
            page,
            r"verify your identity|confirm it's you|confirm it.s you",
        ):
            raise LoginError(
                "Google requires identity verification. "
                "Complete it manually and retry."
                + _where(page)
            )

        # -----------------------------------------------------------
        # Possible post-login prompts
        # -----------------------------------------------------------

        if _click_first(
            page,
            [
                'button:has-text("Not now")',
                'button:has-text("Skip")',
            ],
        ):
            page.wait_for_timeout(
                1_500
            )
            continue

        page.wait_for_timeout(
            500
        )

    raise LoginError(
        "Timed out waiting for Google to finish signing in. "
        + _where(page)
    )


def _raise_interactive_verification_error(page):
    """
    Convert a missing password field into a useful diagnostic.
    """
    if _visible_text(
        page,
        r"captcha|verify you're human|verify you are human",
    ):
        raise LoginError(
            "Google requires CAPTCHA/human verification. "
            "Complete it manually and retry."
            + _where(page)
        )

    raise LoginError(
        "Google did not show the password field. "
        "It may be requesting an additional verification step."
        + _where(page)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_visible(locator) -> bool:
    try:
        return locator.is_visible()
    except Exception:
        return False


def _visible_text(
    page,
    pattern: str,
) -> bool:
    try:
        body = page.locator("body")

        if not body.is_visible():
            return False

        text = body.inner_text(
            timeout=2_000
        )

        return bool(
            re.search(
                pattern,
                text,
                re.IGNORECASE,
            )
        )

    except Exception:
        return False


def _click_first(
    page,
    selectors,
) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(
                selector
            ).first

            if not locator.is_visible():
                continue

            locator.click(
                timeout=3_000
            )

            return True

        except Exception:
            continue

    return False


def _where(page) -> str:
    """
    Return compact diagnostic information.
    """
    try:
        path = urlparse(
            page.url
        ).path

        body_text = page.locator(
            "body"
        ).inner_text(
            timeout=2_000
        )

        body_text = re.sub(
            r"\s+",
            " ",
            body_text,
        ).strip()[:300]

    except Exception:
        return ""

    try:
        inputs = page.eval_on_selector_all(
            "input",
            """
            els => els.map(e => ({
                type: e.type,
                name: e.name,
                id: e.id,
                aria: e.getAttribute("aria-label"),
                visible: !!e.offsetParent
            }))
            """,
        )

    except Exception:
        inputs = []

    return (
        f" (path={path!r}; "
        f"text={body_text!r}; "
        f"inputs={inputs!r})"
    )


def _debug_screenshot(page) -> None:
    """
    Save a diagnostic screenshot when configured.
    """
    folder = os.getenv(
        "GOOGLE_LOGIN_DEBUG_DIR",
        "",
    ).strip()

    if not folder:
        return

    try:
        os.makedirs(
            folder,
            exist_ok=True,
        )

        filename = (
            f"google_login_fail_{int(time.time())}.png"
        )

        page.screenshot(
            path=os.path.join(
                folder,
                filename,
            ),
            full_page=True,
        )

    except Exception:
        pass


# ---------------------------------------------------------------------------
# Netscape cookie export
# ---------------------------------------------------------------------------

def _to_netscape(
    cookies: list[dict],
) -> str:
    """
    Convert Playwright cookies to Netscape cookies.txt format.
    """
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated from an authenticated browser session",
    ]

    for cookie in cookies:
        domain = str(
            cookie.get(
                "domain",
                "",
            )
        )

        bare_domain = (
            domain
            .lstrip(".")
            .lower()
        )

        if not (
            bare_domain.endswith(
                "google.com"
            )
            or bare_domain.endswith(
                "youtube.com"
            )
        ):
            continue

        name = cookie.get(
            "name"
        )

        value = cookie.get(
            "value"
        )

        if not name or value is None:
            continue

        try:
            expires = int(
                cookie.get(
                    "expires",
                    -1,
                )
                or 0
            )

        except (TypeError, ValueError):
            expires = 0

        # Netscape cookie files conventionally use 0
        # for session cookies.
        if expires < 0:
            expires = 0

        http_only_prefix = (
            "#HttpOnly_"
            if cookie.get("httpOnly")
            else ""
        )

        include_subdomains = (
            "TRUE"
            if domain.startswith(".")
            else "FALSE"
        )

        secure = (
            "TRUE"
            if cookie.get("secure")
            else "FALSE"
        )

        path = cookie.get(
            "path",
            "/",
        )

        lines.append(
            "\t".join(
                [
                    http_only_prefix + domain,
                    include_subdomains,
                    path,
                    secure,
                    str(expires),
                    str(name),
                    str(value),
                ]
            )
        )

    return "\n".join(lines) + "\n"
