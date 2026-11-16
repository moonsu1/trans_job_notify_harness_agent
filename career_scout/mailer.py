from __future__ import annotations

import smtplib
import ssl
import json
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

from .agent import credential
from .core import Job, now, prefilter
from .policy import finance_employment
from .errors import error_detail


def send_mail(store, settings, mail_id):
    mail = store.db.execute('SELECT * FROM outbox WHERE id=?', (mail_id,)).fetchone()
    if not mail:
        raise ValueError('발송 대기열 ID 없음')
    if mail['status'] in ('sent', 'uncertain', 'sending', 'superseded'):
        return mail['status']
    for item in json.loads(mail['items']):
        row = store.db.execute('SELECT payload,assessment FROM jobs WHERE id=?', (item['id'],)).fetchone()
        if row:
            job = Job.model_validate_json(row['payload'])
            assessment = json.loads(row['assessment']) if row['assessment'] else None
            employment, _ = finance_employment(job, settings, assessment)
            if prefilter(job, settings) or employment in ('unknown', 'excluded'):
                store.mail_status(mail_id, 'superseded', '현재 기업·고용형태 조건과 맞지 않는 과거 리포트')
                return 'superseded'
    password = credential('gmail_app_password')
    if not password:
        store.mail_status(mail_id, 'pending', 'Gmail 앱 비밀번호 미등록')
        return 'pending'
    address = settings['recipient']
    message = EmailMessage(policy=SMTP)
    message['From'] = address
    message['To'] = address
    message['Subject'] = mail['subject']
    message['Message-ID'] = mail['message_id']
    message['Date'] = format_datetime(now())
    message.set_content(mail['markdown'])
    message.add_alternative(mail['html'], subtype='html')
    sending_data = False
    try:
        # Windows hostnames can contain Korean. SMTP EHLO requires ASCII; do not
        # use smtplib's default socket.getfqdn() here.
        with smtplib.SMTP('smtp.gmail.com', 587, local_hostname='career-scout.local', timeout=25) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(address, password)
            code, _ = smtp.mail(address)
            if code != 250:
                raise smtplib.SMTPSenderRefused(code, b'sender rejected', address)
            code, _ = smtp.rcpt(address)
            if code not in (250, 251):
                raise smtplib.SMTPRecipientsRefused({address: (code, b'recipient rejected')})
            store.mail_status(mail_id, 'sending')
            sending_data = True
            code, _ = smtp.data(message.as_bytes())
            if code != 250:
                sending_data = False
                raise smtplib.SMTPDataError(code, b'data rejected')
            # Persist delivery before QUIT: a QUIT failure is not a delivery failure.
            store.mark_sent(mail)
            sending_data = False
        return 'sent'
    except Exception as exc:
        current = store.db.execute('SELECT status FROM outbox WHERE id=?', (mail_id,)).fetchone()[0]
        if current == 'sent':
            return 'sent'
        # An explicit DATA rejection confirms the server did not accept the message.
        status = 'uncertain' if sending_data and not isinstance(exc, smtplib.SMTPDataError) else 'failed'
        # No server text or credentials are copied into user-visible logs.
        store.mail_status(mail_id, status, error_detail(exc, (password,)))
        return status


def retry_pending(store, settings):
    rows = store.db.execute("SELECT id FROM outbox WHERE status IN ('pending','failed') ORDER BY created").fetchall()
    result = {}
    for row in rows:
        result[row['id']] = send_mail(store, settings, row['id'])
    return result
