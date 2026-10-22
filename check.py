"""네이버 예약 빈자리 감시 -> 노션 + 메일 알림 (GitHub Actions용)"""
import json, os, re, smtplib, sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

KST = timezone(timedelta(hours=9))
BASE_URL = "https://m.booking.naver.com/booking/13/bizes/55864/items/2008915"
TARGET_DATE = os.environ.get("TARGET_DATE", "2026-10-23")  # YYYY-MM-DD
TARGET_TIMES = ["18:00", "18:30", "19:00"]
DESIGNER = "미주"
URL = f"{BASE_URL}?area=ple&lang=ko&startDate={TARGET_DATE}&tab=book&theme=place"
STATE_FILE = Path("state/seen.json")
DEBUG_DIR = Path("debug")

JS_COLLECT = r"""
() => {
  const timeRe = /^(오전|오후)?\s*\d{1,2}:\d{2}$/;
  const badRe = /disabled|unselectable|soldout|sold-out|closed|impossible/i;
  const out = [];
  for (const el of document.querySelectorAll('button, a, li, label, span, div')) {
    if (el.children.length > 1) continue;
    const text = (el.innerText || '').trim();
    if (!timeRe.test(text)) continue;
    let bad = false, node = el;
    for (let i = 0; i < 3 && node; i++, node = node.parentElement) {
      if (node.disabled || node.getAttribute('aria-disabled') === 'true' || badRe.test(node.className || '')) { bad = true; break; }
    }
    out.push({ text, available: !bad });
  }
  return out;
}
"""


def normalize(text: str) -> str:
    m = re.match(r"^(오전|오후)?\s*(\d{1,2}):(\d{2})$", text)
    ampm, h, mi = m.group(1), int(m.group(2)), m.group(3)
    if ampm == "오후" and h < 12:
        h += 12
    elif ampm is None and h <= 9:  # 표기 없는 6:00 등은 오후로 간주
        h += 12
    return f"{h:02d}:{mi}"


def scrape():
    DEBUG_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844}, locale="ko-KR", timezone_id="Asia/Seoul",
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        designer_ok = False
        try:
            loc = page.get_by_text(DESIGNER, exact=False).first
            loc.wait_for(timeout=5000)
            loc.click()
            page.wait_for_timeout(2000)
            designer_ok = True
        except Exception as e:
            print(f"[경고] 디자이너 '{DESIGNER}' 선택 실패: {e}")

        items = page.evaluate(JS_COLLECT)
        page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
        (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
        browser.close()

    avail = {}
    for it in items:
        t = normalize(it["text"])
        avail[t] = avail.get(t, False) or it["available"]
    return avail, designer_ok


def send_mail(subject, body):
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("MAIL_TO")
    if not (user and pw and to):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": text}, timeout=20).raise_for_status()
    return True


def send_notion(title):
    token, db = os.environ.get("NOTION_TOKEN"), os.environ.get("NOTION_DB_ID")
    if not (token and db):
        return False
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    r = requests.get(f"https://api.notion.com/v1/databases/{db}", headers=h, timeout=20)
    r.raise_for_status()
    title_prop = next(k for k, v in r.json()["properties"].items() if v["type"] == "title")
    body = {
        "parent": {"database_id": db},
        "properties": {title_prop: {"title": [{"text": {"content": title}}]}},
        "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "예약 바로가기", "link": {"url": URL}}}]}}],
    }
    requests.post("https://api.notion.com/v1/pages", headers=h, json=body, timeout=20).raise_for_status()
    return True


def main():
    now = datetime.now(KST)
    if now.strftime("%Y-%m-%d") > TARGET_DATE or (
        now.strftime("%Y-%m-%d") == TARGET_DATE and now.strftime("%H:%M") >= "19:00"
    ):
        print("감시 대상 시간이 지나서 종료합니다.")
        return

    avail, designer_ok = scrape()
    print("감지된 시간대:", json.dumps(avail, ensure_ascii=False, sort_keys=True))
    if not avail:
        print("시간 버튼을 하나도 찾지 못했습니다(페이지 구조 변경/차단 가능). debug/ 확인 필요.")
        sys.exit(1)

    found = [t for t in TARGET_TIMES if avail.get(t)]
    seen = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    new = [t for t in found if t not in seen]

    if new:
        note = "" if designer_ok else f"\n(주의: '{DESIGNER}' 디자이너 선택을 확인하지 못했어요. 링크에서 직접 확인하세요.)"
        title = f"[빈자리] {TARGET_DATE} {', '.join(new)} 남성 컷 ({DESIGNER})"
        text = f"{title}\n{URL}{note}"
        ok = []
        for name, fn in (("메일", lambda: send_mail(title, text)), ("노션", lambda: send_notion(title)),
                         ("텔레그램", lambda: send_telegram(text))):
            try:
                ok.append(fn())
            except Exception as e:
                print(f"[오류] {name} 알림 실패: {e}")
                ok.append(False)
        if not any(ok):
            sys.exit(1)  # 알림이 하나도 못 나갔으면 실패 처리 -> 다음 실행에서 재시도

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(found))


if __name__ == "__main__":
    main()
