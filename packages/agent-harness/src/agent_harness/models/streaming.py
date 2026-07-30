"""供应商文本进入 durable event 前的有界分片与跨块安全状态机。"""

from __future__ import annotations

import re

MAX_STREAM_DELTAS = 64
MAX_STREAM_CHUNK_UTF8_BYTES = 4096
MAX_STREAM_COLLECTOR_UTF8_BYTES = MAX_STREAM_DELTAS * MAX_STREAM_CHUNK_UTF8_BYTES
STREAM_EVENT_RESERVATION = MAX_STREAM_DELTAS + 1

_KEYWORDS = (
    "authorization",
    "set-cookie",
    "api_key",
    "api-key",
    "password",
    "secret",
    "cookie",
    "token",
    "sk-",
)
_MAX_KEYWORD_LENGTH = max(map(len, _KEYWORDS))
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)\bauthorization"
    r"\s*[:=]\s*(?:(?:bearer|basic)\s+)?['\"]?"
)
_KEY_HEADER = re.compile(r"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?")
_COOKIE_HEADER = re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:")
_AUTHORIZATION_SCHEME_TAIL = re.compile(r"(?i)(bearer|basic)(?:\s+['\"]?)$")
_AUTHORIZATION_VALUE_TERMINATOR = re.compile(r"[\s,'\";]")
_KEY_VALUE_TERMINATOR = re.compile(r"[\s,'\";&]")
_SK_VALUE_CHARACTER = re.compile(r"[A-Za-z0-9_-]")
_WORD_CHARACTER = re.compile(r"\w")
_WORD_BOUNDARY_KEYWORDS = frozenset({"authorization", "set-cookie", "cookie"})


class StreamLimitExceeded(RuntimeError):
    """公共文本无法在 64×4096 bytes 的固定合同内表达。"""

    code = "model.provider_side_effect_unknown"


class StreamSafetyError(RuntimeError):
    """敏感候选超过可证明上限，任何候选字节都不得公开。"""

    code = "model.provider_side_effect_unknown"


def bounded_utf8_size(text: str, *, max_bytes: int) -> int | None:
    """不构造完整 bytes，在超过上限的首个字符立即返回。"""

    total = 0
    for character in text:
        codepoint = ord(character)
        if codepoint <= 0x7F:
            size = 1
        elif codepoint <= 0x7FF:
            size = 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("stream text must not contain surrogate code points")
        elif codepoint <= 0xFFFF:
            size = 3
        else:
            size = 4
        total += size
        if total > max_bytes:
            return None
    return total


def _utf8_prefix_end(text: str, *, start: int, max_bytes: int) -> int:
    """返回从 start 起不超过字节预算的最长字符边界。"""

    used = 0
    for index in range(start, len(text)):
        size = len(text[index].encode("utf-8"))
        if used + size > max_bytes:
            return index
        used += size
    return len(text)


class Utf8TextChunker:
    """按字符边界形成稳定 UTF-8 目标分片，并执行 64 条硬上限。"""

    def __init__(self, *, target_utf8_bytes: object) -> None:
        if (
            isinstance(target_utf8_bytes, bool)
            or not isinstance(target_utf8_bytes, int)
            or not 1 <= target_utf8_bytes <= MAX_STREAM_CHUNK_UTF8_BYTES
        ):
            raise ValueError("stream chunk target must be an integer between 1 and 4096")
        self._target = target_utf8_bytes
        self._pending = ""
        self._emitted = 0

    @property
    def emitted_count(self) -> int:
        """返回已经形成的公共片段数量，供 completed 摘要核对。"""

        return self._emitted

    def feed(self, text: str) -> list[str]:
        """接收连续安全文本；保留不足一个目标片的尾部等待后续合并。"""

        if not text:
            return []
        self._pending += text
        chunks: list[str] = []
        while len(self._pending.encode("utf-8")) >= self._target:
            chunk, remainder = self._split_target(self._pending)
            self._append_chunk(chunks, chunk)
            self._pending = remainder
        return chunks

    def finish(self) -> list[str]:
        """流结束时提交非空尾部；重复调用不会生成空 delta。"""

        if not self._pending:
            return []
        pending = self._pending
        self._pending = ""
        chunks: list[str] = []
        while pending:
            chunk, pending = self._split_target(pending)
            self._append_chunk(chunks, chunk)
        return chunks

    def _split_target(self, text: str) -> tuple[str, str]:
        """取不截断 Unicode 的最长目标前缀；单字符可超过较小目标但不越硬上限。"""

        used = 0
        end = 0
        for index, character in enumerate(text):
            size = len(character.encode("utf-8"))
            if end and used + size > self._target:
                break
            if not end and size > self._target:
                if size > MAX_STREAM_CHUNK_UTF8_BYTES:  # pragma: no cover - Unicode 上限更小
                    raise StreamLimitExceeded
                end = 1
                break
            used += size
            end = index + 1
            if used == self._target:
                break
        if end == 0:  # pragma: no cover - 非空 Python str 至少含一个 code point
            raise StreamLimitExceeded
        return text[:end], text[end:]

    def _append_chunk(self, chunks: list[str], chunk: str) -> None:
        if not chunk or len(chunk.encode("utf-8")) > MAX_STREAM_CHUNK_UTF8_BYTES:
            raise StreamLimitExceeded
        if self._emitted >= MAX_STREAM_DELTAS:
            raise StreamLimitExceeded
        self._emitted += 1
        chunks.append(chunk)


class IncrementalTextGuard:
    """跨 provider fragment 保存触发词/值候选，只释放已证明安全的前缀。

    状态机对现有 redaction 规则采用同样的整体替换语义。它不会按固定尾窗释放
    已识别候选：触发词后的值必须遇到确定终止符或流结束，超过配置则 fail closed。
    """

    def __init__(self, *, max_candidate_utf8_bytes: object) -> None:
        if (
            isinstance(max_candidate_utf8_bytes, bool)
            or not isinstance(max_candidate_utf8_bytes, int)
            or not 128 <= max_candidate_utf8_bytes <= 4096
        ):
            raise ValueError("sensitive candidate limit must be an integer between 128 and 4096")
        self._limit = max_candidate_utf8_bytes
        self._buffer = ""
        # ``authorization`` / ``cookie`` 使用 Unicode ``\b``。安全前缀一旦
        # 已发布便不再位于 buffer 中，因此必须显式携带其最后一个公开字符
        # 的词字符类别，避免在 fragment 边界伪造新的单词边界。
        self._left_is_word = False
        self._finished = False
        self._failed = False

    def feed(self, text: str) -> list[str]:
        """追加原始文本并返回本轮已证明安全的非空片段。"""

        if self._finished or self._failed:
            raise RuntimeError("incremental text guard is already closed")
        if not text:
            return []
        emitted: list[str] = []
        offset = 0
        while offset < len(text):
            available = self._limit - len(self._buffer.encode("utf-8"))
            prefix_end = _utf8_prefix_end(text, start=offset, max_bytes=available)
            if prefix_end == offset:
                before = self._buffer
                emitted.extend(self._drain(final=False))
                if self._buffer == before:
                    self._raise_candidate_overflow()
                continue
            self._buffer += text[offset:prefix_end]
            offset = prefix_end
            emitted.extend(self._drain(final=False))
        return emitted

    def finish(self) -> list[str]:
        """以流结束作为候选终止符，完成最后一次整体脱敏。"""

        if self._failed:
            raise RuntimeError("incremental text guard has failed")
        if self._finished:
            return []
        self._finished = True
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[str]:
        emitted: list[str] = []
        while self._buffer:
            candidate = self._earliest_complete_header()
            if candidate is not None:
                kind, start, value_start = candidate
                if start:
                    self._emit(emitted, self._buffer[:start])
                    self._buffer = self._buffer[start:]
                    value_start -= start
                end = self._candidate_end(kind=kind, value_start=value_start, final=final)
                if end is None:
                    self._assert_candidate_bound()
                    break
                if end == 0:
                    self._emit(emitted, self._buffer[0])
                    self._buffer = self._buffer[1:]
                    continue
                self._emit(emitted, "[REDACTED]")
                self._buffer = self._buffer[end:]
                continue

            if final:
                self._emit(emitted, self._buffer)
                self._buffer = ""
                break
            potential = self._potential_suffix_start()
            keep_from = (
                potential
                if potential is not None
                else max(0, len(self._buffer) - (_MAX_KEYWORD_LENGTH - 1))
            )
            if keep_from == 0:
                self._assert_candidate_bound()
                break
            self._emit(emitted, self._buffer[:keep_from])
            self._buffer = self._buffer[keep_from:]
            break
        return [item for item in emitted if item]

    def _emit(self, emitted: list[str], text: str) -> None:
        """追加公开文本并保存新 buffer 左侧的 Unicode 单词边界状态。"""

        if not text:
            return
        emitted.append(text)
        self._left_is_word = _WORD_CHARACTER.fullmatch(text[-1]) is not None

    def _earliest_complete_header(self) -> tuple[str, int, int] | None:
        matches: list[tuple[str, int, int]] = []
        authorization_match = self._search_boundary_header(_AUTHORIZATION_HEADER)
        if authorization_match is not None:
            matches.append(
                ("authorization", authorization_match.start(), authorization_match.end())
            )
        key_match = _KEY_HEADER.search(self._buffer)
        if key_match is not None:
            matches.append(("key", key_match.start(), key_match.end()))
        cookie_match = self._search_boundary_header(_COOKIE_HEADER)
        if cookie_match is not None:
            matches.append(("cookie", cookie_match.start(), cookie_match.end()))
        sk_start = self._buffer.find("sk-")
        if sk_start >= 0:
            matches.append(("sk", sk_start, sk_start + 3))
        return min(matches, key=lambda item: item[1]) if matches else None

    def _search_boundary_header(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        """补回已发布前缀的左侧上下文，并允许跳过起点后查找重叠子 header。"""

        position = 0
        while match := pattern.search(self._buffer, position):
            if match.start() != 0 or not self._left_is_word:
                return match
            # `set-cookie` 在起点可能因左侧词字符无效，但其中的 `cookie`
            # 仍可能在连字符后形成合法边界；从下一字符继续搜索而非整体跳过。
            position = match.start() + 1
        return None

    def _candidate_end(self, *, kind: str, value_start: int, final: bool) -> int | None:
        if kind == "cookie":
            # 既有 cookie 正则允许 `\s*` 跨 fragment/换行寻找首个值字符，
            # 但仍要求至少一个非分号字符。普通空白可在回溯时充当值，CR/LF
            # 只能是前导 whitespace 或已开始值后的终止符。
            body_start: int | None = None
            regular_whitespace: int | None = None
            cursor = value_start
            while cursor < len(self._buffer) and self._buffer[cursor].isspace():
                if self._buffer[cursor] not in "\r\n":
                    regular_whitespace = cursor
                cursor += 1
            if cursor == len(self._buffer):
                if not final:
                    return None
                body_start = regular_whitespace
            elif self._buffer[cursor] == ";":
                body_start = regular_whitespace
            else:
                body_start = cursor
            if body_start is None:
                return self._false_positive()
            newline_positions = [
                position
                for position in (
                    self._buffer.find("\r", body_start),
                    self._buffer.find("\n", body_start),
                )
                if position >= 0
            ]
            if newline_positions:
                return min(newline_positions)
            return len(self._buffer) if final else None

        if kind == "sk":
            end = value_start
            while end < len(self._buffer) and _SK_VALUE_CHARACTER.fullmatch(self._buffer[end]):
                end += 1
            length = end - value_start
            if end < len(self._buffer):
                if length >= 8:
                    return end
                # 太短的 sk- 不是凭证；把当前 s 作为安全文本后重新扫描。
                return self._false_positive()
            if final:
                return len(self._buffer) if length >= 8 else self._false_positive()
            return None

        authorization_fallback: int | None = None
        if kind == "authorization":
            # 既有正则在 scheme 后没有 token 或直接遇到分隔符时会回退：把
            # Bearer/Basic 当作值遮蔽，同时保留其后的空白、引号和分隔符。
            scheme = _AUTHORIZATION_SCHEME_TAIL.search(self._buffer[:value_start])
            if scheme is not None:
                authorization_fallback = scheme.end(1)
            if final and value_start == len(self._buffer) and authorization_fallback is not None:
                return authorization_fallback

        # 既有 authorization 规则允许 `&` 进入凭据值，而通用 key/value
        # 规则用它分隔下一个字段；两者必须各自复用原正则的终止集合，不能
        # 因共享状态机把值起始处的 `&` 误判为无值并释放原始凭据。
        value_terminator = (
            _AUTHORIZATION_VALUE_TERMINATOR if kind == "authorization" else _KEY_VALUE_TERMINATOR
        )
        terminator = value_terminator.search(self._buffer, value_start)
        if terminator is not None:
            if terminator.start() == value_start:
                if authorization_fallback is not None:
                    return authorization_fallback
                return self._false_positive()
            return terminator.start()
        if final:
            return len(self._buffer) if value_start < len(self._buffer) else self._false_positive()
        return None

    @staticmethod
    def _false_positive() -> int:
        """用零标记无值/短值触发词，由调用方安全释放一个字符后重新扫描。"""

        return 0

    def _potential_suffix_start(self) -> int | None:
        """保留可能继续形成触发词的后缀，以及 marker 后尚未出现分隔符的空白。"""

        lowered = self._buffer.lower()
        earliest: int | None = None
        for start in range(len(lowered)):
            suffix = lowered[start:]
            for keyword in _KEYWORDS:
                if keyword in _WORD_BOUNDARY_KEYWORDS and not self._has_left_boundary(start):
                    continue
                if keyword.startswith(suffix):
                    earliest = start if earliest is None else min(earliest, start)
                elif keyword != "sk-" and suffix.startswith(keyword):
                    rest = suffix[len(keyword) :]
                    if rest.isspace() or rest == "":
                        earliest = start if earliest is None else min(earliest, start)
        return earliest

    def _has_left_boundary(self, start: int) -> bool:
        """按完整文本的 Unicode ``\b`` 语义判断候选关键词左侧边界。"""

        if start == 0:
            return not self._left_is_word
        return _WORD_CHARACTER.fullmatch(self._buffer[start - 1]) is None

    def _assert_candidate_bound(self) -> None:
        if len(self._buffer.encode("utf-8")) > self._limit:
            self._raise_candidate_overflow()

    def _raise_candidate_overflow(self) -> None:
        """在追加超额字节前关闭状态机，保留不超过硬上限的候选用于诊断。"""

        self._failed = True
        raise StreamSafetyError("sensitive stream candidate exceeds configured bound")


__all__ = [
    "IncrementalTextGuard",
    "MAX_STREAM_CHUNK_UTF8_BYTES",
    "MAX_STREAM_DELTAS",
    "STREAM_EVENT_RESERVATION",
    "StreamLimitExceeded",
    "StreamSafetyError",
    "Utf8TextChunker",
]
