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

    def public_mapping(self) -> dict[str, str]:
        return {
            "key": self.key,
            "competition": self.name,
            "country": self.country,
            "espn_slug": self.espn_slug,
        }


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
        ("Champions League", "UCL", "欧冠", "欧洲冠军联赛"),
    ),
    FootballCompetition(
        "uefa-europa-league",
        "UEFA Europa League",
        "Europe",
        "uefa.europa",
        "soccer_uefa_europa_league",
        ("Europa League", "UEL", "欧联", "欧联杯", "欧洲联赛"),
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
        ("La Liga", "LALIGA", "西甲", "西班牙甲级联赛"),
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
        ("Eredivisie", "荷甲", "荷兰甲级联赛"),
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


def normalize_competition_name(value: str) -> str:
    """Normalize a human competition label without interpreting provider IDs."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized)


def resolve_football_competition(value: str, *, country: str = "") -> FootballCompetition | None:
    """Resolve a reviewed alias; never treat an arbitrary provider slug as trusted."""

    needle = normalize_competition_name(value)
    trusted_country = _COUNTRY_ALIASES.get(country.strip(), country)
    wanted_country = normalize_competition_name(trusted_country)
    if not needle:
        return None
    matches = []
    for competition in FOOTBALL_COMPETITIONS:
        labels = (competition.key, competition.name, *competition.aliases)
        if needle not in {normalize_competition_name(label) for label in labels}:
            continue
        if wanted_country and wanted_country != normalize_competition_name(competition.country):
            continue
        matches.append(competition)
    return matches[0] if len(matches) == 1 else None
