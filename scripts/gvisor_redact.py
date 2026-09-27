#!/usr/bin/env python3
"""Mask credentials before anything is written to a committed evidence bundle.

E05/E10 will inject a real Anthropic key. Every evidence write goes through
Matrix.log/Matrix.write, so scrubbing there is the single chokepoint. This
reduces accidental disclosure; it is not a guarantee, and a human must still
review a bundle before publishing it.
"""
import os
import re

MASK = '[REDACTED]'
# Names whose values are treated as secrets wherever they appear.
SECRET_NAME = re.compile(
    r'(?i)(?:api[_-]?key|auth[_-]?token|access[_-]?token|secret|password|passwd|credential|'
    r'bearer|private[_-]?key|session[_-]?token)')
# Shapes that are secrets regardless of the surrounding key name.
SHAPES = (
    re.compile(r'sk-ant-[A-Za-z0-9_\-]{8,}'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),
    re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'xox[abprs]-[A-Za-z0-9-]{10,}'),
    re.compile(r'eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S),
)
# NAME=value / NAME: value / --flag value, where NAME looks like a secret.
ASSIGNMENT = re.compile(
    r'(?i)(?P<name>[A-Za-z0-9_.\-]*(?:api[_-]?key|auth[_-]?token|access[_-]?token|secret|password|'
    r'passwd|credential|bearer|private[_-]?key|session[_-]?token)[A-Za-z0-9_.\-]*)'
    r'(?P<sep>"?\s*[=:]\s*|\s+)(?P<value>&\{Uid:\d+ Gid:\d+ Groups:\[[\d ]*\] NoSetGroups:(?:true|false)\}|"[^"]*"|\'[^\']*\'|\S+)')
# Values shorter than this are too generic to mask safely (e.g. "none", "0").
MIN_VALUE = 6
# Literals that are never secrets, so masking them only destroys evidence.
KEEP_VALUES = frozenset(
    ('true', 'false', 'null', 'none', 'nil', 'yes', 'no', 'unset', 'not_run', 'redacted'))
# Fields that deliberately hold a non-secret pointer to where a secret lives.
KEEP_NAMES = frozenset(
    ('credential_delivery_ref', 'authorization_ref', 'credentials_injected', 'model_budget'))


def _keep(value):
    bare = value.strip().strip('"\'')
    return len(bare) < MIN_VALUE or bare.lower() in KEEP_VALUES or bare == MASK


def _kept_name(name):
    """A dotted or prefixed path keeps the exemption of its final segment."""
    return name.lower().rsplit('.', 1)[-1] in KEEP_NAMES


class Redactor:
    """Holds literal secret values plus the shape/name rules above."""

    def __init__(self, values=()):
        self.values = set()
        for value in values:
            self.add(value)

    def add(self, value):
        """Register one literal secret. Short or blank values are ignored."""
        if isinstance(value, str) and len(value.strip()) >= MIN_VALUE:
            self.values.add(value.strip())
        return self

    def add_from_environ(self, environ=None):
        """Register values of environment variables whose name looks secret."""
        for name, value in (os.environ if environ is None else environ).items():
            if SECRET_NAME.search(name):
                self.add(value)
        return self

    def text(self, value):
        """Mask a single string."""
        if not isinstance(value, str) or not value:
            return value
        # Longest first so a prefix of one secret cannot leave a tail behind.
        for secret in sorted(self.values, key=len, reverse=True):
            value = value.replace(secret, MASK)
        for shape in SHAPES:
            value = shape.sub(MASK, value)

        def assignment(match):
            if match.group('name') == 'Credential' and re.fullmatch(
                    r'&\{Uid:\d+ Gid:\d+ Groups:\[[\d ]*\] NoSetGroups:(?:true|false)\}', match.group('value')):
                return match.group(0)  # Go's numeric OS identity, not a secret value.
            if _kept_name(match.group('name')) or _keep(match.group('value')):
                return match.group(0)
            return match.group('name') + match.group('sep') + MASK
        return ASSIGNMENT.sub(assignment, value)

    def scrub(self, obj):
        """Mask strings anywhere in a JSON-serializable structure, keys included."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            result = {}
            for key, item in obj.items():
                name = self.text(key) if isinstance(key, str) else key
                # A secret-looking field name masks its own value, whatever it holds.
                if (isinstance(key, str) and not _kept_name(key)
                        and SECRET_NAME.search(key) and isinstance(item, str) and not _keep(item)):
                    result[name] = MASK
                else:
                    result[name] = self.scrub(item)
            return result
        if isinstance(obj, (list, tuple)):
            return [self.scrub(item) for item in obj]
        return obj

    def leaks(self, obj):
        """Registered secrets still present after scrubbing; must always be empty."""
        import json
        text = json.dumps(self.scrub(obj), ensure_ascii=False, default=str)
        return sorted(s for s in self.values if s in text)
