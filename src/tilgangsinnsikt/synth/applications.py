"""Fictional applications, their entitlements and the role matrix behind base assignments."""

from dataclasses import dataclass

APPLICATIONS = [
    ("ytelse", "Ytelse", "ytelse"),
    ("oppfolging", "Oppfølging", "oppfolging"),
    ("arkiv", "Arkiv", "arkiv"),
    ("okonomi", "Økonomi", "okonomi"),
    ("personregister", "Personregister", "plattform"),
    ("kontaktsenter", "Kontaktsenter", "oppfolging"),
    ("hrportal", "HR-portal", "hr"),
    ("rapport", "Rapport", "plattform"),
    ("entra", "Entra ID", "plattform"),
    ("identadmin", "Identadministrasjon", "plattform"),
]

APP_DOMAIN = {app_id: domain for app_id, _, domain in APPLICATIONS}

ENTITLEMENTS = [
    ("ytelse:saksbehandler-sykepenger", "low", "Behandle saker om sykepenger."),
    ("ytelse:saksbehandler-dagpenger", "low", "Behandle saker om dagpenger."),
    ("ytelse:utbetaling-registrere", "medium", "Registrere utbetalinger."),
    ("ytelse:utbetaling-godkjenne", "high", "Godkjenne utbetalinger."),
    ("ytelse:les-alle-regioner", "high", "Lese saker i alle regioner."),
    ("oppfolging:veileder", "low", "Følge opp brukere i egen enhet."),
    ("oppfolging:veileder-utvidet", "medium", "Følge opp brukere på tvers av enheter."),
    ("oppfolging:tiltak-godkjenne", "high", "Godkjenne tiltak."),
    ("arkiv:les-egen-region", "low", "Lese arkivet for egen region."),
    ("arkiv:les-alle", "high", "Lese hele arkivet."),
    ("arkiv:slett", "high", "Slette dokumenter i arkivet."),
    ("okonomi:bilag-registrere", "medium", "Registrere bilag."),
    ("okonomi:bilag-attestere", "high", "Attestere bilag."),
    ("okonomi:leverandor-endre", "high", "Endre leverandøropplysninger."),
    ("personregister:les", "low", "Slå opp personer i personregisteret."),
    ("personregister:les-skjermet", "high", "Lese skjermede personopplysninger."),
    ("personregister:endre", "high", "Endre opplysninger i personregisteret."),
    ("kontaktsenter:les-begrenset", "low", "Se begrenset saksinformasjon."),
    ("kontaktsenter:les-utvidet", "medium", "Se utvidet saksinformasjon."),
    ("hrportal:ansatt", "low", "Selvbetjening for ansatte."),
    ("hrportal:leder", "medium", "Lederfunksjoner for egne ansatte."),
    ("hrportal:hr-admin", "high", "Endre ansettelsesforhold for alle ansatte."),
    ("rapport:leser", "low", "Lese rapporter."),
    ("rapport:bygger", "low", "Lage og dele rapporter."),
    ("rapport:admin", "medium", "Administrere rapportløsningen."),
    ("entra:GA-admins", "high", "Global administrator i Entra ID."),
    ("entra:Utviklere", "medium", "Utviklergruppe med tilgang til utviklingsmiljøer."),
    ("identadmin:identadmin-lokal", "medium", "Administrere identiteter i egen enhet."),
    ("identadmin:identadmin-sentral", "high", "Opprette og endre alle identiteter."),
]

RISK = {entitlement_id: risk for entitlement_id, risk, _ in ENTITLEMENTS}

UNIT_GROUP_PREFIX = "entra:enhet-"
UNIT_GROUP_DESCRIPTION = "Dynamisk gruppe: medlemskap styres av enhet i HR-systemet."

EXEMPT_ALL_REGIONS = "ytelse:les-alle-regioner"


@dataclass(frozen=True)
class Grant:
    """Disjoint options: each member of a peer group gets at most one of these.

    A share of None means a baseline entitlement held by 90-98 % of the group.
    """

    shares: dict[str, float | None]
    requires: str | None = None


def base(*entitlement_ids: str) -> list[Grant]:
    return [Grant({e: None}) for e in entitlement_ids]


def opt(entitlement_id: str, share: float, requires: str | None = None) -> Grant:
    return Grant({entitlement_id: share}, requires)


COMMON = [Grant({"hrportal:ansatt": 1.0})]

# Optional shares stay at 14 % or more so that only planted assignments are rare (< 10 %)
# in a peer group. SoD pairs are disjoint options of one Grant: the baseline has no
# violations, and planting one never needs an entitlement that is rare in the group.
ROLE_MATRIX: dict[tuple[str, str], list[Grant]] = {
    ("veileder", "lokalkontor"): [
        *base("oppfolging:veileder", "personregister:les", "arkiv:les-egen-region"),
        opt("oppfolging:veileder-utvidet", 0.35),
        opt("ytelse:saksbehandler-dagpenger", 0.20),
        opt("rapport:leser", 0.50),
    ],
    ("saksbehandler", "regionkontor"): [
        *base("personregister:les", "arkiv:les-egen-region"),
        Grant({"ytelse:saksbehandler-sykepenger": 0.5, "ytelse:saksbehandler-dagpenger": 0.5}),
        Grant({"ytelse:utbetaling-registrere": 0.82, "ytelse:utbetaling-godkjenne": 0.14}),
        opt("ytelse:les-alle-regioner", 0.15),
        opt("rapport:leser", 0.40),
    ],
    ("saksbehandler", "lokalkontor"): [
        *base("personregister:les", "arkiv:les-egen-region"),
        Grant({"ytelse:saksbehandler-sykepenger": 0.5, "ytelse:saksbehandler-dagpenger": 0.5}),
        Grant({"ytelse:utbetaling-registrere": 0.82, "ytelse:utbetaling-godkjenne": 0.14}),
        opt("oppfolging:veileder", 0.30),
    ],
    ("rådgiver", "direktorat"): [
        *base("rapport:leser", "personregister:les"),
        opt("ytelse:les-alle-regioner", 0.50),
        opt("arkiv:les-alle", 0.30),
        opt("rapport:bygger", 0.60),
    ],
    ("rådgiver", "regionkontor"): [
        *base("rapport:leser", "personregister:les", "arkiv:les-egen-region"),
        opt("ytelse:saksbehandler-sykepenger", 0.30),
        opt("rapport:bygger", 0.40),
    ],
    ("rådgiver", "lokalkontor"): [
        *base("oppfolging:veileder", "rapport:leser", "personregister:les"),
        opt("oppfolging:veileder-utvidet", 0.50),
    ],
    ("leder", "lokalkontor"): [
        *base("hrportal:leder", "rapport:leser", "oppfolging:veileder", "personregister:les"),
        opt("oppfolging:tiltak-godkjenne", 0.60),
        opt("identadmin:identadmin-lokal", 0.40),
    ],
    ("leder", "regionkontor"): [
        *base("hrportal:leder", "rapport:leser", "personregister:les"),
        opt("rapport:bygger", 0.30),
        opt("identadmin:identadmin-lokal", 0.40),
    ],
    ("leder", "direktorat"): [
        *base("hrportal:leder", "rapport:leser"),
        opt("rapport:bygger", 0.40),
    ],
    ("leder", "kontaktsenter"): [
        *base("hrportal:leder", "rapport:leser", "kontaktsenter:les-utvidet"),
    ],
    ("it-konsulent", "direktorat"): [
        *base("rapport:leser"),
        Grant({"entra:Utviklere": 0.55, "personregister:les-skjermet": 0.18}),
        opt("rapport:admin", 0.20),
        opt("personregister:les", 0.50),
    ],
    ("økonomikonsulent", "direktorat"): [
        *base("rapport:leser"),
        Grant({"okonomi:bilag-registrere": 0.75, "okonomi:bilag-attestere": 0.22}),
        opt("okonomi:leverandor-endre", 0.24, requires="okonomi:bilag-registrere"),
    ],
    ("hr-konsulent", "direktorat"): [
        *base("rapport:leser"),
        Grant({"hrportal:hr-admin": 0.35, "identadmin:identadmin-sentral": 0.25}),
    ],
    ("kundeveileder", "kontaktsenter"): [
        *base("kontaktsenter:les-begrenset", "personregister:les"),
        opt("kontaktsenter:les-utvidet", 0.30),
    ],
}

LOOKUP_APPS = {
    "saksbehandler": ["ytelse"],
    "veileder": ["oppfolging"],
    "kundeveileder": ["kontaktsenter"],
    "rådgiver": ["ytelse", "oppfolging"],
}

LOOKUP_RATE_FACTOR = {"kundeveileder": 3.0, "saksbehandler": 1.2, "veileder": 1.0, "rådgiver": 0.3}
