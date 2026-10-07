from rdm.redact import redact_argv, redact_text


def test_redact_text_masks_token_assignment():
    assert redact_text("token=abc123") == "[REDACTED]"


def test_redact_text_masks_colon_separated_secret():
    assert redact_text("password: hunter2") == "[REDACTED]"


def test_redact_text_masks_bearer_value():
    assert redact_text("bearer=eyJhbGciOiJIUzI1NiIs") == "[REDACTED]"


def test_redact_text_masks_only_the_secret_span():
    assert redact_text("api_key: 0000ffff rest") == "api_[REDACTED] rest"


def test_redact_text_masks_bare_hex_token():
    assert redact_text("a" * 40) == "[REDACTED]"


def test_redact_text_leaves_space_separated_bearer():
    assert redact_text("Authorization: Bearer deadbeef") == "Authorization: Bearer deadbeef"


def test_redact_text_leaves_plain_text():
    assert redact_text("build finished in 3s") == "build finished in 3s"


def test_redact_argv_passes_through_non_secrets():
    argv = ["npm", "run", "build", "--port=8080"]
    assert redact_argv(argv) == argv


def test_redact_argv_masks_secret_args():
    assert redact_argv(["TOKEN=abc", "a" * 40, "plain"]) == ["[REDACTED]", "[REDACTED]", "plain"]
