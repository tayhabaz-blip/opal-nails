"""Sending text messages.

SMS_PROVIDER picks the service:
  log     (default) only writes the message to the server log. Used until a provider account exists.
  twilio  sends through Twilio. Needs TWILIO_SID, TWILIO_TOKEN and TWILIO_FROM
          (a phone number or an approved sender name).
A provider is one function: send(phone, text) that raises on failure.
"""
import base64
import logging
import os
import re
import urllib.parse
import urllib.request

log = logging.getLogger("opal.sms")
SENT = []  # last messages, for tests and for the log provider


def to_e164(tel: str) -> str:
    t = re.sub(r"\D", "", tel)
    if t.startswith("0"):
        t = "972" + t[1:]
    return "+" + t


def _log(tel, text):
    SENT.append((tel, text))
    del SENT[:-50]
    log.info("SMS (not sent, no provider) to %s: %s", to_e164(tel)[:-4] + "****", text)


def _twilio(tel, text):
    sid, tok, frm = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"], os.environ["TWILIO_FROM"]
    body = urllib.parse.urlencode({"To": to_e164(tel), "From": frm, "Body": text}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status >= 300:
            raise RuntimeError(f"twilio {r.status}")


PROVIDERS = {"log": _log, "twilio": _twilio}


def send(tel: str, text: str):
    PROVIDERS[os.environ.get("SMS_PROVIDER", "log")](tel, text)
