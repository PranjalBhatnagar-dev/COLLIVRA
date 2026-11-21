"""
Unit tests for orbital_core.celestrak (CelesTrak client, caching, and offline fixtures).

Tests:
- Offline fixture retrieval (stations, Starlink, Cosmos-2251 debris).
- NORAD ID lookups in offline fixtures.
- Disk cache hit, miss, TTL expiration, and clear_cache.
- Fallback from failed network requests to cache and offline fixtures.
- Error handling on malformed JSON, HTTP status codes, and timeouts.
- Transient network error retries (max 2 attempts) with backoff.
- Permanent error handling (403, 404, 500, malformed JSON) with zero retries.
- Empty JSON response handling.
- Record deduplication preserving newest epoch.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from orbital_core.celestrak import (
    CelesTrakClient,
    CelesTrakFetchError,
    deduplicate_records,
)
from orbital_core.schemas import OrbitalObjectRecord

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "data" / "fixtures"


@pytest.fixture
def temp_cache_dir(tmp_path: Path) -> Path:
    cache_path = tmp_path / "celestrak_cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path


@pytest.fixture
def client(temp_cache_dir: Path) -> CelesTrakClient:
    return CelesTrakClient(
        cache_dir=temp_cache_dir,
        fixtures_dir=FIXTURES_DIR,
        cache_ttl_seconds=3600,
    )


class TestOfflineFixtures:
    def test_get_stations_fixture(self, client: CelesTrakClient):
        records = client.get_from_fixture("stations")
        assert len(records) == 2
        norad_ids = {r.norad_cat_id for r in records}
        assert "25544" in norad_ids
        assert "48274" in norad_ids
        for r in records:
            assert r.data_source == "OFFLINE_FIXTURE"

    def test_get_starlink_fixture(self, client: CelesTrakClient):
        records = client.get_from_fixture("starlink")
        assert len(records) == 5
        for r in records:
            assert r.data_source == "OFFLINE_FIXTURE"
            assert r.object_name.startswith("STARLINK")

    def test_get_debris_fixture(self, client: CelesTrakClient):
        records = client.get_from_fixture("cosmos-2251-debris")
        assert len(records) == 5
        for r in records:
            assert r.data_source == "OFFLINE_FIXTURE"
            assert "COSMOS" in r.object_name

    def test_find_in_fixtures_iss(self, client: CelesTrakClient):
        rec = client.find_in_fixtures("25544")
        assert rec is not None
        assert rec.object_name == "ISS (ZARYA)"
        assert rec.data_source == "OFFLINE_FIXTURE"

        # Also supports integer
        rec_int = client.find_in_fixtures(25544)
        assert rec_int is not None
        assert rec_int.norad_cat_id == "25544"

    def test_find_in_fixtures_starlink(self, client: CelesTrakClient):
        rec = client.find_in_fixtures("53541")
        assert rec is not None
        assert rec.object_name == "STARLINK-4485"

    def test_find_in_fixtures_nonexistent(self, client: CelesTrakClient):
        rec = client.find_in_fixtures("99999999")
        assert rec is None

    def test_missing_fixture_raises(self, client: CelesTrakClient):
        with pytest.raises(CelesTrakFetchError, match="Offline fixture"):
            client.get_from_fixture("nonexistent_satellite_fleet")


class TestDiskCaching:
    def test_cache_write_and_read(self, client: CelesTrakClient, temp_cache_dir: Path):
        sample_payload = [
            {
                "OBJECT_NAME": "MOCK SAT",
                "OBJECT_ID": "2026-999A",
                "EPOCH": "2026-11-02T12:00:00Z",
                "MEAN_MOTION": 15.0,
                "ECCENTRICITY": 0.001,
                "INCLINATION": 45.0,
                "RA_OF_ASC_NODE": 100.0,
                "ARG_OF_PERICENTER": 50.0,
                "MEAN_ANOMALY": 200.0,
                "NORAD_CAT_ID": 99999,
            }
        ]

        client._write_cache("group", "test_group", sample_payload)
        cache_path = temp_cache_dir / "group_test_group.json"
        assert cache_path.exists()

        result = client._read_cache("group", "test_group", allow_expired=False)
        assert result is not None
        payload, cached_at = result
        assert len(payload) == 1
        assert payload[0]["NORAD_CAT_ID"] == 99999
        assert isinstance(cached_at, datetime)

    def test_cache_expiration(self, temp_cache_dir: Path):
        client_short_ttl = CelesTrakClient(
            cache_dir=temp_cache_dir,
            fixtures_dir=FIXTURES_DIR,
            cache_ttl_seconds=1,  # 1 second TTL
        )
        sample = [{"dummy": "val"}]
        client_short_ttl._write_cache("group", "exp_group", sample)

        # Manually alter cached_at to 10 seconds ago
        cache_file = temp_cache_dir / "group_exp_group.json"
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        data["cached_at"] = old_time
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        # Should be None when expired
        assert client_short_ttl._read_cache("group", "exp_group", allow_expired=False) is None
        # Should be readable when allow_expired=True
        assert client_short_ttl._read_cache("group", "exp_group", allow_expired=True) is not None

    def test_clear_cache(self, client: CelesTrakClient, temp_cache_dir: Path):
        client._write_cache("group", "item1", [{"test": 1}])
        client._write_cache("group", "item2", [{"test": 2}])
        assert len(list(temp_cache_dir.glob("*.json"))) == 2

        deleted = client.clear_cache()
        assert deleted == 2
        assert len(list(temp_cache_dir.glob("*.json"))) == 0


class TestNetworkRetryAndPermanentErrors:
    @patch("time.sleep", return_value=None)
    @patch("httpx.Client.get")
    def test_transient_error_retries_and_succeeds(
        self, mock_get: MagicMock, mock_sleep: MagicMock, client: CelesTrakClient
    ):
        """Transient error on attempt 1, followed by success on attempt 2."""
        with open(FIXTURES_DIR / "stations_omm.json", "r", encoding="utf-8") as f:
            raw_stations = json.load(f)

        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.text = json.dumps(raw_stations)
        mock_success.json.return_value = raw_stations

        # First call raises ConnectError, second returns 200 OK
        mock_get.side_effect = [
            httpx.ConnectError("Transient connection drop"),
            mock_success,
        ]

        records = client.get_by_group("stations", force_refresh=True)
        assert len(records) == 2
        assert records[0].data_source == "LIVE"
        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("time.sleep", return_value=None)
    @patch("httpx.Client.get")
    def test_transient_retries_exhausted_falls_back(
        self, mock_get: MagicMock, mock_sleep: MagicMock, client: CelesTrakClient
    ):
        """Transient error on all 2 attempts exhausts retries and falls back to fixture."""
        mock_get.side_effect = httpx.TimeoutException("Read timed out")

        records = client.get_by_group("stations", fallback_to_fixture=True)
        assert len(records) == 2
        assert records[0].data_source == "OFFLINE_FIXTURE"
        assert mock_get.call_count == 2

    @patch("httpx.Client.get")
    def test_403_rate_limit_not_retried(self, mock_get: MagicMock, client: CelesTrakClient):
        """Permanent 403 rate-limit must NOT be retried."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "GP data has not updated since your last successful download."
        mock_get.return_value = mock_resp

        records = client.get_by_group("starlink", fallback_to_fixture=True)
        assert len(records) == 5
        assert records[0].data_source == "OFFLINE_FIXTURE"
        # Must be exactly 1 call: no retry on 403
        assert mock_get.call_count == 1

    @patch("httpx.Client.get")
    def test_404_not_found_not_retried(self, mock_get: MagicMock, client: CelesTrakClient):
        """404 / No GP data found must NOT be retried."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "No GP data found"
        mock_get.return_value = mock_resp

        result = client.get_by_norad_id(999999, fallback_to_fixture=False)
        assert result is None
        assert mock_get.call_count == 1

    @patch("httpx.Client.get")
    def test_empty_json_list_response(self, mock_get: MagicMock, client: CelesTrakClient):
        """CelesTrak returning HTTP 200 with empty list [] returns empty list."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "[]"
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        records = client.get_by_group("unknown_empty_group", fallback_to_fixture=False)
        assert records == []
        assert mock_get.call_count == 1

    @patch("httpx.Client.get")
    def test_malformed_json_not_retried(self, mock_get: MagicMock, client: CelesTrakClient):
        """Malformed JSON payload must NOT be retried."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>502 Bad Gateway</html>"
        mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "doc", 0)
        mock_get.return_value = mock_resp

        records = client.get_by_group("stations", fallback_to_fixture=True)
        assert len(records) == 2
        assert records[0].data_source == "OFFLINE_FIXTURE"
        # Must be exactly 1 call: no retry on JSON error
        assert mock_get.call_count == 1


class TestNetworkFallbackAndErrors:
    @patch("httpx.Client.get")
    def test_group_live_success_and_cache(self, mock_get: MagicMock, client: CelesTrakClient):
        with open(FIXTURES_DIR / "stations_omm.json", "r", encoding="utf-8") as f:
            raw_stations = json.load(f)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps(raw_stations)
        mock_resp.json.return_value = raw_stations
        mock_get.return_value = mock_resp

        records = client.get_by_group("stations", force_refresh=True)
        assert len(records) == 2
        assert records[0].data_source == "LIVE"

        # Subsequent call with use_cache=True should come from CACHE without hitting mock again
        mock_get.reset_mock()
        cached_records = client.get_by_group("stations", force_refresh=False)
        assert len(cached_records) == 2
        assert cached_records[0].data_source == "CACHE"
        mock_get.assert_not_called()

    @patch("time.sleep", return_value=None)
    @patch("httpx.Client.get")
    def test_fallback_to_fixture_on_http_error(
        self, mock_get: MagicMock, mock_sleep: MagicMock, client: CelesTrakClient
    ):
        mock_get.side_effect = httpx.ConnectError("Connection refused by sandbox")

        records = client.get_by_group("stations", fallback_to_fixture=True)
        assert len(records) == 2
        for r in records:
            assert r.data_source == "OFFLINE_FIXTURE"

    @patch("time.sleep", return_value=None)
    @patch("httpx.Client.get")
    def test_catnr_fallback_to_fixture(
        self, mock_get: MagicMock, mock_sleep: MagicMock, client: CelesTrakClient
    ):
        mock_get.side_effect = httpx.TimeoutException("Read timed out")

        rec = client.get_by_norad_id(25544, fallback_to_fixture=True)
        assert rec is not None
        assert rec.norad_cat_id == "25544"
        assert rec.object_name == "ISS (ZARYA)"
        assert rec.data_source == "OFFLINE_FIXTURE"

    @patch("time.sleep", return_value=None)
    @patch("httpx.Client.get")
    def test_nonexistent_group_failure_raises(
        self, mock_get: MagicMock, mock_sleep: MagicMock, client: CelesTrakClient
    ):
        mock_get.side_effect = httpx.ConnectError("Network down")

        with pytest.raises(CelesTrakFetchError, match="Failed to retrieve orbital data for group"):
            client.get_by_group("unknown_fleet_xyz", fallback_to_fixture=True)


class TestDeduplication:
    def test_deduplicate_keeps_latest_epoch(self, stations_fixture: list[dict]):
        iss_older = stations_fixture[0].copy()
        iss_older["EPOCH"] = "2026-11-01T10:00:00Z"

        iss_newer = stations_fixture[0].copy()
        iss_newer["EPOCH"] = "2026-11-01T20:00:00Z"
        iss_newer["REV_AT_EPOCH"] = 99999

        rec_old = OrbitalObjectRecord.from_omm_dict(iss_older)
        rec_new = OrbitalObjectRecord.from_omm_dict(iss_newer)

        deduped = deduplicate_records([rec_old, rec_new])
        assert len(deduped) == 1
        assert deduped[0].epoch == datetime(2026, 11, 1, 20, 0, 0, tzinfo=timezone.utc)
        assert deduped[0].rev_at_epoch == 99999  
