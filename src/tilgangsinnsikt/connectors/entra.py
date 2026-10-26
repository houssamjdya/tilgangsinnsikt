"""Read-only Microsoft Entra ID connector: users, groups and memberships from Graph.

Nothing read from the tenant is ever logged or printed; callers get counts only.
"""

import csv
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import msal
import psycopg
import yaml
from psycopg import sql

from tilgangsinnsikt import db
from tilgangsinnsikt.config import EntraSettings

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]
USER_FIELDS = [
    "id",
    "userPrincipalName",
    "displayName",
    "accountEnabled",
    "department",
    "jobTitle",
    "employeeId",
    "signInActivity",
]
GROUP_FIELDS = "id,displayName,description,securityEnabled"
PAGE_SIZE = 999
MAX_RETRIES = 5
RETRY_STATUSES = {429, 503, 504}
SOURCE = "entra"
REQUIRED = {
    "tenant_id": "ENTRA_TENANT_ID",
    "client_id": "ENTRA_CLIENT_ID",
    "client_secret": "ENTRA_CLIENT_SECRET",
}

ACCOUNT_COLUMNS = ["account_id", "identity_id", "app_id", "username", "enabled", "last_login"]
ENTITLEMENT_COLUMNS = ["entitlement_id", "app_id", "name", "description", "risk_level"]
ASSIGNMENT_COLUMNS = ["account_id", "entitlement_id", "granted_at", "granted_by"]

# httpx and MSAL log request URLs at debug and info level, and Graph URLs carry object IDs.
for name in ("httpx", "httpcore", "msal"):
    logging.getLogger(name).setLevel(logging.WARNING)


class EntraError(Exception):
    """An error whose message is safe to print: it never contains tenant data or secrets."""


class GraphError(EntraError):
    def __init__(self, what: str, status: int, code: str | None) -> None:
        self.status = status
        super().__init__(f"Graph request for {what} failed with status {status} ({code or '-'})")


def require(settings: EntraSettings) -> None:
    missing = [env for attr, env in REQUIRED.items() if not getattr(settings, attr)]
    if missing:
        raise EntraError(f"missing settings: {', '.join(missing)} (set them in .env)")


def token_error(result: dict[str, Any]) -> str:
    # The description can name the tenant or the app id; only the error codes are kept.
    codes = sorted(set(re.findall(r"AADSTS\d+", result.get("error_description") or "")))
    return " ".join([result.get("error") or "unknown_error", *codes])


def msal_token(settings: EntraSettings) -> Callable[[], str]:
    require(settings)
    # MSAL errors can quote the authority URL, which contains the tenant id, so they are
    # replaced by messages without it. Network errors from MSAL are OSError subclasses.
    try:
        app = msal.ConfidentialClientApplication(
            settings.client_id,
            authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
            client_credential=settings.client_secret.get_secret_value(),
        )
    except (ValueError, OSError):
        message = "could not set up sign-in: check ENTRA_TENANT_ID and the network"
        raise EntraError(message) from None

    def token() -> str:
        try:
            result = app.acquire_token_for_client(scopes=SCOPES)
        except (ValueError, OSError):
            raise EntraError("token request failed: could not reach the token endpoint") from None
        if "access_token" not in result:
            raise EntraError(f"token request failed: {token_error(result)}")
        return result["access_token"]

    return token


def client(settings: EntraSettings) -> "GraphClient":
    return GraphClient(msal_token(settings))


def _retry_after(response: httpx.Response, attempt: int) -> float:
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return float(2**attempt)


def _error_code(response: httpx.Response) -> str | None:
    try:
        return response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


class GraphClient:
    def __init__(
        self,
        token: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = token
        self.sleep = sleep
        self.http = httpx.Client(transport=transport, timeout=60.0)

    def pages(self, path: str, params: dict[str, Any], what: str) -> Iterator[dict[str, Any]]:
        url: str | None = GRAPH + path
        query: dict[str, Any] | None = params
        while url:
            # A next link elsewhere would receive the bearer token.
            if not url.startswith(GRAPH + "/"):
                raise EntraError(f"Graph returned a next link outside {GRAPH} for {what}")
            data = self._get(url, query, what)
            yield from data.get("value", [])
            url, query = data.get("@odata.nextLink"), None

    def _get(self, url: str, params: dict[str, Any] | None, what: str) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES + 1):
            response = self.http.get(
                url, params=params, headers={"Authorization": f"Bearer {self.token()}"}
            )
            if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                self.sleep(_retry_after(response, attempt))
                continue
            if response.is_error:
                raise GraphError(what, response.status_code, _error_code(response))
            return response.json()
        raise AssertionError("unreachable")


@dataclass
class Snapshot:
    users: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    members: dict[str, list[str]]
    skipped_members: int
    sign_in_activity: bool


def fetch(client: GraphClient) -> Snapshot:
    try:
        users = list(_users(client, USER_FIELDS))
        sign_in_activity = True
    except GraphError as error:
        # signInActivity needs Entra ID P1 and AuditLog.Read.All; without either, Graph
        # refuses the whole request.
        if error.status != 403:
            raise
        fields = [f for f in USER_FIELDS if f != "signInActivity"]
        users = list(_users(client, fields))
        sign_in_activity = False
    groups = list(client.pages("/groups", {"$select": GROUP_FIELDS, "$top": PAGE_SIZE}, "groups"))
    members: dict[str, list[str]] = {}
    skipped = 0
    for group in groups:
        ids = []
        path = f"/groups/{quote(group['id'], safe='')}/members"
        for member in client.pages(path, {"$select": "id", "$top": PAGE_SIZE}, "group members"):
            # Nested groups, devices and service principals are not user accounts.
            if member.get("@odata.type") == "#microsoft.graph.user":
                ids.append(member["id"])
            else:
                skipped += 1
        members[group["id"]] = ids
    return Snapshot(users, groups, members, skipped, sign_in_activity)


def _users(client: GraphClient, fields: list[str]) -> Iterator[dict[str, Any]]:
    return client.pages("/users", {"$select": ",".join(fields), "$top": PAGE_SIZE}, "users")


@dataclass(frozen=True)
class RiskPatterns:
    high: tuple[str, ...] = ()
    medium: tuple[str, ...] = ()

    def risk(self, name: str) -> str:
        lowered = name.lower()
        for level, patterns in (("high", self.high), ("medium", self.medium)):
            if any(fnmatch(lowered, p.lower()) for p in patterns):
                return level
        return "low"


def load_risk_patterns(path: Path) -> RiskPatterns:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RiskPatterns(tuple(raw.get("high") or ()), tuple(raw.get("medium") or ()))


def load_identity_map(path: Path) -> dict[str, str]:
    """CSV with the columns upn and identity_id, for users without an employeeId."""
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not {"upn", "identity_id"} <= set(reader.fieldnames or ()):
            raise EntraError(f"{path.name} needs the columns upn and identity_id")
        return {row["upn"].strip().lower(): row["identity_id"].strip() for row in reader}


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def last_login(user: dict[str, Any]) -> datetime | None:
    # Single sign-on often refreshes tokens without an interactive sign-in, so the later
    # of the two is the best sign that the account is in use.
    activity = user.get("signInActivity") or {}
    stamps = [
        _timestamp(activity.get("lastSignInDateTime")),
        _timestamp(activity.get("lastNonInteractiveSignInDateTime")),
    ]
    return max((s for s in stamps if s), default=None)


@dataclass
class EntraRows:
    accounts: list[tuple] = field(default_factory=list)
    entitlements: list[tuple] = field(default_factory=list)
    assignments: list[tuple] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)


def map_snapshot(
    snapshot: Snapshot,
    identities: set[str],
    identity_map: dict[str, str],
    patterns: RiskPatterns,
) -> EntraRows:
    rows = EntraRows()
    counts = rows.counts
    accounts = set()
    for user in snapshot.users:
        upn = user.get("userPrincipalName") or ""
        employee_id = (user.get("employeeId") or "").strip()
        identity_id = None
        if employee_id in identities:
            identity_id = employee_id
            counts["linked by employeeId"] += 1
        elif identity_map.get(upn.lower()) in identities:
            identity_id = identity_map[upn.lower()]
            counts["linked by mapping file"] += 1
        else:
            counts["not linked (orphan)"] += 1
        enabled = bool(user.get("accountEnabled"))
        counts["enabled accounts"] += enabled
        accounts.add(user["id"])
        rows.accounts.append((user["id"], identity_id, "entra", upn, enabled, last_login(user)))
    counts["users"] = len(snapshot.users)

    for group in snapshot.groups:
        risk = patterns.risk(group.get("displayName") or "")
        counts[f"{risk}-risk groups"] += 1
        rows.entitlements.append(
            (group["id"], "entra", group.get("displayName") or "", group.get("description"), risk)
        )
        for member in dict.fromkeys(snapshot.members.get(group["id"], [])):
            if member in accounts:
                rows.assignments.append((member, group["id"], None, None))
            else:
                counts["memberships of unknown users"] += 1
    counts["groups"] = len(snapshot.groups)
    counts["memberships"] = len(rows.assignments)
    counts["skipped non-user members"] = snapshot.skipped_members
    return rows


def write(conn: psycopg.Connection, rows: EntraRows) -> None:
    """Replace every row with source 'entra' and nothing else, all or nothing."""
    with conn.transaction():
        for table in ("assignment", "entitlement", "account"):
            conn.execute(
                sql.SQL("delete from {} where source = %s").format(sql.Identifier(table)),
                (SOURCE,),
            )
        db.copy_rows(
            conn, "account", [*ACCOUNT_COLUMNS, "source"], (r + (SOURCE,) for r in rows.accounts)
        )
        db.copy_rows(
            conn,
            "entitlement",
            [*ENTITLEMENT_COLUMNS, "source"],
            (r + (SOURCE,) for r in rows.entitlements),
        )
        db.copy_rows(
            conn,
            "assignment",
            [*ASSIGNMENT_COLUMNS, "source"],
            (r + (SOURCE,) for r in rows.assignments),
        )
        conn.execute("analyze account, entitlement, assignment")
