"""Argv-safe clone contract for the work image (#10).

Pure stdlib. Validation covers scheme/host sanity and dest safety; argv
passing (never shell concatenation, never tmux send-keys) makes option
injection structurally impossible. The future Go runner ports this contract.
"""
import urllib.parse


def validate_repo_url(url):
    """Return (ok, reason). Public HTTPS only: sane scheme/host, no userinfo."""
    if not isinstance(url, str) or not url:
        return False, 'url must be a non-empty string'
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        return False, 'whitespace or control characters in url'
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https':
        return False, 'scheme must be https, got %r' % parsed.scheme
    if not parsed.hostname:
        return False, 'missing host'
    if parsed.username is not None or parsed.password is not None:
        return False, 'userinfo in url not allowed'
    return True, 'ok'


def build_clone_argv(url, dest):
    """Return ['git','clone','--',url,dest]; refuse bad input."""
    ok, reason = validate_repo_url(url)
    if not ok:
        raise ValueError('refusing clone: %s' % reason)
    if not isinstance(dest, str) or not dest:
        raise ValueError('refusing clone: dest must be a non-empty string')
    if dest.startswith('-'):
        raise ValueError('refusing clone: dest must not start with "-"')
    return ['git', 'clone', '--', url, dest]


def should_skip_clone(workspace_dir, listing):
    """Idempotency guard: skip clone when the workspace listing is non-empty.

    Pure decision on the listing (e.g. os.listdir(workspace_dir)); the dir
    itself is passed through for the runner-side signature, never inspected.
    """
    return bool(listing)
