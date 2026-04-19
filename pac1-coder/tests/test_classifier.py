"""Tests for classify_task() regex rules — deterministic task type routing."""
import pytest


def _classify():
    from agent.classifier import classify_task
    return classify_task


def _types():
    from agent import classifier
    return classifier


def test_bulk_keywords_longcontext():
    c = _classify()
    assert c("delete all threads from the vault") == "longContext"
    assert c("remove all cards and threads") == "longContext"


def test_three_paths_longcontext():
    c = _classify()
    assert c("compare /a/x.md /b/y.md /c/z.md") == "longContext"


def test_inbox_keywords():
    c = _classify()
    assert c("process the inbox") == "inbox"
    assert c("check inbox for new messages") == "inbox"
    assert c("handle the inbox") == "inbox"


def test_email_keywords():
    c = _classify()
    assert c("send an email to John about the meeting") == "email"
    assert c("compose email to recipient with subject") == "email"


def test_lookup_keywords():
    c = _classify()
    assert c("what is the email of David Linke") == "lookup"
    assert c("find the phone number for John") == "lookup"


def test_count_query_lookup():
    c = _classify()
    assert c("how many blacklisted contacts are there") == "lookup"
    assert c("count all entries in telegram") == "lookup"


def test_think_keywords():
    c = _classify()
    assert c("analyze the trends in the data") == "think"
    assert c("summarize the main points") == "think"


def test_distill_keywords():
    c = _classify()
    assert c("summarize the thread and write a card") == "distill"
    assert c("analyze and create a summary file") == "distill"


def test_default_fallback():
    c = _classify()
    assert c("reschedule the follow-up by 2 weeks") == "default"
    assert c("create an invoice for the order") == "default"


def test_count_with_write_is_default():
    """count + write verb → NOT lookup (write intent present)."""
    c = _classify()
    # "count entries and update the total" has write verb → _WRITE_VERBS_RE blocks lookup
    assert c("count entries and update the total") == "default"
