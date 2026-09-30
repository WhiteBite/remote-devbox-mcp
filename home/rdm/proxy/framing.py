"""Pure request-head framing and validation for the ingress proxy."""

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum

MAX_BODY = 50 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_LINE = 8 * 1024
MAX_HEADER_COUNT = 100

_REQUEST_LINE = re.compile(rb"([^ ]+) ([^ ]+) ([^ ]+)", re.ASCII)
_CONTENT_LENGTH = re.compile(rb"[0-9]{1,19}", re.ASCII)
_OWS = b" \t"

_BODYLESS = frozenset(("GET", "HEAD", "DELETE", "OPTIONS"))
_BODY_REQUIRED = frozenset(("POST", "PUT", "PATCH"))
_DUP_FORBIDDEN = frozenset(
    (b"transfer-encoding", b"host", b"authorization", b"upgrade", b"connection", b"expect")
)
_HOP_BY_HOP = frozenset(
    (
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    )
)


class BodyFraming(str, Enum):
    NONE = "none"
    LENGTH = "length"


@dataclass(frozen=True, slots=True)
class FramingOk:
    framing: BodyFraming
    content_length: int
    expect_continue: bool


@dataclass(frozen=True, slots=True)
class FramingError:
    status: int
    reason: str


FramingDecision = FramingOk | FramingError


class FramingRejected(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.decision = FramingError(status, reason)


def parse_request_line(line: bytes) -> tuple[str, str, str]:
    match = _REQUEST_LINE.fullmatch(line)
    if match is None:
        raise FramingRejected(400, "malformed request line")
    return (
        match.group(1).decode("latin-1"),
        match.group(2).decode("latin-1"),
        match.group(3).decode("latin-1"),
    )


def _reject_bare_line_endings(head: bytes) -> None:
    plain = head.replace(b"\r\n", b"")
    if b"\r" in plain or b"\n" in plain:
        raise FramingRejected(400, "bare line ending")


def parse_head(block: bytes) -> list[tuple[bytes, bytes]]:
    if not block:
        return []
    headers: list[tuple[bytes, bytes]] = []
    for line in block.split(b"\r\n"):
        if line[:1] in (b" ", b"\t"):
            raise FramingRejected(400, "obsolete line folding")
        name, separator, value = line.partition(b":")
        if not separator or not name or b" " in name or b"\t" in name:
            raise FramingRejected(400, "invalid header line")
        headers.append((name.lower(), value))
    return headers


def _parse_content_length(values: list[bytes]) -> int:
    tokens = [token.strip(_OWS) for value in values for token in value.split(b",")]
    if not tokens or any(_CONTENT_LENGTH.fullmatch(token) is None for token in tokens):
        raise FramingRejected(400, "invalid content-length")
    lengths = {int(token) for token in tokens}
    if len(lengths) != 1:
        raise FramingRejected(400, "conflicting content-length")
    return lengths.pop()


def _validate_transfer_encoding(values: list[bytes]) -> None:
    tokens: list[str] = []
    for value in values:
        try:
            decoded = value.decode("ascii")
        except UnicodeDecodeError:
            raise FramingRejected(400, "non-ascii transfer-encoding") from None
        tokens.extend(part.strip(" \t").lower() for part in decoded.split(","))
    if not tokens or "" in tokens:
        raise FramingRejected(400, "empty transfer coding")
    if tokens.count("chunked") > 1:
        raise FramingRejected(400, "duplicate chunked coding")
    if "chunked" in tokens and tokens[-1] != "chunked":
        raise FramingRejected(400, "chunked coding not final")
    if any(token != "chunked" for token in tokens):
        raise FramingRejected(501, "unknown transfer coding")
    raise FramingRejected(501, "chunked transfer coding unsupported")


def validate_head(
    method: str,
    target: str,
    version: str,
    headers: list[tuple[bytes, bytes]],
) -> FramingDecision:
    del target
    for name, value in headers:
        if not name or b"\r" in name or b"\n" in name or b"\r" in value or b"\n" in value:
            return FramingError(400, "malformed header")
    total = 0
    for name, value in headers:
        line = len(name) + len(value) + 2
        if line > MAX_HEADER_LINE:
            return FramingError(431, "header line too long")
        total += line
    if len(headers) > MAX_HEADER_COUNT or total > MAX_HEADER_BYTES:
        return FramingError(431, "header block too large")
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        return FramingError(505, "HTTP version not supported")

    counts = Counter(name for name, _ in headers)
    for name in _DUP_FORBIDDEN:
        if counts[name] > 1:
            return FramingError(400, "duplicate singleton header")

    has_transfer_encoding = counts[b"transfer-encoding"] > 0
    content_lengths = [value for name, value in headers if name == b"content-length"]
    if has_transfer_encoding and content_lengths:
        return FramingError(400, "transfer-encoding with content-length")

    try:
        if has_transfer_encoding:
            _validate_transfer_encoding(
                [value for name, value in headers if name == b"transfer-encoding"]
            )
        content_length = _parse_content_length(content_lengths) if content_lengths else None
    except FramingRejected as rejected:
        return rejected.decision

    expect_continue = False
    expectations = [value for name, value in headers if name == b"expect"]
    if expectations:
        if expectations[0].strip(_OWS).lower() != b"100-continue":
            return FramingError(417, "unsupported expectation")
        expect_continue = True

    if content_length is not None and content_length > MAX_BODY:
        return FramingError(413, "payload too large")

    verb = method.upper()
    if verb == "HEAD" and content_length:
        return FramingError(400, "HEAD with body")
    if verb in _BODYLESS:
        framing = BodyFraming.NONE if content_length is None else BodyFraming.LENGTH
        return FramingOk(framing, content_length or 0, expect_continue)
    if verb in _BODY_REQUIRED and content_length is None:
        return FramingError(411, "length required")
    if content_length is None:
        return FramingOk(BodyFraming.NONE, 0, expect_continue)
    return FramingOk(BodyFraming.LENGTH, content_length, expect_continue)


def validate_request(head: bytes) -> FramingDecision:
    try:
        _reject_bare_line_endings(head)
        line, _, block = head.partition(b"\r\n")
        method, target, version = parse_request_line(line)
        headers = parse_head(block)
    except FramingRejected as rejected:
        return rejected.decision
    return validate_head(method, target, version, headers)


def connection_tokens(headers: list[tuple[bytes, bytes]]) -> set[bytes]:
    tokens: set[bytes] = set()
    for name, value in headers:
        if name == b"connection":
            for token in value.split(b","):
                stripped = token.strip(_OWS).lower()
                if stripped:
                    tokens.add(stripped)
    return tokens


def hop_by_hop(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    named = _HOP_BY_HOP | connection_tokens(headers)
    return [(name, value) for name, value in headers if name in named]