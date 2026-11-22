"""Store, guardrails and cookie-parsing tests."""

import time

import pytest

from igbot.browser import parse_cookie_header
from igbot.config import Config
from igbot.guardrails import Guardrails
from igbot.store import Store, fingerprint_for, day_start, hour_start


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture()
def cfg(tmp_path):
    return Config(
        {
            "behaviour": {
                "cooldown_seconds": 900,
                "hourly_cap": 8,
                "daily_cap": 25,
                "global_daily_cap": 120,
                "baseline_only": False,
            }
        },
        tmp_path,
    )


# ------------------------------------------------------------------- store


def test_fingerprint_is_stable_and_discriminating():
    a = fingerprint_for("t1", "", "Hello there")
    b = fingerprint_for("t1", "", "  hello   THERE ")
    c = fingerprint_for("t2", "", "Hello there")
    assert a == b
    assert a != c


def test_record_is_idempotent_for_the_same_fingerprint(store):
    fp = fingerprint_for("t", "", "hi")
    assert store.record("inbound", thread_id="t", fingerprint=fp) is True
    assert store.record("inbound", thread_id="t", fingerprint=fp) is False
    assert store.has_fingerprint(fp)


def test_count_respects_thread_and_time(store):
    store.record("reply", thread_id="a")
    store.record("reply", thread_id="a")
    store.record("reply", thread_id="b")
    assert store.count("reply", 0, thread_id="a") == 2
    assert store.count("reply", 0, thread_id="b") == 1
    assert store.count("reply", 0) == 3
    assert store.count("reply", time.time() + 100) == 0


def test_day_start_is_midnight(store):
    now = time.time()
    start = day_start(now)
    assert 0 < now - start < 86400
    assert hour_start(now) >= start


# -------------------------------------------------------------- guardrails


def test_first_reply_is_allowed(store, cfg):
    guard = Guardrails(store, cfg)
    assert guard.check("t1").allowed


def test_cooldown_blocks_a_second_reply(store, cfg):
    guard = Guardrails(store, cfg)
    guard.record_reply("t1")
    decision = guard.check("t1")
    assert not decision.allowed
    assert "cooldown" in decision.reason


def test_hourly_cap(store, cfg):
    cfg.set("behaviour.cooldown_seconds", 0)
    cfg.set("behaviour.hourly_cap", 2)
    guard = Guardrails(store, cfg)
    guard.record_reply("t1")
    guard.record_reply("t1")
    decision = guard.check("t1")
    assert not decision.allowed
    assert "hourly" in decision.reason


def test_daily_cap(store, cfg):
    cfg.set("behaviour.cooldown_seconds", 0)
    cfg.set("behaviour.hourly_cap", 0)
    cfg.set("behaviour.daily_cap", 1)
    guard = Guardrails(store, cfg)
    guard.record_reply("t1")
    assert not guard.check("t1").allowed


def test_global_daily_cap_is_shared_across_threads(store, cfg):
    cfg.set("behaviour.cooldown_seconds", 0)
    cfg.set("behaviour.hourly_cap", 0)
    cfg.set("behaviour.daily_cap", 0)
    cfg.set("behaviour.global_daily_cap", 2)
    guard = Guardrails(store, cfg)
    guard.record_reply("a")
    guard.record_reply("b")
    decision = guard.check("c")
    assert not decision.allowed
    assert "global" in decision.reason


def test_baseline_mode_blocks_everything(store, cfg):
    cfg.set("behaviour.baseline_only", True)
    guard = Guardrails(store, cfg)
    decision = guard.check("t1")
    assert not decision.allowed
    assert "baseline" in decision.reason


def test_cap_is_per_conversation(store, cfg):
    cfg.set("behaviour.cooldown_seconds", 0)
    cfg.set("behaviour.hourly_cap", 1)
    guard = Guardrails(store, cfg)
    guard.record_reply("a")
    assert not guard.check("a").allowed
    assert guard.check("b").allowed


# ---------------------------------------------------------------- cookies

REAL_COOKIE = (
    "csrftoken=ICruY3d8dXKF8kUQNQCRrB;datr=lKACa37319WEYUXZ1k8k41qG;"
    "ig_did=BE713D86-894D-400F-9D38-48179368C78F;wd=414x863;"
    "dpr=2.608695652173913;mid=awKglAABAAFciZVSUAtNIWU4k0am;"
    "ds_user_id=63154994371;"
    "sessionid=63154994371%3A3hMZMrNTsuDmKm%3A3%3AAYlGRpB1PlVQ_l1XmjSspW_7bwF-053gUkM-Z_pMCA;"
    'rur="LDC\\05463154994371\\0541826870414:01ff36dea11158aec9a6a216f4d54a2e5a8cd2f9d6f47c5fb4eb9d67a98c24f6bc535210";'
)


def test_parses_every_pair_from_a_real_header():
    cookies = parse_cookie_header(REAL_COOKIE)
    names = {c["name"] for c in cookies}
    assert names == {
        "csrftoken",
        "datr",
        "ig_did",
        "wd",
        "dpr",
        "mid",
        "ds_user_id",
        "sessionid",
        "rur",
    }
    assert len(cookies) == 9


def test_percent_encoded_sessionid_is_decoded():
    cookies = {c["name"]: c["value"] for c in parse_cookie_header(REAL_COOKIE)}
    assert cookies["sessionid"].startswith("63154994371:")
    assert "%3A" not in cookies["sessionid"]


def test_quoted_value_is_unquoted_and_escapes_resolved():
    cookies = {c["name"]: c["value"] for c in parse_cookie_header(REAL_COOKIE)}
    assert cookies["rur"].startswith("LDC,63154994371,1826870414:")
    assert '"' not in cookies["rur"]
    assert "\\" not in cookies["rur"]


def test_values_containing_equals_survive():
    cookies = {c["name"]: c["value"] for c in parse_cookie_header("a=1; b=xx==yy; c=3")}
    assert cookies["b"] == "xx==yy"


def test_empty_and_malformed_input_is_tolerated():
    assert parse_cookie_header("") == []
    assert parse_cookie_header("   ") == []
    assert parse_cookie_header("=novalue") == []
    assert parse_cookie_header("justtext") == []
