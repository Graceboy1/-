"""Every test archives only into its own disposable temporary SQLite database."""
import pytest


@pytest.fixture(autouse=True)
def isolate_research_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEST_RESEARCH_DB", str(tmp_path / "research-tests.sqlite3"))
    monkeypatch.setenv("CONTEST_NEWS_DB", str(tmp_path / "news-tests.sqlite3"))
    monkeypatch.setenv("CONTEST_NEWS_AI_DB", str(tmp_path / "news-ai-tests.sqlite3"))
    monkeypatch.setenv("CONTEST_NEWS_AUTO_REFRESH", "0")
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(tmp_path / "no-user-market-data"))
    monkeypatch.setenv("CONTEST_PAPER_DB", str(tmp_path / "paper-tests.sqlite3"))
