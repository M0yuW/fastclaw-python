"""Trusted football competition aliases and provider identifiers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FootballCompetition:
    """A product-facing competition with reviewed provider identities."""

    key: str
    name: str
    country: str
    espn_slug: str
    odds_sport_key: str
    aliases: tuple[str, ...]
    provider_country: str | None = None
    # Some UEFA competitions use a separate ESPN league for qualifying rounds.
    # Keep it runtime-owned so models cannot substitute arbitrary provider slugs.
    espn_qualifying_slug: str = ""
    # Reviewed alternate Odds API competitions, such as a separately routed
    # qualifying tournament. The primary key remains first for stable callers.
    odds_alternate_sport_keys: tuple[str, ...] = ()
    # Reviewed provider league IDs used only for prior-division context. They
    # are runtime-owned and never exposed as model-supplied identifiers.
    historical_league_ids: tuple[tuple[str, str], ...] = ()

    @property
    def odds_sport_keys(self) -> tuple[str, ...]:
        return (self.odds_sport_key, *self.odds_alternate_sport_keys)

    def public_mapping(self) -> dict[str, str]:
        mapping = {
            "key": self.key,
            "competition": self.name,
            "country": self.country,
            "espn_slug": self.espn_slug,
        }
        if self.espn_qualifying_slug:
            mapping["espn_qualifying_slug"] = self.espn_qualifying_slug
        return mapping


# ESPN slugs were mechanically checked against the public soccer scoreboard endpoint on
# 2026-08-10. Keep this list deliberately small: an unknown competition must fail closed
# until its provider identifier has been verified and reviewed.
FOOTBALL_COMPETITIONS: tuple[FootballCompetition, ...] = (
    FootballCompetition(
        "fifa-world-cup",
        "FIFA World Cup",
        "Worldwide",
        "fifa.world",
        "soccer_fifa_world_cup",
        ("World Cup", "世界杯", "世界杯足球赛", "FIFA世界杯"),
    ),
    FootballCompetition(
        "uefa-champions-league",
        "UEFA Champions League",
        "Europe",
        "uefa.champions",
        "soccer_uefa_champs_league",
        (
            "Champions League",
            "UCL",
            "UEFA Champions League Qualification",
            "UEFA Champions League Qualifying",
            "Champions League Qualification",
            "Champions League Qualifying",
            "UCL Qualification",
            "UCL Qualifying",
            "欧冠",
            "欧洲冠军联赛",
            "欧冠资格赛",
            "欧冠附加赛",
            "欧洲冠军联赛资格赛",
            "欧洲冠军联赛附加赛",
        ),
        espn_qualifying_slug="uefa.champions_qual",
        odds_alternate_sport_keys=("soccer_uefa_champs_league_qualification",),
    ),
    FootballCompetition(
        "uefa-europa-league",
        "UEFA Europa League",
        "Europe",
        "uefa.europa",
        "soccer_uefa_europa_league",
        (
            "Europa League",
            "UEL",
            "UEFA Europa League Qualification",
            "UEFA Europa League Qualifying",
            "Europa League Qualification",
            "Europa League Qualifying",
            "UEL Qualification",
            "UEL Qualifying",
            "欧罗巴",
            "欧联",
            "欧联杯",
            "欧洲联赛",
            "欧罗巴资格赛",
            "欧罗巴附加赛",
            "欧联资格赛",
            "欧联附加赛",
        ),
        espn_qualifying_slug="uefa.europa_qual",
    ),
    FootballCompetition(
        "english-premier-league",
        "English Premier League",
        "England",
        "eng.1",
        "soccer_epl",
        ("Premier League", "EPL", "英超", "英格兰超级联赛"),
    ),
    FootballCompetition(
        "spanish-laliga",
        "Spanish LALIGA",
        "Spain",
        "esp.1",
        "soccer_spain_la_liga",
        (
            "La Liga",
            "LALIGA",
            "西甲",
            "西甲 La Liga",
            "西班牙甲级联赛",
            "西班牙 La Liga",
        ),
        historical_league_ids=(("Spanish La Liga 2", "4400"),),
    ),
    FootballCompetition(
        "german-bundesliga",
        "German Bundesliga",
        "Germany",
        "ger.1",
        "soccer_germany_bundesliga",
        ("Bundesliga", "德甲", "德国甲级联赛"),
    ),
    FootballCompetition(
        "italian-serie-a",
        "Italian Serie A",
        "Italy",
        "ita.1",
        "soccer_italy_serie_a",
        ("Serie A", "意甲", "意大利甲级联赛"),
    ),
    FootballCompetition(
        "french-ligue-1",
        "French Ligue 1",
        "France",
        "fra.1",
        "soccer_france_ligue_one",
        ("Ligue 1", "法甲", "法国甲级联赛"),
    ),
    FootballCompetition(
        "dutch-eredivisie",
        "Dutch Eredivisie",
        "Netherlands",
        "ned.1",
        "soccer_netherlands_eredivisie",
        ("Eredivisie", "荷甲", "荷甲 Eredivisie", "荷兰甲级联赛"),
        "The Netherlands",
    ),
    FootballCompetition(
        "portuguese-primeira-liga",
        "Portuguese Primeira Liga",
        "Portugal",
        "por.1",
        "soccer_portugal_primeira_liga",
        ("Primeira Liga", "Liga Portugal", "葡超", "葡萄牙超级联赛"),
    ),
    FootballCompetition(
        "swedish-allsvenskan",
        "Swedish Allsvenskan",
        "Sweden",
        "swe.1",
        "soccer_sweden_allsvenskan",
        ("Allsvenskan", "瑞典超", "瑞典超级联赛"),
    ),
    FootballCompetition(
        "norwegian-eliteserien",
        "Norwegian Eliteserien",
        "Norway",
        "nor.1",
        "soccer_norway_eliteserien",
        ("Eliteserien", "挪超", "挪威超级联赛"),
    ),
    FootballCompetition(
        "major-league-soccer",
        "Major League Soccer",
        "United States",
        "usa.1",
        "soccer_usa_mls",
        ("MLS", "美职联", "美国职业足球大联盟"),
    ),
)

_COUNTRY_ALIASES = {
    "全球": "Worldwide",
    "世界": "Worldwide",
    "欧洲": "Europe",
    "英格兰": "England",
    "西班牙": "Spain",
    "德国": "Germany",
    "意大利": "Italy",
    "法国": "France",
    "荷兰": "Netherlands",
    "葡萄牙": "Portugal",
    "瑞典": "Sweden",
    "挪威": "Norway",
    "美国": "United States",
}

_COMPETITION_DISPLAY_NAMES = {
    "fifa-world-cup": "世界杯",
    "uefa-champions-league": "欧冠",
    "uefa-europa-league": "欧罗巴",
    "english-premier-league": "英超",
    "spanish-laliga": "西甲",
    "german-bundesliga": "德甲",
    "italian-serie-a": "意甲",
    "french-ligue-1": "法甲",
    "dutch-eredivisie": "荷甲",
    "portuguese-primeira-liga": "葡超",
    "swedish-allsvenskan": "瑞典超",
    "norwegian-eliteserien": "挪超",
    "major-league-soccer": "美职联",
}


def normalize_competition_name(value: str) -> str:
    """Normalize a human competition label without interpreting provider IDs."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized)


def canonical_competition_identity(value: str, *, country: str = "") -> str:
    """Return the stable identity used by caches, ledgers, and provider joins.

    Human-facing names are deliberately not used as keys.  A reviewed alias such
    as ``西甲`` or ``La Liga`` resolves to the same competition key; unknown
    values still get a deterministic normalized fallback so old ledgers remain
    readable without silently being assigned to a different competition.
    """

    resolved = resolve_football_competition(value, country=country)
    # Keep the provider-independent key compact because this value is also
    # embedded in legacy cache keys (for example ``spanishlaliga|...``).
    return (
        normalize_competition_name(resolved.key)
        if resolved is not None
        else normalize_competition_name(value)
    )


def canonical_competition_display(value: str) -> str:
    """Return one stable Chinese label for customer-facing ledger output."""

    text = " ".join(str(value or "").split())
    resolved = resolve_football_competition(text)
    if resolved is None:
        return text
    return _COMPETITION_DISPLAY_NAMES.get(resolved.key, resolved.name)


def resolve_football_competition(value: str, *, country: str = "") -> FootballCompetition | None:
    """Resolve a reviewed alias, using country only to disambiguate duplicate labels.

    Models sometimes attach a participating club's country to an international
    competition. A unique reviewed competition label is authoritative and must
    not be rejected by that advisory hint.
    """

    needle = normalize_competition_name(value)
    trusted_country = _COUNTRY_ALIASES.get(country.strip(), country)
    wanted_country = normalize_competition_name(trusted_country)
    if not needle:
        return None
    matches: list[FootballCompetition] = []
    for competition in FOOTBALL_COMPETITIONS:
        labels = (competition.key, competition.name, *competition.aliases)
        if needle not in {normalize_competition_name(label) for label in labels}:
            continue
        matches.append(competition)
    if not matches:
        # Coordinators and historical ledgers often retain a bilingual display
        # label such as "UEFA Europa League 欧联资格赛". It is not an exact alias,
        # but it contains reviewed aliases that uniquely identify one catalog
        # competition. Only accept a unique embedded match; ambiguous composite
        # labels still fail closed.
        for competition in FOOTBALL_COMPETITIONS:
            labels = (competition.key, competition.name, *competition.aliases)
            normalized_labels = {
                normalize_competition_name(label)
                for label in labels
                if len(normalize_competition_name(label)) >= 4
            }
            if any(label in needle for label in normalized_labels):
                matches.append(competition)
    if wanted_country:
        country_matches = [
            competition
            for competition in matches
            if wanted_country == normalize_competition_name(competition.country)
        ]
        if len(country_matches) == 1:
            return country_matches[0]
    return matches[0] if len(matches) == 1 else None
