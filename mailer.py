"""Outgoing e-mail for ClassRally (stdlib only).

Modes (MAIL_MODE):
  off   nothing is sent or stored
  log   default — each message is written to <data>/outbox/*.eml (mode 600),
        nothing leaves the server; safe for development and dry runs
  live  messages are sent through SMTP (SMTP_HOST/PORT/USER/PASSWORD,
        SMTP_SECURITY=starttls|ssl|none, MAIL_FROM)

Sending happens in a background thread so HTTP requests never wait for SMTP.
Secrets are never logged.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
import threading
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any

MODES = ("off", "log", "live")
SECURITY = ("starttls", "ssl", "none")
LANGS = ("cs", "en", "sk", "de", "uk")
OUTBOX_KEEP_DAYS = 7  # log mode: the outbox holds one-time links, keep it short


class Mailer:
    def __init__(self, mode: str = "log", outbox_dir: str | Path = "data/outbox",
                 host: str = "", port: int = 587, user: str = "", password: str = "",
                 sender: str = "", security: str = "starttls") -> None:
        mode = (mode or "log").strip().lower()
        self.mode = mode if mode in MODES else "log"
        self.outbox_dir = Path(outbox_dir)
        self.host = host
        self.port = int(port or 587)
        self.user = user
        self._password = password
        self.sender = sender or user
        security = (security or "starttls").strip().lower()
        if security not in SECURITY:
            # A typo must never silently turn encryption off.
            print(f"WARNING: unknown SMTP_SECURITY={security!r} -> using starttls", file=sys.stderr)
            security = "starttls"
        self.security = security
        if self.mode == "live" and not (self.host and self.sender):
            print("WARNING: MAIL_MODE=live without SMTP_HOST/MAIL_FROM -> using log mode", file=sys.stderr)
            self.mode = "log"

    @classmethod
    def from_env(cls, outbox_dir: str | Path) -> "Mailer":
        env = os.environ.get
        return cls(mode=env("MAIL_MODE", "log"), outbox_dir=outbox_dir,
                   host=env("SMTP_HOST", ""), port=int(env("SMTP_PORT", "587") or 587),
                   user=env("SMTP_USER", ""), password=env("SMTP_PASSWORD", ""),
                   sender=env("MAIL_FROM", ""), security=env("SMTP_SECURITY", "starttls"))

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "from": self.sender if self.mode == "live" else ""}

    # -- message building --

    def build(self, to: str, subject: str, text: str) -> EmailMessage:
        name, addr = parseaddr(to)
        if not addr or "@" not in addr or any(c in to for c in "\r\n"):
            raise ValueError("invalid recipient")
        if any(c in subject for c in "\r\n"):
            raise ValueError("invalid subject")
        msg = EmailMessage()
        msg["From"] = self.sender or "ClassRally <noreply@localhost>"
        msg["To"] = formataddr((name, addr))
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        domain = parseaddr(msg["From"])[1].partition("@")[2] or "localhost"
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(text)
        return msg

    def send(self, to: str, subject: str, text: str) -> str:
        """Queue a message. Returns the mode used ('off', 'log' or 'live')."""
        msg = self.build(to, subject, text)
        if self.mode == "off":
            return "off"
        if self.mode == "log":
            self._write_outbox(msg)
            return "log"
        threading.Thread(target=self._send_smtp, args=(msg,), daemon=True).start()
        return "live"

    # -- transports --

    def _write_outbox(self, msg: EmailMessage) -> Path:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now().timestamp() - OUTBOX_KEEP_DAYS * 86400
        for old in self.outbox_dir.glob("*.eml"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        # microseconds keep the file names in creation order
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        path = self.outbox_dir / f"{stamp}-{uuid.uuid4().hex[:8]}.eml"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(msg))
        return path

    def _send_smtp(self, msg: EmailMessage) -> None:
        to = msg["To"]
        try:
            ctx = ssl.create_default_context()
            if self.security == "ssl":
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=ctx)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=30)
                if self.security == "starttls":
                    smtp.starttls(context=ctx)
            with smtp:
                if self.user:
                    smtp.login(self.user, self._password)
                smtp.send_message(msg)
            print(f"mail sent: {msg['Subject']!r} -> {to}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — log and continue, never crash the server
            print(f"mail FAILED: {msg['Subject']!r} -> {to}: {type(e).__name__}: {e}", file=sys.stderr)


# --- Templates (plain text) ---

_SIGNATURE = {
    "cs": "ClassRally — kvízy a písemky pro třídu",
    "en": "ClassRally — quizzes and tests for the classroom",
    "sk": "ClassRally — kvízy a písomky pre triedu",
    "de": "ClassRally — Quiz und Tests für die Klasse",
    "uk": "ClassRally — вікторини та контрольні для класу",
}

_TEMPLATES: dict[str, dict[str, tuple[str, str]]] = {
    "verify": {
        "cs": ("ClassRally: potvrďte e-mailovou adresu",
               "Dobrý den,\n\nk účtu „{nickname}“ v aplikaci ClassRally ({base}) byla zadána tato "
               "e-mailová adresa. Potvrďte ji prosím otevřením odkazu (platí 48 hodin):\n\n{link}\n\n"
               "Potvrzená adresa slouží k obnově zapomenutého hesla. Pokud jste ji nezadávali vy, "
               "tento e-mail ignorujte."),
        "en": ("ClassRally: confirm your e-mail address",
               "Hello,\n\nthis e-mail address was entered for the account \"{nickname}\" in ClassRally "
               "({base}). Please confirm it by opening this link (valid for 48 hours):\n\n{link}\n\n"
               "A confirmed address lets you recover a forgotten password. If you did not enter it, "
               "please ignore this e-mail."),
        "sk": ("ClassRally: potvrďte e-mailovú adresu",
               "Dobrý deň,\n\nk účtu „{nickname}“ v aplikácii ClassRally ({base}) bola zadaná táto "
               "e-mailová adresa. Potvrďte ju, prosím, otvorením odkazu (platí 48 hodín):\n\n{link}\n\n"
               "Potvrdená adresa slúži na obnovenie zabudnutého hesla. Ak ste ju nezadávali vy, "
               "tento e-mail ignorujte."),
        "de": ("ClassRally: Bestätigen Sie Ihre E-Mail-Adresse",
               "Guten Tag,\n\ndiese E-Mail-Adresse wurde für das Konto „{nickname}“ in ClassRally "
               "({base}) angegeben. Bitte bestätigen Sie sie über diesen Link (48 Stunden gültig):"
               "\n\n{link}\n\nMit einer bestätigten Adresse können Sie ein vergessenes Passwort "
               "zurücksetzen. Wenn Sie die Adresse nicht angegeben haben, ignorieren Sie diese E-Mail."),
        "uk": ("ClassRally: підтвердьте адресу електронної пошти",
               "Добрий день,\n\nцю адресу електронної пошти було вказано для облікового запису "
               "«{nickname}» у ClassRally ({base}). Підтвердьте її, будь ласка, за цим посиланням "
               "(дійсне 48 годин):\n\n{link}\n\nПідтверджена адреса потрібна для відновлення "
               "забутого пароля. Якщо ви її не вказували, просто проігноруйте цей лист."),
    },
    "reset": {
        "cs": ("ClassRally: obnova hesla",
               "Dobrý den,\n\nněkdo (nejspíš vy) požádal o obnovu hesla k účtu „{nickname}“ "
               "v aplikaci ClassRally ({base}).\n\nNové heslo nastavíte zde (odkaz platí 60 minut "
               "a lze ho použít jen jednou):\n\n{link}\n\nPokud jste o obnovu nežádali, tento e-mail "
               "ignorujte — heslo zůstane beze změny."),
        "en": ("ClassRally: password reset",
               "Hello,\n\nsomeone (probably you) asked to reset the password of the account "
               "\"{nickname}\" in ClassRally ({base}).\n\nSet a new password here (the link is valid "
               "for 60 minutes and works only once):\n\n{link}\n\nIf you did not ask for this, ignore "
               "this e-mail — your password stays unchanged."),
        "sk": ("ClassRally: obnovenie hesla",
               "Dobrý deň,\n\nniekto (pravdepodobne vy) požiadal o obnovenie hesla k účtu „{nickname}“ "
               "v aplikácii ClassRally ({base}).\n\nNové heslo nastavíte tu (odkaz platí 60 minút a dá "
               "sa použiť iba raz):\n\n{link}\n\nAk ste o obnovenie nežiadali, tento e-mail ignorujte — "
               "heslo zostane bez zmeny."),
        "de": ("ClassRally: Passwort zurücksetzen",
               "Guten Tag,\n\njemand (wahrscheinlich Sie) hat das Zurücksetzen des Passworts für das "
               "Konto „{nickname}“ in ClassRally ({base}) angefordert.\n\nHier legen Sie ein neues "
               "Passwort fest (der Link ist 60 Minuten gültig und nur einmal verwendbar):\n\n{link}\n\n"
               "Wenn Sie dies nicht angefordert haben, ignorieren Sie diese E-Mail — Ihr Passwort "
               "bleibt unverändert."),
        "uk": ("ClassRally: відновлення пароля",
               "Добрий день,\n\nхтось (імовірно, ви) попросив відновити пароль облікового запису "
               "«{nickname}» у ClassRally ({base}).\n\nНовий пароль можна встановити тут (посилання "
               "дійсне 60 хвилин і працює лише один раз):\n\n{link}\n\nЯкщо ви цього не робили, "
               "проігноруйте лист — пароль залишиться без змін."),
    },
    "password_changed": {
        "cs": ("ClassRally: heslo bylo změněno",
               "Dobrý den,\n\nheslo k účtu „{nickname}“ v aplikaci ClassRally ({base}) bylo právě "
               "změněno a všechna přihlášení byla ukončena.\n\nPokud jste to nebyli vy, obnovte si heslo "
               "na adrese {base}/reset a kontaktujte správce."),
        "en": ("ClassRally: your password was changed",
               "Hello,\n\nthe password of the account \"{nickname}\" in ClassRally ({base}) has just "
               "been changed and all sessions were signed out.\n\nIf this was not you, reset your "
               "password at {base}/reset and contact the administrator."),
        "sk": ("ClassRally: heslo bolo zmenené",
               "Dobrý deň,\n\nheslo k účtu „{nickname}“ v aplikácii ClassRally ({base}) bolo práve "
               "zmenené a všetky prihlásenia boli ukončené.\n\nAk ste to neboli vy, obnovte si heslo "
               "na adrese {base}/reset a kontaktujte správcu."),
        "de": ("ClassRally: Ihr Passwort wurde geändert",
               "Guten Tag,\n\ndas Passwort des Kontos „{nickname}“ in ClassRally ({base}) wurde soeben "
               "geändert und alle Anmeldungen wurden beendet.\n\nWenn Sie das nicht waren, setzen Sie "
               "Ihr Passwort unter {base}/reset zurück und wenden Sie sich an den Administrator."),
        "uk": ("ClassRally: пароль змінено",
               "Добрий день,\n\nпароль облікового запису «{nickname}» у ClassRally ({base}) щойно "
               "змінено, усі входи завершено.\n\nЯкщо це були не ви, відновіть пароль на {base}/reset "
               "і зверніться до адміністратора."),
    },
    "invite": {
        "cs": ("ClassRally: pozvánka pro učitele",
               "Dobrý den,\n\n{inviter} vás zve jako učitele do aplikace ClassRally ({base}) — kvízy "
               "a písemky pro třídu.\n\nÚčet si založíte zde (odkaz platí 7 dní a lze ho použít jen "
               "jednou):\n\n{link}\n\nPokud pozvánku nečekáte, tento e-mail ignorujte."),
        "en": ("ClassRally: teacher invitation",
               "Hello,\n\n{inviter} invites you as a teacher to ClassRally ({base}) — quizzes and "
               "tests for the classroom.\n\nCreate your account here (the link is valid for 7 days "
               "and works only once):\n\n{link}\n\nIf you did not expect this invitation, ignore "
               "this e-mail."),
        "sk": ("ClassRally: pozvánka pre učiteľa",
               "Dobrý deň,\n\n{inviter} vás pozýva ako učiteľa do aplikácie ClassRally ({base}) — "
               "kvízy a písomky pre triedu.\n\nÚčet si založíte tu (odkaz platí 7 dní a dá sa použiť "
               "iba raz):\n\n{link}\n\nAk pozvánku nečakáte, tento e-mail ignorujte."),
        "de": ("ClassRally: Einladung für Lehrkräfte",
               "Guten Tag,\n\n{inviter} lädt Sie als Lehrkraft zu ClassRally ({base}) ein — Quiz und "
               "Tests für die Klasse.\n\nIhr Konto legen Sie hier an (der Link ist 7 Tage gültig und "
               "nur einmal verwendbar):\n\n{link}\n\nWenn Sie keine Einladung erwarten, ignorieren "
               "Sie diese E-Mail."),
        "uk": ("ClassRally: запрошення для вчителя",
               "Добрий день,\n\n{inviter} запрошує вас як вчителя до ClassRally ({base}) — вікторини "
               "та контрольні для класу.\n\nСтворіть обліковий запис тут (посилання дійсне 7 днів "
               "і працює лише один раз):\n\n{link}\n\nЯкщо ви не очікували запрошення, проігноруйте "
               "цей лист."),
    },
}


def render(kind: str, lang: str, **values: str) -> tuple[str, str]:
    """Return (subject, body) for a template in the given language (fallback cs)."""
    lang = lang if lang in LANGS else "cs"
    subject, body = _TEMPLATES[kind][lang]
    return subject, body.format(**values) + "\n\n-- \n" + _SIGNATURE[lang] + "\n"
