import re
import logging
from typing import Any


class MaskSensitiveDataFilter(logging.Filter):
    """
    Logging filter that sanitizes log records before emission to ensure that
    OTPs, JWTs, bearer tokens, passwords, and sensitive keys are never printed in plain text.
    """
    SENSITIVE_PATTERNS = [
        # Catch any 4-8 digit number sequence when preceded by OTP / code / pin keywords
        (re.compile(r'(?i)\b((?:doorstep\s+)?otp\s*[:=\s]\s*)\d{4,8}\b'), r'\1[REDACTED_OTP]'),
        (re.compile(r'(?i)\b(code\s*[:=\s]\s*)\d{4,8}\b'), r'\1[REDACTED_OTP]'),
        (re.compile(r'(?i)\b(pin\s*[:=\s]\s*)\d{4,8}\b'), r'\1[REDACTED_OTP]'),
        # Standalone OTP patterns (e.g., OTP: 123456 or otp=123456)
        (re.compile(r'(?i)\b(otp\b[^0-9\r\n]{0,20})(\d{4,8})\b'), r'\1[REDACTED_OTP]'),
        # Bearer tokens
        (re.compile(r'(?i)\b(bearer\s+)[A-Za-z0-9_\-\.]+'), r'\1[REDACTED_TOKEN]'),
        # JWT strings (header.payload.signature)
        (re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+\b'), '[REDACTED_JWT]'),
        # Passwords, secrets, and api keys
        (re.compile(r'(?i)\b((?:password|secret|api[_-]?key)\s*[:=]\s*)[^\s,]+'), r'\1[REDACTED]'),
        # Reset tokens and uids
        (re.compile(r'(?i)\b((?:token|uid)\s*[:=]\s*)[^\s,]+'), r'\1[REDACTED]'),
    ]


    def _sanitize(self, text: Any) -> Any:
        if not isinstance(text, str):
            return text
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._sanitize(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._sanitize(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._sanitize(v) for v in record.args)
            elif isinstance(record.args, list):
                record.args = [self._sanitize(v) for v in record.args]
        return True


class MaskSensitiveFormatter(logging.Formatter):
    """
    Logging formatter that ensures formatted record strings do not leak sensitive values.
    """
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        filter_instance = MaskSensitiveDataFilter()
        return filter_instance._sanitize(formatted)
