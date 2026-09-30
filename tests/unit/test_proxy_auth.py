import pytest

from rdm.proxy.auth import check_token, constant_time_equal

TOKEN = b"s3cret-token"


def auth(value):
    return [(b"authorization", value)]


def test_correct_token():
    assert check_token(auth(b"Bearer s3cret-token"), TOKEN) is True


def test_missing_header():
    assert check_token([], TOKEN) is False


def test_empty_value():
    assert check_token(auth(b""), TOKEN) is False


def test_whitespace_only_after_scheme():
    assert check_token(auth(b"Bearer   "), TOKEN) is False


def test_duplicate_authorization():
    headers = [(b"authorization", b"Bearer s3cret-token")] * 2
    assert check_token(headers, TOKEN) is False


def test_basic_scheme():
    assert check_token(auth(b"Basic s3cret-token"), TOKEN) is False


def test_case_insensitive_scheme():
    assert check_token(auth(b"bearer s3cret-token"), TOKEN) is True


def test_trailing_ows_trimmed():
    assert check_token(auth(b"Bearer s3cret-token \t "), TOKEN) is True


def test_wrong_token():
    assert check_token(auth(b"Bearer other"), TOKEN) is False


def test_str_value_no_crash():
    assert check_token([(b"authorization", "Bearer s3cret-token")], TOKEN) is False


def test_str_name_no_crash():
    assert check_token([("authorization", b"Bearer s3cret-token")], TOKEN) is False


def test_str_expected_no_crash():
    assert check_token(auth(b"Bearer s3cret-token"), "s3cret-token") is False


def test_empty_expected_rejected():
    assert check_token(auth(b"Bearer s3cret-token"), b"") is False


def test_no_token_after_scheme():
    assert check_token(auth(b"Bearer"), TOKEN) is False


def test_constant_time_equal():
    assert constant_time_equal(b"abc", b"abc") is True
    assert constant_time_equal(b"abc", b"abd") is False
    assert constant_time_equal(b"short", b"much-longer-value") is False