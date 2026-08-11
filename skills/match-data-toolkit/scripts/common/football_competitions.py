"""Reviewed football provider identities shared by bundled Skill scripts."""

from __future__ import annotations

import re
import unicodedata

COMPETITIONS = (
    ("FIFA World Cup", "Worldwide", "fifa.world", "soccer_fifa_world_cup", ("World Cup", "世界杯", "FIFA世界杯")),
    ("UEFA Champions League", "Europe", "uefa.champions", "soccer_uefa_champs_league", ("Champions League", "UCL", "欧冠")),
    ("UEFA Europa League", "Europe", "uefa.europa", "soccer_uefa_europa_league", ("Europa League", "UEL", "欧联", "欧联杯")),
    ("English Premier League", "England", "eng.1", "soccer_epl", ("Premier League", "EPL", "英超")),
    ("Spanish LALIGA", "Spain", "esp.1", "soccer_spain_la_liga", ("La Liga", "LALIGA", "西甲")),
    ("German Bundesliga", "Germany", "ger.1", "soccer_germany_bundesliga", ("Bundesliga", "德甲")),
    ("Italian Serie A", "Italy", "ita.1", "soccer_italy_serie_a", ("Serie A", "意甲")),
    ("French Ligue 1", "France", "fra.1", "soccer_france_ligue_one", ("Ligue 1", "法甲")),
    ("Dutch Eredivisie", "Netherlands", "ned.1", "soccer_netherlands_eredivisie", ("Eredivisie", "荷甲")),
    ("Portuguese Primeira Liga", "Portugal", "por.1", "soccer_portugal_primeira_liga", ("Primeira Liga", "Liga Portugal", "葡超")),
    ("Swedish Allsvenskan", "Sweden", "swe.1", "soccer_sweden_allsvenskan", ("Allsvenskan", "瑞典超", "瑞典超级联赛")),
    ("Norwegian Eliteserien", "Norway", "nor.1", "soccer_norway_eliteserien", ("Eliteserien", "挪超", "挪威超级联赛")),
    ("Major League Soccer", "United States", "usa.1", "soccer_usa_mls", ("MLS", "美职联")),
)


def normalize(value: str) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", value).casefold())


def resolve_competition(value: str) -> dict[str, str] | None:
    needle = normalize(value)
    matches = []
    for name, country, espn_slug, odds_sport_key, aliases in COMPETITIONS:
        labels = (name, *aliases)
        if needle in {normalize(label) for label in labels}:
            matches.append(
                {
                    "competition": name,
                    "country": country,
                    "espn_slug": espn_slug,
                    "odds_sport_key": odds_sport_key,
                }
            )
    return matches[0] if len(matches) == 1 else None
