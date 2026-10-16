"""Comandos de Telegram: /stats /semana /abiertas /estado /ayuda (solo desde tu chat)."""
import logging
import requests

log = logging.getLogger("commands")


def command_loop(token, chat_id, handlers, stop):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    offset = None
    while not stop.is_set():
        try:
            params = {"timeout": 25}
            if offset:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=40)
            if r.status_code == 409:
                log.warning("Otro proceso lee los comandos de este bot (409). Reintento en 60s.")
                stop.wait(60)
                continue
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) != str(chat_id):
                    continue
                text = (m.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                fn = handlers.get(text.split()[0].split("@")[0].lower())
                if fn:
                    fn()
        except Exception as e:
            log.debug("command_loop: %s", e)
            stop.wait(5)
