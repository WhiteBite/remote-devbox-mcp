import pytest

from rdm.proxy import framing
from rdm.proxy.framing import (
    MAX_BODY,
    BodyFraming,
    FramingError,
    FramingOk,
    connection_tokens,
    hop_by_hop,
    parse_request_line,
    validate_head,
    validate_request,
)

HTTP = "HTTP/1.1"


def make(method, headers=(), target="/"):
    lines = [f"{method} {target} {HTTP}".encode(), *[n + b": " + v for n, v in headers]]
    return b"\r\n".join(lines)


def status(decision):
    return decision.status if isinstance(decision, FramingError) else None


@pytest.mark.parametrize("value", [b"1_0", b"+10", b"-1", b"0x10", b"5, 6", b"1, 2", b"", b"   "])
def test_content_length_rejected(value):
    decision = validate_head("POST", "/", HTTP, [(b"content-length", value)])
    assert status(decision) == 400


@pytest.mark.parametrize(
    "value, expected",
    [(b"5", 5), (b" 5 ", 5), (b"5, 5", 5), (b"0005", 5), (b"0", 0)],
)
def test_content_length_accepted(value, expected):
    decision = validate_head("POST", "/", HTTP, [(b"content-length", value)])
    assert decision == FramingOk(BodyFraming.LENGTH, expected, False)


def test_duplicate_identical_content_length_collapses():
    decision = validate_head(
        "POST", "/", HTTP, [(b"content-length", b"5"), (b"content-length", b"5")]
    )
    assert decision == FramingOk(BodyFraming.LENGTH, 5, False)


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"5"), (b"content-length", b"6")],
        [(b"content-length", b"5, 6")],
    ],
)
def test_differing_content_length_rejected(headers):
    assert status(validate_head("POST", "/", HTTP, headers)) == 400


@pytest.mark.parametrize(
    "name",
    [
        b"host",
        b"authorization",
        b"upgrade",
        b"connection",
        b"expect",
        b"transfer-encoding",
    ],
)
def test_duplicate_singleton_header_rejected(name):
    headers = [(name, b"a"), (name, b"b")]
    assert status(validate_head("GET", "/", HTTP, headers)) == 400


def test_head_with_content_length_rejected():
    assert status(validate_head("HEAD", "/", HTTP, [(b"content-length", b"5")])) == 400


def test_head_with_zero_content_length_allowed():
    decision = validate_head("HEAD", "/", HTTP, [(b"content-length", b"0")])
    assert decision == FramingOk(BodyFraming.LENGTH, 0, False)


def test_delete_with_content_length_allowed():
    decision = validate_head("DELETE", "/", HTTP, [(b"content-length", b"5")])
    assert decision == FramingOk(BodyFraming.LENGTH, 5, False)


@pytest.mark.parametrize("method", ["GET", "OPTIONS"])
def test_bodyless_without_length_is_no_body(method):
    decision = validate_head(method, "/", HTTP, [])
    assert decision == FramingOk(BodyFraming.NONE, 0, False)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_body_required_without_length_is_411(method):
    assert status(validate_head(method, "/", HTTP, [])) == 411


def test_post_with_zero_content_length_allowed():
    decision = validate_head("POST", "/", HTTP, [(b"content-length", b"0")])
    assert decision == FramingOk(BodyFraming.LENGTH, 0, False)


def test_content_length_at_boundary_allowed():
    decision = validate_head(
        "POST", "/", HTTP, [(b"content-length", str(MAX_BODY).encode())]
    )
    assert decision == FramingOk(BodyFraming.LENGTH, MAX_BODY, False)


def test_content_length_over_boundary_rejected():
    decision = validate_head(
        "POST", "/", HTTP, [(b"content-length", str(MAX_BODY + 1).encode())]
    )
    assert status(decision) == 413


def test_transfer_encoding_with_content_length_rejected():
    headers = [(b"transfer-encoding", b"chunked"), (b"content-length", b"5")]
    assert status(validate_head("POST", "/", HTTP, headers)) == 400


@pytest.mark.parametrize(
    "value, expected",
    [
        (b"chunked", 501),
        (b"gzip, chunked", 501),
        (b"chunked, gzip", 400),
        (b"chunked, chunked", 400),
        (b"gzip", 501),
        (b"identity", 501),
    ],
)
def test_transfer_encoding_tokens(value, expected):
    decision = validate_head("POST", "/", HTTP, [(b"transfer-encoding", value)])
    assert status(decision) == expected


@pytest.mark.parametrize("version", ["HTTP/9.9", "HTTP/2.0", "HTTP/1.2"])
def test_unsupported_version(version):
    assert status(validate_head("GET", "/", version, [])) == 505


def test_too_many_headers():
    headers = [(f"x-{i}".encode(), b"v") for i in range(101)]
    assert status(validate_head("GET", "/", HTTP, headers)) == 431


def test_header_line_too_long():
    headers = [(b"x-big", b"a" * (70 * 1024))]
    assert status(validate_head("GET", "/", HTTP, headers)) == 431


def test_header_block_too_large():
    headers = [(f"x-{i}".encode(), b"a" * 2048) for i in range(40)]
    assert status(validate_head("GET", "/", HTTP, headers)) == 431


def test_header_value_injection_rejected():
    headers = [(b"x-evil", b"a\r\nb")]
    assert status(validate_head("GET", "/", HTTP, headers)) == 400


def test_empty_header_name_rejected():
    assert status(validate_head("GET", "/", HTTP, [(b"", b"x")])) == 400


@pytest.mark.parametrize("value", [b"100-continue", b"100-Continue", b" 100-continue "])
def test_expect_continue_allowed(value):
    decision = validate_head("POST", "/", HTTP, [(b"expect", value), (b"content-length", b"0")])
    assert decision == FramingOk(BodyFraming.LENGTH, 0, True)


@pytest.mark.parametrize("value", [b"foo", b"100-continue-x", b"x100-continue"])
def test_expect_other_rejected(value):
    decision = validate_head("POST", "/", HTTP, [(b"expect", value), (b"content-length", b"0")])
    assert status(decision) == 417


def test_parse_request_line_ok():
    assert parse_request_line(b"GET /x HTTP/1.1") == ("GET", "/x", "HTTP/1.1")


@pytest.mark.parametrize(
    "line",
    [b"OPTIONS *", b"GET /", b"GET  / HTTP/1.1", b"", b"GET / HTTP/1.1 extra"],
)
def test_parse_request_line_rejected(line):
    with pytest.raises(framing.FramingRejected):
        parse_request_line(line)


def test_validate_request_get_ok():
    assert validate_request(make("GET", [(b"host", b"h")])) == FramingOk(
        BodyFraming.NONE, 0, False
    )


def test_validate_request_post_with_length_ok():
    assert validate_request(make("POST", [(b"content-length", b"3")])) == FramingOk(
        BodyFraming.LENGTH, 3, False
    )


def test_validate_request_space_before_colon():
    raw = b"POST / HTTP/1.1\r\nTransfer-Encoding : chunked\r\nContent-Length: 0"
    assert status(validate_request(raw)) == 400


def test_validate_request_obs_fold():
    raw = b"GET / HTTP/1.1\r\nHost: x\r\n X-Evil: y"
    assert status(validate_request(raw)) == 400


def test_validate_request_bare_lf():
    assert status(validate_request(b"GET / HTTP/1.1\nHost: x")) == 400


def test_validate_request_bare_cr():
    assert status(validate_request(b"GET / HTTP/1.1\rHost: x")) == 400


def test_validate_request_header_without_colon():
    assert status(validate_request(b"GET / HTTP/1.1\r\nbroken")) == 400


def test_hop_by_hop_named_by_connection_removed():
    headers = [(b"connection", b"close, X-Foo"), (b"x-foo", b"bar"), (b"host", b"h")]
    removed = {name for name, _ in hop_by_hop(headers)}
    assert removed == {b"connection", b"x-foo"}
    kept = [(name, value) for name, value in headers if name not in removed]
    assert kept == [(b"host", b"h")]


def test_connection_tokens_case_and_ows():
    headers = [(b"connection", b" keep-alive , TE "), (b"connection", b"X-Bar")]
    assert connection_tokens(headers) == {b"keep-alive", b"te", b"x-bar"}