import logging
import requests
import config as C

log = logging.getLogger("telegram")


def notify(text: str):
    log.info(text.replace("\n", " | "))
    if not C.TG_TOKEN or not C.TG_CHAT:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{C.TG_TOKEN}/sendMessage",
            json={"chat_id": C.TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("Telegram %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram error: %s", e)
