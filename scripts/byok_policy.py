#!/usr/bin/env python3
"""BYOK credential policy rules for issue #21 (in-repo stdlib slice).

Rules-only library for the #21 BYOK track. Per the 2026-10-02 delivery
note this is the **G03 sub-scope first**: ephemeral injection, missing-key
rejection, revocation, and backup exclusion as pure policy. The FULL
credential proxy (Claude traffic streamed through a trusted proxy so the
sandbox never reads the key) remains the FINAL GOAL of #21 and is NOT
claimed here (docs/contracts/byok-policy.md maps delivered vs final).

Components:
  SecretStore — put/get/list/revoke over an enveloped at-rest form
      (sha256 digest + sealed-blob stand-in; NO plaintext field name or
      value ever enters the persisted dict). Plaintext lives in the
      trusted proxy memory only, behind the audited use() accessor.
      get()/list() return metadata ONLY (name, created, last_used_at,
      digest prefix) — never the value. Logs and audit lines identify a
      key by digest prefix (redact(), same idea as tenant_authz).
  require_credential(state) — missing-key semantics: Suspend + no key ->
      409 credentials_required (stays Suspend, no operation; delegated to
      workspace-persistence.json / control-plane-api.json); no key at
      create -> the agent process is refused (ADR sandbox-lifecycle §6:
      缺 key 不啟動 Claude).
  plan_injection(sandbox_id, generation, key_id, rotate=False) —
      ephemeral injection contract: tmpfs-only /run/credentials/<name>,
      mode 0600, owner runtime uid, lifetime = instance lifetime, cleared
      on stop. Rejected on generation mismatch or a second key without
      explicit rotate; rotate replaces atomically (old revoked at the
      same timestamp as the new plan).
  revoke(key_id) — immediate: pending injections rejected, active
      injections marked for teardown (per sandbox+generation), and NEVER
      an auto-kill of the sandbox (human/policy path, watchdog
      isolation); returns the teardown actions.
  upstream_rule(url, is_redirect=False) — egress guard for the FIXED
      Anthropic upstream (https + api.anthropic.com + /v1/ prefix only);
      rejects other schemes/hosts/paths/ports, redirects
      (redirect_not_followed), userinfo and query smuggling. Pure URL
      check; the streaming proxy itself is a runtime TODO.
  backup_plan() / trust_report() — backup exclusion list (secret store
      namespace) and the machine-readable trust-boundary statement.

Tests: scripts/test_byok_policy.py (mutate-and-fail guards included).
Spec: docs/contracts/byok-policy.md.
"""
import hashlib
import secrets
import threading
import time
import urllib.parse

# --------------------------------------------------------------- constants

UPSTREAM_SCHEME = "https"
UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PATH_PREFIX = "/v1/"

CREDENTIAL_DIR = "/run/credentials"
SECRET_STORE_NAMESPACE = "secret-store/"
INJECTION_MODE = "0o600"
INJECTION_OWNER = "runtime-uid"
DIGEST_PREFIX_LEN = 12

# Missing-key semantics, delegated to the contracts (never re-invented):
# control-plane-api.json security.missing_credential_on_resume and
# workspace-persistence.json invariant credentials_never_persisted_in_volumes.
CREDENTIALS_REQUIRED = {
    "http_status": 409,
    "code": "credentials_required",
    "creates_operation": False,
    "stays": "Suspend",
}
NO_KEY_REFUSE_START = {
    "http_status": None,  # not an HTTP error: creation itself is still accepted
    "behavior": ("the Claude agent process is not started until a credential "
                 "is injected (ADR sandbox-lifecycle §6: 缺 key 不啟動 Claude)"),
}


# ----------------------------------------------------------------- verdicts

class Verdict(tuple):
    """(allowed, reason): the answer vocabulary for the standalone gates."""

    __slots__ = ()

    def __new__(cls, allowed, reason):
        return super().__new__(cls, (allowed, reason))

    allowed = property(lambda self: self[0])
    reason = property(lambda self: self[1])


class InjectionPlan(tuple):
    """(ok, injection, reason): the answer vocabulary of plan_injection."""

    __slots__ = ()

    def __new__(cls, ok, injection, reason):
        return super().__new__(cls, (ok, injection, reason))

    ok = property(lambda self: self[0])
    injection = property(lambda self: self[1])
    reason = property(lambda self: self[2])


def redact(digest):
    """Loggable stand-in for a key: digest prefix only, never the value
    (same idiom as tenant_authz.redact / gvisor evidence scrubbing)."""
    return digest[:DIGEST_PREFIX_LEN]


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# -------------------------------------------------------------- secret store

class SecretStore:
    """Enveloped-at-rest secret store for the trusted proxy context.

    At rest (at_rest(), the form a persisted store WOULD take) each key is
    {name, digest, sealed_blob, created_at, last_used_at, revoked_at} —
    no plaintext field name, no plaintext value. The sealed blob is a
    STAND-IN for real envelope crypto (runtime TODO): a store-salt-bound
    token that cannot be reversed into the value.

    In memory, plaintext exists only in self._plaintext and leaves it only
    through use(), which is audited (digest-prefix key_id, never the value)
    and refuses revoked/unknown keys.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._salt = secrets.token_hex(16)  # stand-in KEK salt
        self._plaintext = {}  # key_id -> value; trusted proxy memory ONLY
        self._records = {}    # key_id -> enveloped record (at-rest form)
        self._generations = {}      # sandbox_id -> current generation
        self._injections = {}       # sandbox_id -> one injection record
        self._injection_history = []  # superseded/rotated/stopped records
        self.audit = []             # redacted events only, grep-safe

    # -- credential CRUD (metadata views only) ------------------------------

    def put(self, name, value):
        """Register a credential; returns its digest-derived key_id.

        Re-sending the same value re-registers it (the CLI resume re-send
        flow) and clears any revoked mark; a DIFFERENT value gets a
        different key_id and must go through injection rotate.
        """
        if (not isinstance(name, str) or not name
                or not isinstance(value, str) or not value.strip()):
            raise ValueError("name and value must be non-empty strings")
        digest = _digest(value)
        key_id = "key_" + redact(digest)
        sealed = "sealed:" + hashlib.sha256(
            (self._salt + digest).encode()).hexdigest()
        ts = self._clock()
        with self._lock:
            existing = self._records.get(key_id)
            if existing is None:
                self._records[key_id] = {
                    "name": name,
                    "digest": digest,
                    "sealed_blob": sealed,
                    "created_at": ts,
                    "last_used_at": None,
                    "revoked_at": None,
                }
                event = "put"
            else:  # deliberate re-registration (resume re-send)
                existing["name"] = name
                existing["sealed_blob"] = sealed
                existing["revoked_at"] = None
                event = "re_register"
            self._plaintext[key_id] = value
            self.audit.append({"event": event, "key_id": key_id, "at": ts})
        return key_id

    def get(self, key_id):
        """Metadata view ONLY: name, created, last_used_at, digest prefix.
        NEVER the value — the read API cannot leak what it does not hold."""
        with self._lock:
            record = self._records.get(key_id)
        if record is None:
            return None
        return {
            "key_id": key_id,
            "name": record["name"],
            "created_at": record["created_at"],
            "last_used_at": record["last_used_at"],
            "digest_prefix": redact(record["digest"]),
            "revoked_at": record["revoked_at"],
        }

    def list(self):
        with self._lock:
            key_ids = list(self._records)
        return [view for view in (self.get(k) for k in key_ids)
                if view is not None]

    def use(self, key_id, purpose="proxy"):
        """The ONLY plaintext accessor: returns the value to the trusted
        proxy context, audited by digest-prefix key_id. Unknown and
        revoked keys return None (audited as use_denied)."""
        ts = self._clock()
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                self.audit.append({"event": "use_denied",
                                   "reason": "unknown_key",
                                   "key_id": key_id, "at": ts})
                return None
            if record["revoked_at"] is not None:
                self.audit.append({"event": "use_denied", "reason": "revoked",
                                   "key_id": key_id, "purpose": purpose,
                                   "at": ts})
                return None
            record["last_used_at"] = ts
            self.audit.append({"event": "use", "key_id": key_id,
                               "purpose": purpose, "at": ts})
            return self._plaintext[key_id]

    def at_rest(self):
        """The enveloped form a persisted store WOULD take — no plaintext
        field name, no plaintext value (backup/migration debug view)."""
        with self._lock:
            return {key_id: dict(record)
                    for key_id, record in self._records.items()}

    # -- revocation ----------------------------------------------------------

    def revoke(self, key_id):
        """Immediate revocation. Pending injections are rejected, active
        ephemeral injections are marked for teardown (per sandbox and
        generation), and the sandbox is NEVER auto-killed — teardown or
        kill stays a human/policy decision (watchdog isolation)."""
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                return {"revoked": False, "reason": "unknown_key",
                        "teardown": [], "kill_sandbox": False}
            ts = self._clock()
            record["revoked_at"] = ts
            self._plaintext.pop(key_id, None)  # value leaves memory now
            teardown = []
            for sandbox_id, injection in self._injections.items():
                if (injection["key_id"] != key_id
                        or injection["status"] not in ("pending", "active")):
                    continue
                teardown.append({
                    "action": ("teardown_injection"
                               if injection["status"] == "active"
                               else "reject_pending_injection"),
                    "sandbox_id": sandbox_id,
                    "generation": injection["generation"],
                    "path": injection["descriptor"]["path"],
                })
                injection["status"] = "revoked"
                injection["revoked_at"] = ts
                injection["revoked_reason"] = "key_revoked"
            self.audit.append({"event": "revoke", "key_id": key_id,
                               "at": ts, "teardown_count": len(teardown)})
            return {
                "revoked": True,
                "key_id": key_id,
                "teardown": teardown,
                "kill_sandbox": False,
                "note": ("no auto-kill: sandbox teardown/kill stays a "
                         "human/policy decision (watchdog isolation)"),
            }

    # -- ephemeral injection -------------------------------------------------

    def set_sandbox_generation(self, sandbox_id, generation):
        """Register the sandbox's CURRENT generation (create/resume
        allocates a new one). A new generation supersedes any surviving
        injection record: resume starts a fresh instance whose tmpfs is
        empty (workspace-persistence resume drops ephemeral credentials)."""
        with self._lock:
            self._generations[sandbox_id] = generation
            existing = self._injections.get(sandbox_id)
            if (existing is not None
                    and existing["status"] in ("pending", "active")
                    and existing["generation"] != generation):
                existing["status"] = "revoked"
                existing["revoked_at"] = self._clock()
                existing["revoked_reason"] = "superseded_by_new_generation"
                self._injection_history.append(dict(existing))
                del self._injections[sandbox_id]

    def plan_injection(self, sandbox_id, generation, key_id, rotate=False):
        """Plan the ephemeral injection for one sandbox instance.

        Rejections: unknown_key, key_revoked (covers any pending injection
        of a revoked key), generation_mismatch (stale or unregistered
        generation — conservative), already_injected (same key twice
        without rotate), key_conflict (a second key without explicit
        rotate). rotate=True replaces the injection ATOMICALLY: the old
        record is revoked at the same timestamp the new one is planned.
        """
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                return InjectionPlan(False, None, "unknown_key")
            if record["revoked_at"] is not None:
                return InjectionPlan(False, None, "key_revoked")
            if self._generations.get(sandbox_id) != generation:
                return InjectionPlan(False, None, "generation_mismatch")
            existing = self._injections.get(sandbox_id)
            ts = self._clock()
            if existing is not None and existing["status"] in ("pending",
                                                               "active"):
                if not rotate:
                    reason = ("already_injected"
                              if existing["key_id"] == key_id
                              else "key_conflict")
                    return InjectionPlan(False, None, reason)
                existing["status"] = "revoked"
                existing["revoked_at"] = ts  # SAME ts as the new plan
                existing["revoked_reason"] = "rotate"
                self._injection_history.append(dict(existing))
            injection = {
                "sandbox_id": sandbox_id,
                "generation": generation,
                "key_id": key_id,
                "status": "pending",
                "planned_at": ts,
                "confirmed_at": None,
                "revoked_at": None,
                "revoked_reason": None,
                "descriptor": {
                    "path": f"{CREDENTIAL_DIR}/{record['name']}",
                    "filesystem": "tmpfs",  # never disk-backed
                    "mode": INJECTION_MODE,
                    "owner": INJECTION_OWNER,
                    "lifetime": "instance",
                    "cleared_on": "stop",
                },
            }
            self._injections[sandbox_id] = injection
            return InjectionPlan(True, dict(injection), "ok")

    def confirm_injection(self, sandbox_id, generation):
        """Runtime confirms the tmpfs mount. Only a still-pending plan for
        the CURRENT generation of a non-revoked key activates; a key
        revoked between plan and confirm is rejected here."""
        with self._lock:
            injection = self._injections.get(sandbox_id)
            if (injection is None or injection["generation"] != generation
                    or injection["status"] != "pending"):
                return Verdict(False, "no_pending_injection")
            if self._generations.get(sandbox_id) != generation:
                return Verdict(False, "generation_mismatch")
            record = self._records.get(injection["key_id"])
            if record is None or record["revoked_at"] is not None:
                injection["status"] = "revoked"
                injection["revoked_at"] = self._clock()
                injection["revoked_reason"] = "key_revoked"
                return Verdict(False, "key_revoked")
            injection["status"] = "active"
            injection["confirmed_at"] = self._clock()
            return Verdict(True, "ok")

    def active_injections(self):
        """Active injections per sandbox+generation (revocation input)."""
        with self._lock:
            return [dict(injection)
                    for injection in self._injections.values()
                    if injection["status"] == "active"]

    def clear(self, sandbox_id):
        """Stop path: the instance's tmpfs credentials are cleared
        (workspace-persistence: cleared on stop). Returns cleared paths."""
        with self._lock:
            injection = self._injections.pop(sandbox_id, None)
            if injection is None or injection["status"] not in ("pending",
                                                                "active"):
                return []
            injection["status"] = "revoked"
            injection["revoked_at"] = self._clock()
            injection["revoked_reason"] = "stopped"
            self._injection_history.append(dict(injection))
            return [injection["descriptor"]["path"]]


# ------------------------------------------------------------ missing key

def require_credential(state):
    """Missing-key gate.

    state: {"action": "create"|"resume"|..., "observed_state": ...,
            "has_credential": bool}

    - Suspend -> resume without a key: 409 credentials_required — no
      operation created, stays Suspend (semantics DELEGATED to
      workspace-persistence.json / control-plane-api.json, not re-invented
      here; see the CREDENTIALS_REQUIRED mapping).
    - create without a key: the sandbox may be created, but the agent
      process is refused (NO_KEY_REFUSE_START, ADR §6 缺 key 不啟動).
    """
    if state.get("has_credential"):
        return Verdict(True, "ok")
    if state.get("action") == "create":
        return Verdict(False, "no_key_refuse_start")
    return Verdict(False, "credentials_required")


# ------------------------------------------------------------ egress guard

def upstream_rule(url, is_redirect=False):
    """Pure URL check for the FIXED Anthropic upstream (no proxy runtime).

    Allow: https, exactly the configured api host (default port only),
    path under /v1/, no userinfo, no query. Redirects are NEVER followed
    (the fixed upstream does not legitimately redirect): any redirect
    target is denied with redirect_not_followed, so the key can never be
    bounced to another host.
    """
    if is_redirect:
        return Verdict(False, "redirect_not_followed")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port  # raises ValueError on a bad port literal
    except (ValueError, AttributeError, TypeError):
        return Verdict(False, "invalid_url")
    if parts.username or parts.password:
        return Verdict(False, "userinfo_present")
    if parts.scheme != UPSTREAM_SCHEME:
        return Verdict(False, "scheme_not_https")
    if (parts.hostname or "").lower() != UPSTREAM_HOST:
        return Verdict(False, "host_not_allowed")
    if port is not None and port != 443:
        return Verdict(False, "host_not_allowed")  # non-default port
    if not parts.path.startswith(UPSTREAM_PATH_PREFIX):
        return Verdict(False, "path_not_allowed")
    if parts.query:
        return Verdict(False, "query_not_allowed")  # key-smuggling channel
    return Verdict(True, "allow")


# ------------------------------------------------- backup / trust boundary

def backup_plan():
    """Swap-core/backup exclusion list: the secret store namespace and the
    credential tmpfs are excluded. Neither plaintext NOR sealed blobs are
    backed up — the sealed form is bound to the store-local key stand-in,
    and a backup carrying both would just move the key with the data."""
    return {
        "excluded": [SECRET_STORE_NAMESPACE, CREDENTIAL_DIR + "/"],
        "rules": [
            "plaintext never enters any backup",
            "sealed blobs are excluded too (store-local key never travels "
            "with a backup)",
        ],
    }


def trust_report():
    """Machine-readable trust-boundary statement (honest per the
    2026-10-02 note: the full proxy is the FINAL GOAL, not claimed)."""
    return {
        "schema": "byok-trust-boundary/1",
        "delivered_scope": "G03: ephemeral injection / missing-key "
                           "rejection / revocation / backup exclusion",
        "final_goal_full_proxy": {
            "claimed": False,
            "description": "key stays inside the trusted proxy; the "
                           "sandbox never reads it — final goal of #21, "
                           "not delivered in this slice",
        },
        "key_held_in": "trusted proxy memory only (SecretStore plaintext "
                       "map, audited use() accessor)",
        # Honest limited-trial disclosure (ADR sandbox-lifecycle §5:
        # 受限期必須揭露 key 可被沙盒中的使用者／Agent 程序讀取).
        "key_readable_by_sandbox_processes": True,
        "at_rest_form": "sha256 digest + sealed-blob stand-in (real "
                        "envelope crypto: runtime TODO)",
        "never_persisted_in": ["db", "logs", "images", "workspace_volume",
                               "home_volume", "backups"],
        "logs_identify_keys_by": "digest prefix only",
        "injection": {
            "path": CREDENTIAL_DIR + "/<name>",
            "filesystem": "tmpfs",
            "mode": INJECTION_MODE,
            "owner": INJECTION_OWNER,
            "lifetime": "instance lifetime; cleared on stop",
        },
        "transport": "https to the fixed Anthropic upstream only "
                     "(upstream_rule); redirects never followed",
        "revocation": "immediate: pending rejected, active marked for "
                      "teardown; the sandbox is NEVER auto-killed "
                      "(human/policy path)",
    }
