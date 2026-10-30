import pytest

from zoomollo import jobboards, pipeline
from zoomollo.db import DB

SAMPLES = {
    "boards-api.greenhouse.io": {"jobs": [{"id": 1, "title": "AE", "location": {"name": "NYC"},
        "absolute_url": "https://gh/1", "updated_at": "2026-10-01",
        "content": "&lt;p&gt;Questions? recruiter.jane@acme.com or 415-555-0134&lt;/p&gt;"}]},
    "api.lever.co": [{"id": "abc", "text": "SDR", "categories": {"location": "Remote"},
        "hostedUrl": "https://lever/abc", "createdAt": 1, "descriptionPlain": "Email talent@acme.com",
        "lists": [{"text": "Reqs", "content": "<li>x</li>"}], "additionalPlain": ""}],
    "api.ashbyhq.com": {"jobs": [{"id": "z1", "title": "Eng", "location": "SF", "jobUrl": "https://ashby/z1",
        "publishedAt": "2026-09-01", "descriptionHtml": "<p>Contact <a href='mailto:sam.lee@acme.com'>Sam Lee</a></p>"}]},
    "apply.workable.com": {"jobs": [{"shortcode": "W1", "title": "PM", "city": "Austin", "state": "TX",
        "country": "US", "url": "https://wk/W1", "published_on": "2026-08-01", "description": "<p>hi</p>"}]},
}


@pytest.fixture(autouse=True)
def fake_get(monkeypatch):
    def _get(url, **params):
        for host, data in SAMPLES.items():
            if host in url:
                return data
        raise AssertionError(url)
    monkeypatch.setattr(jobboards, "_get", _get)


def test_all_boards_parse():
    gh = jobboards.fetch_jobs("greenhouse", "acme")
    assert gh[0].title == "AE" and gh[0].emails == {"recruiter.jane@acme.com"} and gh[0].phones == {"+14155550134"}
    assert jobboards.fetch_jobs("lever", "acme")[0].emails == {"talent@acme.com"}
    assert jobboards.fetch_jobs("ashby", "acme")[0].emails == {"sam.lee@acme.com"}
    assert jobboards.fetch_jobs("workable", "acme")[0].location == "Austin, TX, US"
    assert [b for b, _ in jobboards.detect_board("acme")] == ["greenhouse", "lever", "ashby", "workable"]


def test_import_jobs(tmp_path):
    db = DB(str(tmp_path / "j.db"))
    res = pipeline.import_jobs(db, "greenhouse", "acme", domain="acme.com")
    assert res == {"jobs": 1, "emails": 1, "phones": 1}
    assert db.jobs()[0]["company_key"] == "acme.com"
    pipeline.import_jobs(db, "greenhouse", "acme", domain="acme.com")  # idempotent
    assert len(db.jobs()) == 1
