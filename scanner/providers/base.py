from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterable


class SportsDataProvider(ABC):
    """Contract for football data providers.

    Implementations must return provider-native dictionaries. The scanner layer
    is responsible for normalizing them into engine/domain structures.
    """

    @abstractmethod
    def fixtures_by_date(self, target_date: date) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def team_recent_fixtures(self, team_id: int | str, *, last: int = 10) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def head_to_head(self, home_team_id: int | str, away_team_id: int | str, *, last: int = 5) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def fixture_odds(self, fixture_id: int | str) -> list[dict]:
        raise NotImplementedError

    def fixture_lineups(self, fixture_id: int | str) -> list[dict]:
        return []
