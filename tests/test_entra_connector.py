from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest
from conftest import AS_OF
from typer.testing import CliRunner

from tilgangsinnsikt import db
from tilgangsinnsikt.analyses import sod
from tilgangsinnsikt.cli import app
from tilgangsinnsikt.config import RULES_DIR, EntraSettings
from tilgangsinnsikt.connectors import entra

TOKEN = "test-token"
U1, U2, U3 = (f"00000000-0000-0000-0000-00000000000{n}" for n in (1, 2, 3))
G1, G2, G3 = (f"00000000-0000-0000-0000-0000000000a{n}" for n in (1, 2, 3))
DEVICE, NESTED = "00000000-0000-0000-0000-0000000000d1", "00000000-0000-0000-0000-0000000000b1"
USERS = [
    {
        "id": U1,
        "userPrincipalName": "p000001@dyo.example",
        "displayName": "Testperson En",
        "accountEnabled": True,
        "employeeId": "P000001",
        "signInActivity": {
            "lastSignInDateTime": "2026-05-01T08:00:00Z",
            "lastNonInteractiveSignInDateTime": "2026-05-20T08:00:00Z",
        },
    },
    {
        "id": U2,
        "userPrincipalName": "p000002@dyo.example",
        "displayName": "Testperson To",
        "accountEnabled": False,
        "employeeId": None,
        "signInActivity": None,
    },
    {
        "id": U3,
        "userPrincipalName": "konsulent-01@dyo.example",
        "displayName": "Testkonsulent",
        "accountEnabled": True,
    },
]
GROUPS = [
    {"id": G1, "displayName": "GA-admins", "description": None, "securityEnabled": True},
    {"id": G2, "displayName": "Utviklere-prod", "description": "Test", "securityEnabled": True},
    {"id": G3, "displayName": "Alle ansatte", "description": None, "securityEnabled": True},
]
USER_TYPE, DEVICE_TYPE, GROUP_TYPE = (
    "#microsoft.graph.user",
    "#microsoft.graph.device",
    "#microsoft.graph.group",
)
MEMBERS = {
    G1: [(U1, USER_TYPE)],
    G2: [(U1, USER_TYPE), (DEVICE, DEVICE_TYPE)],
    G3: [(U1, USER_TYPE), (U2, USER_TYPE), (U3, USER_TYPE), (NESTED, GROUP_TYPE)],
}
SECRET_VALUES = [
    TOKEN,
    *[u["userPrincipalName"] for u in USERS],
    *[u["displayName"] for u in USERS],
]
SECRET_VALUES += [g["displayName"] for g in GROUPS] + [U1, U2, U3, G1, G2, G3]


class FakeGraph:
    def __init__(self, premium: bool = True, next_link_host: str = entra.GRAPH) -> None:
        self.premium = premium
        self.next_link_host = next_link_host
        self.throttled = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        path = request.url.path.removeprefix("/v1.0")
        if path == "/users":
            if "signInActivity" in request.url.params.get("$select", "") and not self.premium:
                return httpx.Response(403, json={"error": {"code": "Authentication_Failed"}})
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json={"value": USERS[2:]})
            link = f"{self.next_link_host}/users?page=2"
            return httpx.Response(200, json={"value": USERS[:2], "@odata.nextLink": link})
        if path == "/groups":
            if not self.throttled:
                self.throttled = True
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"value": GROUPS})
        if path.startswith("/groups/") and path.endswith("/members"):
            group = path.split("/")[2]
            value = [{"@odata.type": t, "id": i} for i, t in MEMBERS[group]]
            return httpx.Response(200, json={"value": value})
        return httpx.Response(404)


def graph_client(fake: FakeGraph, sleeps: list[float] | None = None) -> entra.GraphClient:
    sink = sleeps if sleeps is not None else []
    return entra.GraphClient(lambda: TOKEN, httpx.MockTransport(fake), sleep=sink.append)


@pytest.fixture(autouse=True)
def no_real_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(settings: EntraSettings):
        raise AssertionError("tests must not sign in to a real tenant")

    monkeypatch.setattr(entra, "msal_token", refuse)


@pytest.fixture
def entra_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Running from an empty directory keeps the developer's .env out of the test.
    monkeypatch.chdir(tmp_path)
    for name in ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "IDENTITY_MAP", "SOD_GROUPS"):
        monkeypatch.delenv(f"ENTRA_{name}", raising=False)
    monkeypatch.setenv("ENTRA_TENANT_ID", "<tenant-id>")
    monkeypatch.setenv("ENTRA_CLIENT_ID", "<client-id>")
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "<client-secret>")


def patterns() -> entra.RiskPatterns:
    return entra.load_risk_patterns(RULES_DIR / "entra_privileged_groups.yaml")


def test_fetch_follows_paging_and_retries_throttling() -> None:
    fake, sleeps = FakeGraph(), []
    snapshot = entra.fetch(graph_client(fake, sleeps))
    assert [u["id"] for u in snapshot.users] == [U1, U2, U3]
    assert [g["id"] for g in snapshot.groups] == [G1, G2, G3]
    assert snapshot.members == {G1: [U1], G2: [U1], G3: [U1, U2, U3]}
    assert snapshot.skipped_members == 2
    assert snapshot.sign_in_activity
    assert sleeps == [1.0]
    assert all(r.method == "GET" for r in fake.requests)
    assert all(r.url.host == "graph.microsoft.com" for r in fake.requests)


def test_missing_p1_licence_falls_back_to_users_without_sign_in_activity() -> None:
    snapshot = entra.fetch(graph_client(FakeGraph(premium=False)))
    assert not snapshot.sign_in_activity
    assert len(snapshot.users) == 3


def test_next_link_outside_graph_is_refused() -> None:
    fake = FakeGraph(next_link_host="https://example.invalid/v1.0")
    with pytest.raises(entra.EntraError, match="next link outside"):
        entra.fetch(graph_client(fake))
    assert all(r.url.host == "graph.microsoft.com" for r in fake.requests)


def test_graph_errors_carry_status_and_code_only() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        body = {"error": {"code": "Authorization_RequestDenied", "message": f"object {U1}"}}
        return httpx.Response(403, json=body)

    client = entra.GraphClient(lambda: TOKEN, httpx.MockTransport(denied), sleep=lambda s: None)
    with pytest.raises(entra.GraphError) as error:
        list(client.pages("/groups", {}, "groups"))
    assert str(error.value) == (
        "Graph request for groups failed with status 403 (Authorization_RequestDenied)"
    )


def test_token_error_keeps_codes_only() -> None:
    result = {
        "error": "invalid_client",
        "error_description": f"AADSTS7000215: Invalid client secret for app {U1}. Trace ID: {U2}",
    }
    assert entra.token_error(result) == "invalid_client AADSTS7000215"


def test_require_names_missing_settings_without_values(entra_env: None, monkeypatch) -> None:
    monkeypatch.delenv("ENTRA_CLIENT_ID")
    monkeypatch.delenv("ENTRA_CLIENT_SECRET")
    with pytest.raises(entra.EntraError) as error:
        entra.require(EntraSettings())
    assert "ENTRA_CLIENT_ID" in str(error.value) and "ENTRA_CLIENT_SECRET" in str(error.value)
    assert "ENTRA_TENANT_ID" not in str(error.value)
    assert "<tenant-id>" not in str(error.value)


def test_map_links_identities_and_sets_risk(tmp_path: Path) -> None:
    mapping = tmp_path / "map.csv"
    mapping.write_text("upn,identity_id\nP000002@dyo.example,P000002\n", encoding="utf-8")
    snapshot = entra.fetch(graph_client(FakeGraph()))
    rows = entra.map_snapshot(
        snapshot, {"P000001", "P000002"}, entra.load_identity_map(mapping), patterns()
    )
    accounts = {a[0]: a for a in rows.accounts}
    assert accounts[U1][1:5] == ("P000001", "entra", "p000001@dyo.example", True)
    assert accounts[U1][5] == datetime(2026, 5, 20, 8, tzinfo=UTC)
    assert accounts[U2][1] == "P000002" and accounts[U2][4] is False and accounts[U2][5] is None
    assert accounts[U3][1] is None
    assert {e[0]: e[4] for e in rows.entitlements} == {G1: "high", G2: "medium", G3: "low"}
    assert len(rows.assignments) == 5
    assert all(a[2] is None and a[3] is None for a in rows.assignments)
    assert rows.counts["linked by employeeId"] == 1
    assert rows.counts["linked by mapping file"] == 1
    assert rows.counts["not linked (orphan)"] == 1
    assert rows.counts["skipped non-user members"] == 2


def entra_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {
        t: conn.execute(f"select count(*) from {t} where source = 'entra'").fetchone()[0]
        for t in ("account", "entitlement", "assignment")
    }


def other_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {
        t: conn.execute(f"select count(*) from {t}").fetchone()[0]
        for t in db.MODEL_TABLES
        if t not in ("account", "entitlement", "assignment")
    } | {
        t: conn.execute(f"select count(*) from {t} where source <> 'entra'").fetchone()[0]
        for t in ("account", "entitlement", "assignment")
    }


def test_write_replaces_only_entra_rows(loaded: psycopg.Connection) -> None:
    before = other_counts(loaded)
    snapshot = entra.fetch(graph_client(FakeGraph()))
    entra.write(loaded, entra.map_snapshot(snapshot, {"P000001"}, {}, patterns()))
    assert entra_counts(loaded) == {"account": 3, "entitlement": 3, "assignment": 5}
    assert other_counts(loaded) == before

    snapshot.users = snapshot.users[:1]
    entra.write(loaded, entra.map_snapshot(snapshot, {"P000001"}, {}, patterns()))
    assert entra_counts(loaded) == {"account": 1, "entitlement": 3, "assignment": 3}
    assert other_counts(loaded) == before
    linked = "select identity_id from account where account_id = %s"
    assert loaded.execute(linked, (U1,)).fetchone()[0] == "P000001"


def test_sod_matches_an_entra_group_by_configured_name(loaded: psycopg.Connection) -> None:
    person = loaded.execute(
        """
        select a.identity_id from assignment s join account a using (account_id)
        join identity i using (identity_id)
        where s.entitlement_id = 'personregister:les-skjermet' and i.status = 'active'
          and a.identity_id not in (
              select a2.identity_id from assignment s2 join account a2 using (account_id)
              where s2.entitlement_id = 'entra:Utviklere')
        order by 1 limit 1
        """
    ).fetchone()[0]
    snapshot = entra.fetch(graph_client(FakeGraph()))
    snapshot.users[0] = snapshot.users[0] | {"employeeId": person}
    entra.write(loaded, entra.map_snapshot(snapshot, {person}, {}, patterns()))

    unmapped = sod.run(loaded, "r1", AS_OF, group_names={})
    assert person not in {f.identity_id for f in unmapped}

    mapped = sod.run(loaded, "r2", AS_OF, group_names={"entra:Utviklere": "utviklere-PROD"})
    [finding] = [f for f in mapped if f.identity_id == person]
    assert finding.evidence["rule"] == "utvikler-og-produksjonsdata"
    assert finding.evidence["left"] == [G2]
    assert finding.evidence["since"] is None
    assert "siden" not in finding.explanation_nb


def invoke(*args: str):
    return CliRunner().invoke(app, list(args), env={"COLUMNS": "200"})


def test_connect_entra_dry_run_prints_counts_only(
    entra_env: None, cli_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entra, "client", lambda settings: graph_client(FakeGraph()))
    result = invoke("connect-entra", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Dry run: nothing was written." in result.output
    assert "memberships" in result.output
    for value in [*SECRET_VALUES, "<tenant-id>", "<client-id>", "<client-secret>"]:
        assert value not in result.output


def test_connect_entra_writes_entra_rows(
    entra_env: None, cli_schema: str, small_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("load", "--data", str(small_dataset)).exit_code == 0
    monkeypatch.setattr(entra, "client", lambda settings: graph_client(FakeGraph()))
    result = invoke("connect-entra")
    assert result.exit_code == 0, result.output
    assert "rows from other sources are unchanged" in result.output
    for value in SECRET_VALUES:
        assert value not in result.output
    with db.connect() as conn:
        assert entra_counts(conn) == {"account": 3, "entitlement": 3, "assignment": 5}


def test_connect_entra_stops_when_settings_are_missing(
    entra_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENTRA_CLIENT_SECRET")
    result = invoke("connect-entra", "--dry-run")
    assert result.exit_code == 1
    assert "ENTRA_CLIENT_SECRET" in result.output
