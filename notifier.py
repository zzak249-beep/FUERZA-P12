import logging
import time
import requests
import config as C

log = logging.getLogger("telegram")


def notify(text: str):
    log.info(text.replace("\n", " | ")[:200])
    if not C.TG_TOKEN or not C.TG_CHAT:
        return False
    payload = {"chat_id": C.TG_CHAT, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(4):
        try:
            r = requests.post(f"https://api.telegram.org/bot{C.TG_TOKEN}/sendMessage", json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            if r.ok:
                return True
            log.warning("Telegram %s %s", r.status_code, r.text[:200])
            return False
        except Exception as e:
            log.warning("Telegram error: %s", e)
            time.sleep(1 + attempt)
    return False
