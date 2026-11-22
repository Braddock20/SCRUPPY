"""Rulebook tests — including the regression that motivated word boundaries."""

from igbot.rules import Rulebook, Rule, load_rules


def test_substring_does_not_match_unrelated_words():
    """"hi" must not fire on "shipping" or "which"."""
    book = Rulebook([Rule(match=["hi"], reply="greeting")])
    assert book.match("When is shipping?") is None
    assert book.match("Which one should I pick?") is None
    assert book.match("I need a child seat") is None
    assert book.match("This is the shrine") is None


def test_real_greeting_matches():
    book = Rulebook([Rule(match=["hi"], reply="greeting")])
    assert book.match("Hi!") is not None
    assert book.match("hi there") is not None
    assert book.match("Hey, quick question") is None


def test_multi_word_phrase_matches():
    book = Rulebook([Rule(match=["how much"], reply="pricing")])
    assert book.match("Hi, how much is it?") is not None
    assert book.match("How much?") is not None
    assert book.match("How muched up") is None


def test_most_specific_rule_wins():
    rules = load_rules(
        [
            {"name": "generic", "match": ["returns"], "reply": "generic"},
            {"name": "specific", "match": ["return shipping"], "reply": "specific"},
        ]
    )
    book = Rulebook(rules)
    match = book.match("Question about return shipping please")
    assert match is not None
    assert match.reply == "specific"


def test_priority_overrides_specificity():
    rules = load_rules(
        [
            {"name": "long", "match": ["return shipping"], "reply": "long"},
            {"name": "pinned", "match": ["returns"], "reply": "pinned", "priority": 10},
        ]
    )
    book = Rulebook(rules)
    assert book.match("returns?").reply == "pinned"


def test_reply_for_falls_back_to_default():
    book = Rulebook([])
    text, source = book.reply_for("something odd", "default reply")
    assert text == "default reply"
    assert source == "default"


def test_rule_without_reply_is_skipped():
    rules = load_rules([{"match": ["x"], "reply": ""}, {"match": ["y"], "reply": "ok"}])
    assert len(rules) == 1
    assert rules[0].reply == "ok"


def test_punctuation_does_not_break_matching():
    book = Rulebook([Rule(match=["price"], reply="p")])
    assert book.match("What's the price?") is not None
    assert book.match("price, please") is not None
