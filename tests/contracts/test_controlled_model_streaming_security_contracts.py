"""Unicode 分片与跨 provider fragment 的受控模型文本流安全合同。"""

from __future__ import annotations

import pytest

from agent_harness.models.streaming import (
    IncrementalTextGuard,
    StreamLimitExceeded,
    StreamSafetyError,
    Utf8TextChunker,
)
from agent_harness.security.redaction import redact_secrets


def _guarded_fragments(text: str, split: int) -> tuple[list[str], str]:
    guard = IncrementalTextGuard(max_candidate_utf8_bytes=512)
    emitted = [*guard.feed(text[:split]), *guard.feed(text[split:]), *guard.finish()]
    return emitted, "".join(emitted)


@pytest.mark.parametrize(
    "secret_text",
    [
        "prefix sk-abcdefgh suffix",
        "prefix authorization: Bearer abcdefgh suffix",
        "authorization=&secretvalue",
        "prefix cookie: session=abcdefgh\nnext",
        "prefix api_key=abcdefgh suffix",
        "prefix password=abcdefgh suffix",
        "prefix secret=abcdefgh suffix",
        "prefix token=abcdefgh suffix",
    ],
)
def test_incremental_guard_redacts_secrets_across_every_fragment_boundary(
    secret_text: str,
) -> None:
    """任意供应商切块都不能让已识别凭证值先于整体替换进入公共输出。"""

    expected = redact_secrets(secret_text)
    assert isinstance(expected, str)
    for split in range(len(secret_text) + 1):
        emitted, combined = _guarded_fragments(secret_text, split)
        assert combined == expected
        assert "abcdefgh" not in combined
        assert "[REDACTED]" in combined
        assert all("abcdefgh" not in fragment for fragment in emitted)


@pytest.mark.parametrize(
    "secret_text",
    [
        "OPENAI_API_KEY=abcdefgh suffix",
        "db_password=abcdefgh suffix",
        "client_secret=abcdefgh suffix",
        "access_token=abcdefgh suffix",
    ],
)
def test_incremental_guard_matches_existing_redaction_for_embedded_secret_keys(
    secret_text: str,
) -> None:
    """配置名前缀不能改变既有自由文本脱敏语义，任意字符边界都必须同义。"""

    expected = redact_secrets(secret_text)
    assert isinstance(expected, str)
    for split in range(len(secret_text) + 1):
        emitted, combined = _guarded_fragments(secret_text, split)
        assert combined == expected
        assert "abcdefgh" not in combined
        assert all("abcdefgh" not in fragment for fragment in emitted)

    guard = IncrementalTextGuard(max_candidate_utf8_bytes=512)
    emitted = [fragment for character in secret_text for fragment in guard.feed(character)]
    emitted.extend(guard.finish())
    assert "".join(emitted) == expected
    assert all("abcdefgh" not in fragment for fragment in emitted)


@pytest.mark.parametrize(
    "authorization_text",
    [
        "authorization: Bearer ",
        "authorization: Basic ",
        "authorization: Bearer ;a",
        "authorization: Basic ,a",
    ],
)
def test_incremental_guard_matches_existing_redaction_for_scheme_only_authorization(
    authorization_text: str,
) -> None:
    """流结束时的 scheme-only 形状也必须复用既有正则的回退替换语义。"""

    expected = redact_secrets(authorization_text)
    assert isinstance(expected, str)
    for split in range(len(authorization_text) + 1):
        _emitted, combined = _guarded_fragments(authorization_text, split)
        assert combined == expected

    guard = IncrementalTextGuard(max_candidate_utf8_bytes=512)
    emitted = [fragment for character in authorization_text for fragment in guard.feed(character)]
    emitted.extend(guard.finish())
    assert "".join(emitted) == expected


@pytest.mark.parametrize(
    "cookie_text",
    [
        "cookie:",
        "cookie:;session=abc",
        "set-cookie:",
        "set-cookie:;Path=/",
        "cookie:\na",
        "set-cookie:\ra",
    ],
)
def test_incremental_guard_matches_existing_redaction_for_empty_cookie_values(
    cookie_text: str,
) -> None:
    """空值和分号起始值不是既有 cookie secret，任意切点都不得改写文本。"""

    expected = redact_secrets(cookie_text)
    assert isinstance(expected, str)
    for split in range(len(cookie_text) + 1):
        _emitted, combined = _guarded_fragments(cookie_text, split)
        assert combined == expected

    guard = IncrementalTextGuard(max_candidate_utf8_bytes=512)
    emitted = [fragment for character in cookie_text for fragment in guard.feed(character)]
    emitted.extend(guard.finish())
    assert "".join(emitted) == expected


@pytest.mark.parametrize(
    "text",
    [
        "_authorization: z",
        "aauthorization: z",
        "éauthorization: z",
        "_cookie: z",
        "9cookie: z",
        "变量cookie: z",
        "_set-cookie: z",
        " authorization: z",
        ".cookie: z",
        "（set-cookie: z",
    ],
)
def test_incremental_guard_preserves_word_boundary_semantics_across_fragments(
    text: str,
) -> None:
    """左侧单词字符跨切点时不得伪造 ``\b``，标点或空白边界仍须脱敏。"""

    expected = redact_secrets(text)
    assert isinstance(expected, str)
    for split in range(len(text) + 1):
        _emitted, combined = _guarded_fragments(text, split)
        assert combined == expected

    guard = IncrementalTextGuard(max_candidate_utf8_bytes=512)
    emitted = [fragment for character in text for fragment in guard.feed(character)]
    emitted.extend(guard.finish())
    assert "".join(emitted) == expected


def test_incremental_guard_emits_proven_safe_prefix_before_final_result() -> None:
    """普通文本无需等待最终结果，安全前缀可持续进入后续有界分片。"""

    guard = IncrementalTextGuard(max_candidate_utf8_bytes=512)
    emitted = guard.feed("ordinary public text with enough safe prefix ")

    assert emitted
    assert "".join(emitted) in "ordinary public text with enough safe prefix "


def test_sensitive_candidate_overflow_fails_closed_without_releasing_candidate() -> None:
    """触发词后的未终止候选超过硬上限时不泄漏前缀，也不扩大缓冲。"""

    guard = IncrementalTextGuard(max_candidate_utf8_bytes=128)
    safe = guard.feed("public ")
    assert "secret" not in "".join(safe).lower()

    with pytest.raises(StreamSafetyError):
        guard.feed("secret=" + "a" * 129)
    # 该断言验证无法从公共输出观察的 retained-memory 硬边界；不为此扩大生产 API。
    retained = vars(guard)["_buffer"]
    assert isinstance(retained, str)
    assert len(retained.encode("utf-8")) <= 128


def test_utf8_chunker_respects_character_boundaries_target_and_hard_count() -> None:
    """中文与 emoji 不得被截断，公共片段按 UTF-8 bytes 计数且最多 64 条。"""

    chunker = Utf8TextChunker(target_utf8_bytes=7)
    chunks = [*chunker.feed("你好🙂abc世界"), *chunker.finish()]

    assert "".join(chunks) == "你好🙂abc世界"
    assert all(chunk and len(chunk.encode("utf-8")) <= 7 for chunk in chunks)
    assert chunks == ["你好", "🙂abc", "世界"]

    overflow = Utf8TextChunker(target_utf8_bytes=1)
    with pytest.raises(StreamLimitExceeded):
        overflow.feed("a" * 65)


@pytest.mark.parametrize("target", [0, 4097, True, "1024"])
def test_utf8_chunker_rejects_invalid_or_coerced_target(target: object) -> None:
    """分片器自身也保护 1～4096 硬边界，不能只依赖外层配置。"""

    with pytest.raises(ValueError):
        Utf8TextChunker(target_utf8_bytes=target)  # type: ignore[arg-type]
