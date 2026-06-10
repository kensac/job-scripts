from __future__ import annotations

import csv
import json
import os
import select
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from core.paths import DATA_DIR

CSV_FILE = str(DATA_DIR / "actalent_results.csv")
PROFILE_FILE = str(DATA_DIR / "profile.json")
PROGRESS_FILE = str(DATA_DIR / "applied_actalent.txt")

FORM_LOAD_TIMEOUT = 25.0
RESUME_AUTOFILL_WAIT = 4.0

# Phrases on the post-submit confirmation panel ("THANK YOU! We have everything
# needed for the time being. Hang tight while we determine next steps.").
CONFIRM_MARKERS = ["we have everything needed", "determine next steps"]
POLL_INTERVAL = 1.5

# JS walks the shadow-DOM tree (the Allegis/Radancy widget hides fields in shadow roots)
# and returns every input/textarea with its placeholder, label, and a bit of surrounding
# text (ctx) so we can disambiguate radio groups that all just say "Yes"/"No".
COLLECT_JS = r"""
const out = [];
function ctx(e){
  let n = e, t = '';
  for (let i = 0; i < 6 && n; i++){
    n = n.parentElement || (n.getRootNode() && n.getRootNode().host);
    if (n && n.innerText){ t = n.innerText; if (t.length > 25) break; }
  }
  return (t || '').slice(0, 180);
}
function lbl(e){
  if (e.labels && e.labels.length) return e.labels[0].innerText;
  const root = e.getRootNode();
  if (e.id && root.querySelector){
    const l = root.querySelector('label[for="' + e.id + '"]');
    if (l) return l.innerText;
  }
  return e.getAttribute('aria-label') || '';
}
function walk(root){
  root.querySelectorAll('input,textarea').forEach(e => {
    out.push({el: e, type: e.type || 'text', ph: e.placeholder || '', label: (lbl(e) || '').trim(), ctx: ctx(e)});
  });
  root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
}
walk(document);
return out;
"""


def load_profile() -> Dict:
    profile = json.loads(Path(PROFILE_FILE).read_text())
    missing = [k for k in ("first_name", "last_name", "email", "phone") if not profile.get(k)]
    if missing:
        sys.exit(f"profile.json is missing required values: {', '.join(missing)}")
    resume = profile.get("resume_path", "")
    if resume and not Path(resume).expanduser().exists():
        sys.exit(f"resume_path does not exist: {resume}")
    return profile


def load_apply_urls() -> List[str]:
    rows = list(csv.DictReader(open(CSV_FILE)))
    return [r["apply_url"] for r in rows if r.get("apply_url")]


def load_apply_jobs() -> List[tuple]:
    rows = list(csv.DictReader(open(CSV_FILE)))
    return [(r["apply_url"], r.get("title", "")) for r in rows if r.get("apply_url")]


def load_progress() -> set:
    if Path(PROGRESS_FILE).exists():
        return set(Path(PROGRESS_FILE).read_text().splitlines())
    return set()


def mark_done(url: str) -> None:
    with open(PROGRESS_FILE, "a") as f:
        f.write(url + "\n")


def collect_controls(driver) -> List[Dict]:
    return driver.execute_script(COLLECT_JS)


def match(controls: List[Dict], needles: List[str], types: set, use_ctx: bool = False) -> Optional[Dict]:
    for c in controls:
        if c["type"] not in types:
            continue
        hay = (c["ph"] + " " + c["label"] + (" " + c["ctx"] if use_ctx else "")).lower()
        if any(n in hay for n in needles):
            return c
    return None


def type_into(driver, el, value: str) -> bool:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        el.click()
        el.send_keys(Keys.COMMAND, "a")
        el.send_keys(Keys.DELETE)
        el.send_keys(value)
        return True
    except Exception:
        # Fallback for inputs that reject synthetic typing: set value + fire React events.
        try:
            driver.execute_script(
                """
                const el = arguments[0], v = arguments[1];
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                """,
                el, value,
            )
            return True
        except Exception:
            return False


def fill_text(driver, controls, needles, value, filled, failed, name, use_ctx=False) -> None:
    if not value:
        return
    c = match(controls, needles, {"text", "tel", "email", "search", ""}, use_ctx=use_ctx)
    if not c:
        failed.append(name)
        return
    try:
        existing = (c["el"].get_attribute("value") or "").strip()
    except Exception:
        existing = ""
    if existing:
        filled.append(f"{name} (autofilled)")
        return
    if type_into(driver, c["el"], value):
        filled.append(name)
    else:
        failed.append(name)


def click_radio(driver, controls, ctx_needle, label_needle, filled, failed, name) -> None:
    for c in controls:
        if c["type"] != "radio":
            continue
        if ctx_needle and ctx_needle not in c["ctx"].lower():
            continue
        if label_needle in c["label"].lower():
            try:
                driver.execute_script("arguments[0].click();", c["el"])
                filled.append(name)
                return
            except Exception:
                break
    failed.append(name)


def select_dropdown(driver, current_text, option_text, filled, failed, name) -> None:
    if not option_text:
        return
    try:
        trigger = driver.execute_script(
            r"""
            const want = arguments[0].toLowerCase();
            function find(root){
              for (const e of root.querySelectorAll('*')){
                const t = (e.innerText || '').trim().toLowerCase();
                if (t === want && e.offsetParent !== null) return e;
                if (e.shadowRoot){ const r = find(e.shadowRoot); if (r) return r; }
              }
              return null;
            }
            return find(document);
            """,
            current_text,
        )
        if not trigger:
            failed.append(name)
            return
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
        trigger.click()
        time.sleep(0.8)
        option = driver.execute_script(
            r"""
            const want = arguments[0].toLowerCase();
            function find(root){
              for (const e of root.querySelectorAll('li,[role=option],option,div,span')){
                const t = (e.innerText || '').trim().toLowerCase();
                if (t === want && e.offsetParent !== null) return e;
                if (e.shadowRoot){ const r = find(e.shadowRoot); if (r) return r; }
              }
              return null;
            }
            return find(document);
            """,
            option_text,
        )
        if option:
            option.click()
            filled.append(name)
        else:
            failed.append(name)
    except Exception:
        failed.append(name)


def dismiss_cookies(driver) -> None:
    for txt in ("Reject All", "Accept All"):
        try:
            driver.find_element(By.XPATH, f"//button[normalize-space()='{txt}']").click()
            time.sleep(1.5)
            return
        except Exception:
            pass


def wait_for_form(driver) -> bool:
    deadline = time.time() + FORM_LOAD_TIMEOUT
    while time.time() < deadline:
        controls = collect_controls(driver)
        if match(controls, ["first name"], {"text"}):
            return True
        time.sleep(1.0)
    return False


def apply_to_job(driver, url: str, profile: Dict) -> None:
    driver.get(url)
    time.sleep(4)
    dismiss_cookies(driver)

    if not wait_for_form(driver):
        print("  ! form did not load (login required or layout changed) - fill manually")
        return

    resume = profile.get("resume_path", "")
    if resume:
        controls = collect_controls(driver)
        file_input = next((c["el"] for c in controls if c["type"] == "file"), None)
        if file_input:
            try:
                file_input.send_keys(str(Path(resume).expanduser().resolve()))
                print("  uploaded resume, waiting for autofill...")
                time.sleep(RESUME_AUTOFILL_WAIT)
            except Exception as exc:
                print(f"  ! resume upload failed: {exc}")

    controls = collect_controls(driver)
    filled: List[str] = []
    failed: List[str] = []

    fill_text(driver, controls, ["first name"], profile.get("first_name"), filled, failed, "first_name")
    fill_text(driver, controls, ["last name"], profile.get("last_name"), filled, failed, "last_name")
    fill_text(driver, controls, ["phone number", "phone"], profile.get("phone"), filled, failed, "phone")
    fill_text(driver, controls, ["email"], profile.get("email"), filled, failed, "email")
    fill_text(driver, controls, ["main st", "street"], profile.get("street"), filled, failed, "street")
    fill_text(driver, controls, ["beverly hills", "city"], profile.get("city"), filled, failed, "city")
    fill_text(driver, controls, ["90210", "zip", "postal"], profile.get("zip"), filled, failed, "zip")
    fill_text(driver, controls, ["current or most recent"], profile.get("current_title"), filled, failed, "current_title", use_ctx=True)

    click_radio(driver, controls, "sponsor", "yes" if profile.get("requires_sponsorship") else "no", filled, failed, "sponsorship")
    if profile.get("education"):
        click_radio(driver, controls, None, profile["education"].lower(), filled, failed, "education")
    click_radio(driver, controls, "text message", "agree to receive" if profile.get("sms_consent") else "do not want", filled, failed, "sms_consent")

    select_dropdown(driver, "select a country", profile.get("country"), filled, failed, "country")
    select_dropdown(driver, "select a state", profile.get("state"), filled, failed, "state")

    print(f"  filled: {', '.join(filled) if filled else 'none'}")
    if failed:
        print(f"  NOT filled (do manually): {', '.join(failed)}")


# Floating control bar injected at the document root (outside shadow DOM) so it
# survives the widget's re-renders. Buttons set window.__applyAction, which the
# wait loop polls. arguments[0] is the label text.
CONTROLS_JS = r"""
if (!document.getElementById('__apply_bar')) {
  window.__applyAction = null;
  const bar = document.createElement('div');
  bar.id = '__apply_bar';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#111;color:#fff;padding:8px 12px;display:flex;gap:10px;align-items:center;font-family:sans-serif;font-size:14px;box-shadow:0 2px 10px rgba(0,0,0,.5);';
  const label = document.createElement('span');
  label.textContent = arguments[0] || 'Job application';
  label.style.cssText = 'flex:1;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
  function mk(text, color, action){
    const b = document.createElement('button');
    b.textContent = text;
    b.style.cssText = 'cursor:pointer;border:none;border-radius:6px;padding:8px 14px;font-size:14px;font-weight:600;color:#fff;background:' + color + ';';
    b.onclick = () => { window.__applyAction = action; b.textContent = '…'; };
    return b;
  }
  bar.appendChild(label);
  bar.appendChild(mk('✓ Mark done & next', '#1a7f37', 'done'));
  bar.appendChild(mk('↦ Skip (revisit later)', '#9a6700', 'skip'));
  bar.appendChild(mk('✕ Quit', '#b42318', 'quit'));
  document.documentElement.appendChild(bar);
}
"""


def inject_controls(driver, label: str) -> None:
    try:
        driver.execute_script(CONTROLS_JS, label)
    except Exception:
        pass


def detect_submitted(driver) -> bool:
    try:
        text = (driver.execute_script("return document.body.innerText;") or "").lower()
    except Exception:
        return False
    return any(m in text for m in CONFIRM_MARKERS)


def wait_for_outcome(driver, label: str) -> str:
    print("  Auto-advances on submit. Use the in-page buttons, or type: Enter=done+next, s=skip, q=quit.")
    while True:
        inject_controls(driver, label)

        if detect_submitted(driver):
            print("  ✓ submission confirmed")
            return "next"

        try:
            action = driver.execute_script("return window.__applyAction;")
        except Exception:
            action = None
        if action == "done":
            print("  ↳ marked done (button)")
            return "next"
        if action == "skip":
            print("  ↳ skipped (button)")
            return "skip"
        if action == "quit":
            return "quit"

        ready, _, _ = select.select([sys.stdin], [], [], POLL_INTERVAL)
        if ready:
            line = sys.stdin.readline().strip().lower()
            if line == "q":
                return "quit"
            if line == "s":
                return "skip"
            return "next"


def main() -> int:
    profile = load_profile()
    jobs = load_apply_jobs()
    done = load_progress()
    pending = [(u, t) for (u, t) in jobs if u not in done]

    print(f"{len(jobs)} jobs in CSV, {len(done)} already done, {len(pending)} to go.\n")
    if not pending:
        print("Nothing left to apply to.")
        return 0

    options = Options()
    options.add_argument("--window-size=1400,2200")
    options.add_experimental_option("detach", True)
    driver = webdriver.Chrome(options=options)

    try:
        for i, (url, title) in enumerate(pending, 1):
            label = f"[{i}/{len(pending)}] {title}".strip()
            print(label)
            try:
                apply_to_job(driver, url, profile)
            except Exception as exc:
                print(f"  ! error: {exc}")
            outcome = wait_for_outcome(driver, label)
            if outcome == "quit":
                break
            if outcome != "skip":
                mark_done(url)
    finally:
        driver.quit()

    return 0


if __name__ == "__main__":
    sys.exit(main())
