"""Canonical football team identity resolution shared by every football tool."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class TeamIdentity:
    """A stable team identity with optional provider-specific evidence."""

    canonical_id: str
    display_name: str
    normalized_name: str
    provider: str = ""
    provider_id: str = ""


class TeamIdentityResolver:
    """Resolve user/provider names to one stable team identity.

    Provider IDs are authoritative within a provider.  Names remain a safe
    fallback because some feeds (notably odds feeds) do not publish IDs.  The
    reviewed alias catalog handles translations and common club prefixes; an
    unknown name still gets a deterministic normalized identity instead of
    being silently matched to a different club.
    """

    _ALIASES: ClassVar[dict[str, str]] = {
        "阿拉维斯": "alaves",
        "deportivoalaves": "alaves",
        "alaves": "alaves",
        "赫塔费": "getafe",
        "getafe": "getafe",
        "乌德勒支": "utrecht",
        "fcutrecht": "utrecht",
        "utrecht": "utrecht",
        "精英": "excelsior",
        "sbvexcelsior": "excelsior",
        "excelsior": "excelsior",
        "威廉二世": "willemii",
        "willemii": "willemii",
        "奈梅亨": "nec",
        "nec": "nec",
        "necnijmegen": "nec",
        "阿尔克马": "az",
        "阿尔克马尔": "az",
        "azalkmaar": "az",
        "az": "az",
        "埃因霍温": "psveindhoven",
        "psveindhoven": "psveindhoven",
        "psv": "psveindhoven",
        "福图纳锡塔德": "fortunasittard",
        "fortunasittard": "fortunasittard",
        "坎布尔": "cambuur",
        "cambuur": "cambuur",
        "塞维利亚": "sevilla",
        "sevilla": "sevilla",
        "巴列卡诺": "rayovallecano",
        "rayovallecano": "rayovallecano",
        "桑坦德竞技": "racingsantander",
        "racingsantander": "racingsantander",
        "racingdesantander": "racingsantander",
        "racingclubdesantander": "racingsantander",
        "realracingclubdesantander": "racingsantander",
        "比利亚雷亚尔": "villarreal",
        "villarreal": "villarreal",
        "villarrealcf": "villarreal",
        "西班牙人": "espanyol",
        "espanyol": "espanyol",
        "rcdespanyol": "espanyol",
        "莱万特": "levante",
        "levante": "levante",
        "levanteud": "levante",
    }
    _PREFIXES: ClassVar[frozenset[str]] = frozenset(
        {
            "afc",
            "as",
            "cd",
            "cf",
            "club",
            "deportivo",
            "fc",
            "fk",
            "if",
            "ik",
            "rc",
            "rcd",
            "sc",
            "sbv",
            "sk",
            "sv",
        }
    )
    _SUFFIXES: ClassVar[frozenset[str]] = frozenset({"cf", "fc", "ud", "club"})

    def __init__(self) -> None:
        self._provider_ids: dict[tuple[str, str], str] = {}
        self._canonical_provider_ids: dict[tuple[str, str], str] = {}

    @classmethod
    def normalize_name(cls, value: str) -> str:
        text = "".join(
            char
            for char in unicodedata.normalize("NFKD", str(value or "")).casefold()
            if not unicodedata.combining(char)
        )
        tokens = re.findall(r"[\w]+", unicodedata.normalize("NFKC", text))
        while len(tokens) > 1 and tokens[0] in cls._PREFIXES:
            tokens.pop(0)
        while len(tokens) > 1 and tokens[-1] in cls._SUFFIXES:
            tokens.pop()
        return "".join(tokens)

    def resolve(
        self,
        value: str,
        *,
        provider: str = "",
        provider_id: str | int | None = None,
    ) -> TeamIdentity:
        display_name = " ".join(str(value or "").split())
        normalized = self.normalize_name(display_name)
        provider_name = str(provider or "").strip().casefold()
        provider_key = str(provider_id or "").strip()
        # Keep the name-derived identity on each object. A provider ID is
        # authoritative only when both sides of a comparison carry that same
        # provider ID; it must not turn an unrelated display-name change into
        # a match merely because a previous request used the same feed ID.
        canonical_id = self._ALIASES.get(normalized, normalized)
        if provider_key:
            self._provider_ids[(provider_name, provider_key)] = canonical_id
            self._canonical_provider_ids[(provider_name, canonical_id)] = provider_key
        return TeamIdentity(
            canonical_id=canonical_id,
            display_name=display_name,
            normalized_name=normalized,
            provider=provider_name,
            provider_id=provider_key,
        )

    def provider_id(self, canonical_id: str, *, provider: str) -> str:
        return self._canonical_provider_ids.get((provider.casefold(), canonical_id), "")

    def equivalent(self, left: TeamIdentity, right: TeamIdentity) -> bool:
        if left.provider and right.provider and left.provider == right.provider:
            if left.provider_id and right.provider_id and left.provider_id == right.provider_id:
                return True
        return bool(left.canonical_id) and left.canonical_id == right.canonical_id


DEFAULT_TEAM_IDENTITY_RESOLVER = TeamIdentityResolver()


def resolve_team_identity(
    value: str,
    *,
    provider: str = "",
    provider_id: str | int | None = None,
) -> TeamIdentity:
    return DEFAULT_TEAM_IDENTITY_RESOLVER.resolve(
        value, provider=provider, provider_id=provider_id
    )


def football_team_identity(value: str) -> str:
    return resolve_team_identity(value).canonical_id


def football_teams_match(
    left: str,
    right: str,
    *,
    left_provider: str = "",
    left_provider_id: str | int | None = None,
    right_provider: str = "",
    right_provider_id: str | int | None = None,
) -> bool:
    return DEFAULT_TEAM_IDENTITY_RESOLVER.equivalent(
        resolve_team_identity(left, provider=left_provider, provider_id=left_provider_id),
        resolve_team_identity(right, provider=right_provider, provider_id=right_provider_id),
    )
