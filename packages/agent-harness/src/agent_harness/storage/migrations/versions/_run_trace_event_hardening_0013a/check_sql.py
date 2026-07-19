"""解析 0013a 反射到的受控 CHECK SQL 子集。"""

from __future__ import annotations

from typing import Any, NamedTuple

_MAX_CHECK_EXPRESSION_LENGTH = 4096
_MAX_CHECK_TOKENS = 512
_MAX_CHECK_NESTING = 64


class _CheckParseError(ValueError):
    """表示反射到的 CHECK 不属于迁移允许的受控 SQL 子集。"""


class _CheckToken(NamedTuple):
    """受控 CHECK 语法中的单个词法单元。

    ``kind`` 决定解析器如何解释 ``value``；保留原始值而不在词法阶段归一化，
    让关键字大小写、标识符和字符串字面量能在各自正确的边界处理。
    """

    kind: str
    value: str


_CheckNode = tuple[Any, ...]


def _canonical_boolean(node: _CheckNode) -> _CheckNode:
    """规范化可交换 Boolean 节点，但不做会掩盖约束弱化的代数化简。"""

    operation = node[0]
    if operation not in {"and", "or"}:
        return node
    children: list[_CheckNode] = []
    for child in node[1]:
        canonical = _canonical_boolean(child)
        if canonical[0] == operation:
            children.extend(canonical[1])
        else:
            children.append(canonical)
    return (operation, tuple(sorted(children, key=repr)))


_RECORD_SCOPE = ("column", "record_scope")
_RUN_ID = ("column", "run_id")
_TRACE_ID = ("column", "trace_id")
_RUN = ("string", "run")
_NON_RUN = ("string", "non_run")


def _cast(node: _CheckNode, type_name: str, *, array: bool = False) -> _CheckNode:
    """构造显式类型转换节点，保留 PostgreSQL 与 SQLite 反射差异。"""

    return ("cast", node, type_name, array)


_SQLITE_SCOPE = ("in", _RECORD_SCOPE, tuple(sorted((_RUN, _NON_RUN), key=repr)))
_POSTGRES_SCOPE = (
    "in_any",
    _cast(_RECORD_SCOPE, "text"),
    _cast(
        (
            "array",
            (
                _cast(_RUN, "character varying"),
                _cast(_NON_RUN, "character varying"),
            ),
        ),
        "text",
        array=True,
    ),
)
_SQLITE_RUN_OWNERSHIP = _canonical_boolean(
    (
        "or",
        (
            ("neq", _RECORD_SCOPE, _RUN),
            (
                "and",
                (
                    ("is_not_null", _RUN_ID),
                    ("is_not_null", _TRACE_ID),
                ),
            ),
        ),
    )
)
_POSTGRES_RUN_OWNERSHIP = _canonical_boolean(
    (
        "or",
        (
            ("neq", _cast(_RECORD_SCOPE, "text"), _cast(_RUN, "text")),
            (
                "and",
                (
                    ("is_not_null", _RUN_ID),
                    ("is_not_null", _TRACE_ID),
                ),
            ),
        ),
    )
)
_SQLITE_NON_RUN_OWNERSHIP = _canonical_boolean(
    (
        "or",
        (
            ("neq", _RECORD_SCOPE, _NON_RUN),
            ("is_null", _RUN_ID),
        ),
    )
)
_POSTGRES_NON_RUN_OWNERSHIP = _canonical_boolean(
    (
        "or",
        (
            ("neq", _cast(_RECORD_SCOPE, "text"), _cast(_NON_RUN, "text")),
            ("is_null", _RUN_ID),
        ),
    )
)

_EXPECTED_CHECK_SIGNATURES = {
    "ck_canonical_events_record_scope": (_SQLITE_SCOPE, _POSTGRES_SCOPE),
    "ck_canonical_events_run_ownership": (
        _SQLITE_RUN_OWNERSHIP,
        _POSTGRES_RUN_OWNERSHIP,
    ),
    "ck_canonical_events_non_run_ownership": (
        _SQLITE_NON_RUN_OWNERSHIP,
        _POSTGRES_NON_RUN_OWNERSHIP,
    ),
    "ck_audit_logs_record_scope": (_SQLITE_SCOPE, _POSTGRES_SCOPE),
}


def _tokenize_check_expression(expression: str) -> list[_CheckToken]:
    """词法化 SQLite/PostgreSQL 反射 SQL；任何未知字符均拒绝。"""

    if len(expression) > _MAX_CHECK_EXPRESSION_LENGTH:
        raise _CheckParseError("expression length limit exceeded")
    tokens: list[_CheckToken] = []
    index = 0
    nesting = 0

    def append_token(token: _CheckToken) -> None:
        """追加 token 并在词法阶段执行上限，避免不可信反射 SQL 耗尽资源。"""

        tokens.append(token)
        if len(tokens) > _MAX_CHECK_TOKENS:
            raise _CheckParseError("expression token limit exceeded")

    while index < len(expression):
        character = expression[index]
        if character.isspace():
            index += 1
            continue
        pair = expression[index : index + 2]
        if pair in {"::", "!=", "<>"}:
            append_token(_CheckToken("symbol", pair))
            index += 2
            continue
        if character in "(),=[]":
            if character in "([":
                nesting += 1
                if nesting > _MAX_CHECK_NESTING:
                    raise _CheckParseError("expression nesting limit exceeded")
            elif character in ")]":
                nesting = max(0, nesting - 1)
            append_token(_CheckToken("symbol", character))
            index += 1
            continue
        if character == "'":
            # SQL 字符串用连续单引号转义；只有完整闭合后才交给语法层。
            index += 1
            value: list[str] = []
            while index < len(expression):
                if expression[index] != "'":
                    value.append(expression[index])
                    index += 1
                    continue
                if index + 1 < len(expression) and expression[index + 1] == "'":
                    value.append("'")
                    index += 2
                    continue
                index += 1
                append_token(_CheckToken("string", "".join(value)))
                break
            else:
                raise _CheckParseError("unterminated string")
            continue
        if character == '"':
            # 双引号标识符也支持成对转义，防止把列名误拆为关键字。
            index += 1
            value = []
            while index < len(expression):
                if expression[index] != '"':
                    value.append(expression[index])
                    index += 1
                    continue
                if index + 1 < len(expression) and expression[index + 1] == '"':
                    value.append('"')
                    index += 2
                    continue
                index += 1
                append_token(_CheckToken("word", "".join(value)))
                break
            else:
                raise _CheckParseError("unterminated identifier")
            continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < len(expression) and (
                expression[end].isalnum() or expression[end] in {"_", "$"}
            ):
                end += 1
            append_token(_CheckToken("word", expression[index:end]))
            index = end
            continue
        raise _CheckParseError(f"unsupported character {character!r}")
    append_token(_CheckToken("eof", ""))
    return tokens


class _CheckExpressionParser:
    """把四个 CHECK 所需的有限 SQL 语法解析为可精确比较的 AST。"""

    def __init__(self, expression: str) -> None:
        """将受限 SQL 预先词法化，并把读取游标定位在首个 token。"""

        self._tokens = _tokenize_check_expression(expression)
        self._position = 0

    def parse(self) -> _CheckNode:
        """解析完整 CHECK 表达式并返回可跨方言比较的规范化 AST。

        可选的 ``CHECK(...)`` 外壳由反射结果决定；尾随 token、递归过深和未支持的
        语法均失败封闭，不能被当作等价约束接受。
        """

        try:
            if self._accept_word("CHECK"):
                self._expect_symbol("(")
                node = self._parse_or()
                self._expect_symbol(")")
            else:
                node = self._parse_or()
            if self._current().kind != "eof":
                raise _CheckParseError("trailing tokens")
            return _canonical_boolean(node)
        except RecursionError as exc:
            raise _CheckParseError("expression nesting limit exceeded") from exc

    def _parse_or(self) -> _CheckNode:
        """解析 OR 链，其子节点交由 AND 层保证运算优先级。"""

        children = [self._parse_and()]
        while self._accept_word("OR"):
            children.append(self._parse_and())
        return children[0] if len(children) == 1 else ("or", tuple(children))

    def _parse_and(self) -> _CheckNode:
        """解析 AND 链，并将每个子项限制为基础表达式。"""

        children = [self._parse_primary()]
        while self._accept_word("AND"):
            children.append(self._parse_primary())
        return children[0] if len(children) == 1 else ("and", tuple(children))

    def _parse_primary(self) -> _CheckNode:
        """优先识别谓词；失败后才回溯为括号分组，避免吞掉真实语法错误。"""

        checkpoint = self._position
        try:
            return self._parse_predicate()
        except _CheckParseError:
            self._position = checkpoint
        if self._accept_symbol("("):
            node = self._parse_or()
            self._expect_symbol(")")
            return node
        raise _CheckParseError("expected predicate")

    def _parse_predicate(self) -> _CheckNode:
        """解析允许的集合、比较和空值谓词，拒绝任意 SQL 运算符。"""

        left = self._parse_scalar()
        if self._accept_word("IN"):
            values = self._parse_string_list()
            return ("in", left, tuple(sorted(values, key=repr)))
        if self._accept_symbol("="):
            self._expect_word("ANY")
            self._expect_symbol("(")
            array = self._parse_scalar()
            self._expect_symbol(")")
            return ("in_any", left, array)
        if self._accept_symbol("!=") or self._accept_symbol("<>"):
            right = self._parse_scalar()
            return ("neq", left, right)
        if self._accept_word("IS"):
            negated = self._accept_word("NOT")
            self._expect_word("NULL")
            return ("is_not_null" if negated else "is_null", left)
        raise _CheckParseError("unsupported predicate")

    def _parse_string_list(self) -> tuple[_CheckNode, ...]:
        """解析 ``IN`` 右侧列表，具体项仍走标量规则以统一类型转换处理。"""

        self._expect_symbol("(")
        values: list[_CheckNode] = []
        while True:
            value = self._parse_scalar()
            values.append(value)
            if not self._accept_symbol(","):
                break
        self._expect_symbol(")")
        return tuple(values)

    def _parse_scalar(self) -> _CheckNode:
        """解析字符串、列、数组或括号标量，并继续吸收尾随显式类型转换。"""

        token = self._current()
        if token.kind == "string":
            self._position += 1
            node: _CheckNode = ("string", token.value)
        elif token.kind == "word" and token.value.upper() == "ARRAY":
            self._position += 1
            self._expect_symbol("[")
            values: list[_CheckNode] = []
            while True:
                value = self._parse_scalar()
                values.append(value)
                if not self._accept_symbol(","):
                    break
            self._expect_symbol("]")
            node = ("array", tuple(values))
        elif token.kind == "word":
            self._position += 1
            node = ("column", token.value.lower())
        elif self._accept_symbol("("):
            node = self._parse_scalar()
            self._expect_symbol(")")
        else:
            raise _CheckParseError("expected scalar")
        return self._parse_casts(node)

    def _parse_casts(self, node: _CheckNode) -> _CheckNode:
        """读取连续 ``::`` 转换，只放行迁移产生的已知 text 兼容形式。"""

        while self._accept_symbol("::"):
            if self._accept_word("CHARACTER"):
                self._expect_word("VARYING")
                type_name = "character varying"
            elif self._accept_word("TEXT"):
                type_name = "text"
            else:
                raise _CheckParseError("unsupported cast")
            array = False
            if self._accept_symbol("["):
                self._expect_symbol("]")
                array = True
            node = ("cast", node, type_name, array)
        return node

    def _current(self) -> _CheckToken:
        """返回当前 token；调用方负责在成功匹配后推进游标。"""

        return self._tokens[self._position]

    def _accept_word(self, value: str) -> bool:
        """大小写无关地消费一个关键字，匹配失败时保持游标不变。"""

        token = self._current()
        if token.kind == "word" and token.value.upper() == value:
            self._position += 1
            return True
        return False

    def _expect_word(self, value: str) -> None:
        """要求当前位置为指定关键字，否则终止受控语法解析。"""

        if not self._accept_word(value):
            raise _CheckParseError(f"expected {value}")

    def _accept_symbol(self, value: str) -> bool:
        """消费精确的标点或运算符；不把相似符号视作等价。"""

        token = self._current()
        if token.kind == "symbol" and token.value == value:
            self._position += 1
            return True
        return False

    def _expect_symbol(self, value: str) -> None:
        """要求当前位置为指定符号，缺失时报告一致的解析错误。"""

        if not self._accept_symbol(value):
            raise _CheckParseError(f"expected {value}")


# 子包外只经这些公开别名消费，底层对象名称保持与已发布 revision 一致。
CheckParseError = _CheckParseError
CheckExpressionParser = _CheckExpressionParser
EXPECTED_CHECK_SIGNATURES = _EXPECTED_CHECK_SIGNATURES
