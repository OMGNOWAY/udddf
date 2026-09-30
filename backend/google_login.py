"""Headless Google sign-in -> YouTube cookies.

Logs into a spare Google account with a headless Chromium (Playwright) and
returns a Netscape-format cookies.txt string that downloader.save_cookies()
accepts. Use a throwaway account, never your main one.

Env vars:
  GOOGLE_EMAIL, GOOGLE_PASSWORD   required
  GOOGLE_TOTP_SECRET              optional: base32 secret if the account uses an
                                  authenticator app for 2-step verification (needs pyotp)
  GOOGLE_LOGIN_COOLDOWN_SECONDS   min gap between automatic attempts (default 900)
  GOOGLE_LOGIN_DEBUG_DIR          if set, a screenshot is saved there when a login fails
"""
import os
import re
import threading
import time
from urllib.parse import urlparse


class LoginError(Exception):
    """Login failed for a reason we can explain (shown to the admin / logged)."""


LOGIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"
_YT_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")
_SESSION_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-3PSID", "SID")

try:
    DEFAULT_COOLDOWN = int(os.getenv("GOOGLE_LOGIN_COOLDOWN_SECONDS", "900"))
except ValueError:
    DEFAULT_COOLDOWN = 900

_lock = threading.Lock()
_last_attempt = 0.0
_last_error = None


def is_configured() -> bool:
    return bool(os.getenv("GOOGLE_EMAIL", "").strip() and os.getenv("GOOGLE_PASSWORD", ""))


def status() -> dict:
    return {"configured": is_configured(), "lastError": _last_error}


def fetch_cookies(cooldown: int | None = None) -> str:
    """Sign in and return cookies.txt text. Raises LoginError on any failure.

    `cooldown` is the minimum number of seconds since the previous attempt.
    Hammering Google with failed logins is a fast way to get the account locked.
    """
    global _last_attempt, _last_error

    email = os.getenv("GOOGLE_EMAIL", "").strip()
    password = os.getenv("GOOGLE_PASSWORD", "")
    if not email or not password:
        raise LoginError("GOOGLE_EMAIL and GOOGLE_PASSWORD are not set on the server.")
    if cooldown is None:
        cooldown = DEFAULT_COOLDOWN

    if not _lock.acquire(blocking=False):
        raise LoginError("A Google login is already running.")
    try:
        wait = cooldown - (time.time() - _last_attempt)
        if wait > 0:
            raise LoginError(
                f"Logged in (or tried to) recently. Waiting {int(wait)}s before the next attempt so Google doesn't lock the account."
            )
        _last_attempt = time.time()
        try:
            text = _browser_login(email, password, os.getenv("GOOGLE_TOTP_SECRET", "").strip())
        except LoginError as e:
            _last_error = str(e)
            raise
        except Exception as e:  # playwright crash, browser missing, OOM, ...
            _last_error = f"{type(e).__name__}: {e}"
            raise LoginError(f"Browser login crashed: {_last_error}") from e
        _last_error = None
        return text
    finally:
        _lock.release()


# ─── Browser flow ─────────────────────────────────────────────────────

def _browser_login(email: str, password: str, totp_secret: str) -> str:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise LoginError("playwright is not installed (pip install playwright && playwright install chromium).")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            # channel="chromium" = Chromium's "new" headless mode, which looks like a normal
            # browser (the default headless shell is trivially detectable).
            channel="chromium",
            headless=True,
            # Playwright adds --enable-automation by default, which sets navigator.webdriver
            # and is one of the things Google's "browser may not be secure" check looks for.
            ignore_default_args=["--enable-automation"],
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            major = browser.version.split(".")[0]
            context = browser.new_context(
                # Headless Chromium advertises "HeadlessChrome" in its UA, which Google flags.
                user_agent=f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id=os.getenv("GOOGLE_LOGIN_TIMEZONE", "America/New_York"),
                viewport={"width": 1280, "height": 900},
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            page.set_default_timeout(20000)
            try:
                _do_login(page, email, password, totp_secret, PWTimeout)
                # yt-dlp's wiki suggests grabbing cookies from robots.txt and then closing the
                # session, so YouTube doesn't rotate them while a browser tab keeps using them.
                page.goto("https://www.youtube.com/robots.txt", wait_until="domcontentloaded")
                cookies = context.cookies()
            except Exception:
                _debug_screenshot(page)
                raise
        finally:
            browser.close()

    if not any(c["name"] in _SESSION_COOKIES for c in cookies):
        raise LoginError("Reached YouTube but the session isn't signed in (no Google session cookies).")
    return _to_netscape(cookies)


_EMAIL_SEL = 'input[type="email"], input#identifierId, input[name="identifier"], input[autocomplete="username"]'
_PASS_SEL = 'input[type="password"], input[name="Passwd"]'


def _wait_for_input(page, selectors, label, timeout_ms, PWTimeout):
    """Return a visible input matching the selectors, or (fallback) the one with this label."""
    first = page.locator(selectors).first
    try:
        first.wait_for(state="visible", timeout=timeout_ms)
        return first
    except PWTimeout:
        pass
    by_label = page.get_by_label(re.compile(label, re.I)).first
    try:
        by_label.wait_for(state="visible", timeout=5000)
        return by_label
    except PWTimeout:
        return None


def _do_login(page, email, password, totp_secret, PWTimeout):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    try:
        # Google's page is JS-rendered and slow on a small server; let it settle.
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    # 1) email
    box = _wait_for_input(page, _EMAIL_SEL, r"email or phone", 45000, PWTimeout)
    if box is None:
        raise LoginError("Google's sign-in page didn't show an email box. " + _where(page))
    box.fill(email)
    box.press("Enter")

    # 2) password
    box = _wait_for_input(page, _PASS_SEL, r"enter your password|password", 30000, PWTimeout)
    if box is None and "/challenge/recaptcha" in page.url:
        # "Verify it's you" bot check. Often it is just a Confirm button (invisible reCAPTCHA).
        if _click_first(page, ['button:has-text("Confirm")', 'button:has-text("Verify")', 'button:has-text("Next")']):
            box = _wait_for_input(page, _PASS_SEL, r"enter your password|password", 30000, PWTimeout)
        if box is None:
            raise LoginError(
                "Google is showing its \"Verify it's you\" reCAPTCHA and it wasn't passed. A human has to solve that, so it can't be automated from this server. "
                + _where(page)
            )
    if box is None:
        if "rejected" in page.url:
            raise LoginError(
                "Google rejected the sign-in (\"this browser or app may not be secure\") right after the email step. " + _where(page)
            )
        raise LoginError("Google didn't ask for the password. " + _where(page))
    box.fill(password)
    box.press("Enter")

    # 3) whatever comes next: YouTube (success), a 2FA code, or a challenge/rejection
    deadline = time.time() + 45
    totp_sent = False
    while time.time() < deadline:
        url = page.url
        host = urlparse(url).hostname or ""

        if host in _YT_HOSTS:
            page.wait_for_load_state("load")
            return

        if "/challenge/totp" in url or _visible(page, 'input[name="totpPin"]'):
            if totp_sent:
                raise LoginError("Google rejected the 2-step verification code (is GOOGLE_TOTP_SECRET right?).")
            if not totp_secret:
                raise LoginError(
                    "Google wants a 2-step verification code. Set GOOGLE_TOTP_SECRET (authenticator app secret) or turn 2-step off on the spare account."
                )
            try:
                import pyotp
            except ImportError:
                raise LoginError("pyotp is not installed (pip install pyotp) but GOOGLE_TOTP_SECRET is set.")
            page.fill('input[name="totpPin"]', pyotp.TOTP(totp_secret.replace(" ", "")).now())
            page.keyboard.press("Enter")
            totp_sent = True
            page.wait_for_timeout(2500)
            continue

        if "rejected" in url or "deniedsignin" in url:
            raise LoginError("Google rejected the sign-in (it blocks a lot of automated / datacenter logins). " + _where(page))

        if "/challenge/pwd" in url:
            # Still the password page (Google's URL for it contains "challenge"). Either it is
            # about to move on, or it is showing an error.
            if _visible(page, 'text=/wrong password/i'):
                raise LoginError("Google says the password is wrong.")
            if _visible(page, 'input[name="ca"]'):
                raise LoginError("Google is showing a captcha, which can't be automated. " + _where(page))
            page.wait_for_timeout(500)
            continue

        if "/challenge/" in url:
            raise LoginError("Google wants extra verification that can't be automated. " + _where(page))

        if "speedbump" in url or _visible(page, 'button:has-text("Not now")'):
            if _click_first(page, ['button:has-text("Not now")', 'button:has-text("Skip")', 'button:has-text("Cancel")']):
                page.wait_for_timeout(1500)
                continue
            raise LoginError("Google showed a prompt the bot can't dismiss. " + _where(page))

        if _visible(page, 'text=/wrong password/i'):
            raise LoginError("Google says the password is wrong.")

        page.wait_for_timeout(500)

    raise LoginError("Timed out waiting for Google to finish signing in. " + _where(page))


# ─── Helpers ──────────────────────────────────────────────────────────

def _visible(page, selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible()
    except Exception:
        return False


def _click_first(page, selectors) -> bool:
    for sel in selectors:
        if _visible(page, sel):
            try:
                page.locator(sel).first.click()
                return True
            except Exception:
                continue
    return False


def _where(page) -> str:
    try:
        path = urlparse(page.url).path
        text = re.sub(r"\s+", " ", page.inner_text("body")).strip()[:160]
    except Exception:
        return ""
    try:
        inputs = page.eval_on_selector_all(
            "input",
            "els => els.map(e => [e.type, e.name, e.id, e.getAttribute('aria-label'), e.offsetParent !== null ? 'visible' : 'hidden'].join('/'))",
        )
    except Exception:
        inputs = []
    return f"(stuck at {path!r}: {text!r}; inputs: {inputs})"


def _debug_screenshot(page):
    folder = os.getenv("GOOGLE_LOGIN_DEBUG_DIR", "").strip()
    if not folder:
        return
    try:
        os.makedirs(folder, exist_ok=True)
        page.screenshot(path=os.path.join(folder, f"google_login_fail_{int(time.time())}.png"))
    except Exception:
        pass


def _to_netscape(cookies: list) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        bare = domain.lstrip(".").lower()
        if not (bare.endswith("google.com") or bare.endswith("youtube.com")):
            continue
        expires = c.get("expires", -1)
        expires = int(expires) if expires and expires > 0 else 0  # 0 = session cookie (yt-dlp understands it)
        lines.append(
            "\t".join(
                [
                    ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    c.get("path", "/"),
                    "TRUE" if c.get("secure") else "FALSE",
                    str(expires),
                    c["name"],
                    c["value"],
                ]
            )
        )
    return "\n".join(lines) + "\n"
