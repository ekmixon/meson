# Copyright 2014-2017 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import codecs
import textwrap
import types
import typing as T
from .mesonlib import MesonException
from . import mlog

if T.TYPE_CHECKING:
    from .ast import AstVisitor

# This is the regex for the supported escape sequences of a regular string
# literal, like 'abc\x00'
ESCAPE_SEQUENCE_SINGLE_RE = re.compile(r'''
    ( \\U[A-Fa-f0-9]{8}   # 8-digit hex escapes
    | \\u[A-Fa-f0-9]{4}   # 4-digit hex escapes
    | \\x[A-Fa-f0-9]{2}   # 2-digit hex escapes
    | \\[0-7]{1,3}        # Octal escapes
    | \\N\{[^}]+\}        # Unicode characters by name
    | \\[\\'abfnrtv]      # Single-character escapes
    )''', re.UNICODE | re.VERBOSE)

class MesonUnicodeDecodeError(MesonException):
    def __init__(self, match: str) -> None:
        super().__init__(match)
        self.match = match

def decode_match(match: T.Match[str]) -> str:
    try:
        return codecs.decode(match.group(0).encode(), 'unicode_escape')
    except UnicodeDecodeError:
        raise MesonUnicodeDecodeError(match.group(0))

class ParseException(MesonException):
    def __init__(self, text: str, line: str, lineno: int, colno: int) -> None:
        # Format as error message, followed by the line with the error, followed by a caret to show the error column.
        super().__init__(f"{text}\n{line}\n{' ' * colno}^")
        self.lineno = lineno
        self.colno = colno

class BlockParseException(MesonException):
    def __init__(
                self,
                text: str,
                line: str,
                lineno: int,
                colno: int,
                start_line: str,
                start_lineno: int,
                start_colno: int,
            ) -> None:
        # This can be formatted in two ways - one if the block start and end are on the same line, and a different way if they are on different lines.

        if lineno == start_lineno:
            # If block start and end are on the same line, it is formatted as:
            # Error message
            # Followed by the line with the error
            # Followed by a caret to show the block start
            # Followed by underscores
            # Followed by a caret to show the block end.
            super().__init__(
                f"{text}\n{line}\n{' ' * start_colno}^{'_' * (colno - start_colno - 1)}^"
            )

        else:
            # If block start and end are on different lines, it is formatted as:
            # Error message
            # Followed by the line with the error
            # Followed by a caret to show the error column.
            # Followed by a message saying where the block started.
            # Followed by the line of the block start.
            # Followed by a caret for the block start.
            super().__init__(
                "%s\n%s\n%s\nFor a block that started at %d,%d\n%s\n%s"
                % (
                    text,
                    line,
                    f"{' ' * colno}^",
                    start_lineno,
                    start_colno,
                    start_line,
                    f"{' ' * start_colno}^",
                )
            )

        self.lineno = lineno
        self.colno = colno

TV_TokenTypes = T.TypeVar('TV_TokenTypes', int, str, bool)

class Token(T.Generic[TV_TokenTypes]):
    def __init__(self, tid: str, filename: str, line_start: int, lineno: int, colno: int, bytespan: T.Tuple[int, int], value: TV_TokenTypes):
        self.tid = tid                # type: str
        self.filename = filename      # type: str
        self.line_start = line_start  # type: int
        self.lineno = lineno          # type: int
        self.colno = colno            # type: int
        self.bytespan = bytespan      # type: T.Tuple[int, int]
        self.value = value            # type: TV_TokenTypes

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.tid == other
        elif isinstance(other, Token):
            return self.tid == other.tid
        return NotImplemented

class Lexer:
    def __init__(self, code: str):
        self.code = code
        self.keywords = {'true', 'false', 'if', 'else', 'elif',
                         'endif', 'and', 'or', 'not', 'foreach', 'endforeach',
                         'in', 'continue', 'break'}
        self.future_keywords = {'return'}
        self.token_specification = [
            # Need to be sorted longest to shortest.
            ('ignore', re.compile(r'[ \t]')),
            ('fstring', re.compile(r"f'([^'\\]|(\\.))*'")),
            ('id', re.compile('[_a-zA-Z][_0-9a-zA-Z]*')),
            ('number', re.compile(r'0[bB][01]+|0[oO][0-7]+|0[xX][0-9a-fA-F]+|0|[1-9]\d*')),
            ('eol_cont', re.compile(r'\\\n')),
            ('eol', re.compile(r'\n')),
            ('multiline_string', re.compile(r"'''(.|\n)*?'''", re.M)),
            ('comment', re.compile(r'#.*')),
            ('lparen', re.compile(r'\(')),
            ('rparen', re.compile(r'\)')),
            ('lbracket', re.compile(r'\[')),
            ('rbracket', re.compile(r'\]')),
            ('lcurl', re.compile(r'\{')),
            ('rcurl', re.compile(r'\}')),
            ('dblquote', re.compile(r'"')),
            ('string', re.compile(r"'([^'\\]|(\\.))*'")),
            ('comma', re.compile(r',')),
            ('plusassign', re.compile(r'\+=')),
            ('dot', re.compile(r'\.')),
            ('plus', re.compile(r'\+')),
            ('dash', re.compile(r'-')),
            ('star', re.compile(r'\*')),
            ('percent', re.compile(r'%')),
            ('fslash', re.compile(r'/')),
            ('colon', re.compile(r':')),
            ('equal', re.compile(r'==')),
            ('nequal', re.compile(r'!=')),
            ('assign', re.compile(r'=')),
            ('le', re.compile(r'<=')),
            ('lt', re.compile(r'<')),
            ('ge', re.compile(r'>=')),
            ('gt', re.compile(r'>')),
            ('questionmark', re.compile(r'\?')),
        ]

    def getline(self, line_start: int) -> str:
        return self.code[line_start:self.code.find('\n', line_start)]

    def lex(self, filename: str) -> T.Generator[Token, None, None]:
        line_start = 0
        lineno = 1
        loc = 0
        par_count = 0
        bracket_count = 0
        curl_count = 0
        col = 0
        while loc < len(self.code):
            matched = False
            value = None  # type: T.Union[str, bool, int]
            for (tid, reg) in self.token_specification:
                if mo := reg.match(self.code, loc):
                    curline = lineno
                    curline_start = line_start
                    col = mo.start() - line_start
                    matched = True
                    span_start = loc
                    loc = mo.end()
                    span_end = loc
                    bytespan = (span_start, span_end)
                    match_text = mo.group()
                    if tid in ['ignore', 'comment']:
                        break
                    elif tid == 'lparen':
                        par_count += 1
                    elif tid == 'rparen':
                        par_count -= 1
                    elif tid == 'lbracket':
                        bracket_count += 1
                    elif tid == 'rbracket':
                        bracket_count -= 1
                    elif tid == 'lcurl':
                        curl_count += 1
                    elif tid == 'rcurl':
                        curl_count -= 1
                    elif tid == 'dblquote':
                        raise ParseException('Double quotes are not supported. Use single quotes.', self.getline(line_start), lineno, col)
                    elif tid in {'string', 'fstring'}:
                        # Handle here and not on the regexp to give a better error message.
                        if match_text.find("\n") != -1:
                            mlog.warning(textwrap.dedent("""\
                                    Newline character in a string detected, use ''' (three single quotes) for multiline strings instead.
                                    This will become a hard error in a future Meson release.\
                                """),
                                self.getline(line_start),
                                str(lineno),
                                str(col)
                            )
                        value = match_text[2 if tid == 'fstring' else 1:-1]
                        try:
                            value = ESCAPE_SEQUENCE_SINGLE_RE.sub(decode_match, value)
                        except MesonUnicodeDecodeError as err:
                            raise MesonException(f"Failed to parse escape sequence: '{err.match}' in string:\n  {match_text}")
                    elif tid == 'multiline_string':
                        tid = 'string'
                        value = match_text[3:-3]
                        lines = match_text.split('\n')
                        if len(lines) > 1:
                            lineno += len(lines) - 1
                            line_start = mo.end() - len(lines[-1])
                    elif tid == 'number':
                        value = int(match_text, base=0)
                    elif tid == 'eol_cont':
                        lineno += 1
                        line_start = loc
                        break
                    elif tid == 'eol':
                        lineno += 1
                        line_start = loc
                        if par_count > 0 or bracket_count > 0 or curl_count > 0:
                            break
                    elif tid == 'id':
                        if match_text in self.keywords:
                            tid = match_text
                        else:
                            if match_text in self.future_keywords:
                                mlog.warning(f"Identifier '{match_text}' will become a reserved keyword in a future release. Please rename it.",
                                             location=types.SimpleNamespace(filename=filename, lineno=lineno))
                            value = match_text
                    yield Token(tid, filename, curline_start, curline, col, bytespan, value)
                    break
            if not matched:
                raise ParseException('lexer', self.getline(line_start), lineno, col)

class BaseNode:
    def __init__(self, lineno: int, colno: int, filename: str, end_lineno: T.Optional[int] = None, end_colno: T.Optional[int] = None):
        self.lineno = lineno      # type: int
        self.colno = colno        # type: int
        self.filename = filename  # type: str
        self.end_lineno = end_lineno if end_lineno is not None else self.lineno
        self.end_colno = end_colno if end_colno is not None else self.colno

        # Attributes for the visitors
        self.level = 0            # type: int
        self.ast_id = ''          # type: str
        self.condition_level = 0  # type: int

    def accept(self, visitor: 'AstVisitor') -> None:
        fname = f'visit_{type(self).__name__}'
        if hasattr(visitor, fname):
            func = getattr(visitor, fname)
            if callable(func):
                func(self)

class ElementaryNode(T.Generic[TV_TokenTypes], BaseNode):
    def __init__(self, token: Token[TV_TokenTypes]):
        super().__init__(token.lineno, token.colno, token.filename)
        self.value = token.value        # type: TV_TokenTypes
        self.bytespan = token.bytespan  # type: T.Tuple[int, int]

class BooleanNode(ElementaryNode[bool]):
    def __init__(self, token: Token[bool]):
        super().__init__(token)
        assert isinstance(self.value, bool)

class IdNode(ElementaryNode[str]):
    def __init__(self, token: Token[str]):
        super().__init__(token)
        assert isinstance(self.value, str)

    def __str__(self) -> str:
        return "Id node: '%s' (%d, %d)." % (self.value, self.lineno, self.colno)

class NumberNode(ElementaryNode[int]):
    def __init__(self, token: Token[int]):
        super().__init__(token)
        assert isinstance(self.value, int)

class StringNode(ElementaryNode[str]):
    def __init__(self, token: Token[str]):
        super().__init__(token)
        assert isinstance(self.value, str)

    def __str__(self) -> str:
        return "String node: '%s' (%d, %d)." % (self.value, self.lineno, self.colno)

class FormatStringNode(ElementaryNode[str]):
    def __init__(self, token: Token[str]):
        super().__init__(token)
        assert isinstance(self.value, str)

    def __str__(self) -> str:
        return "Format string node: '{self.value}' ({self.lineno}, {self.colno})."

class ContinueNode(ElementaryNode):
    pass

class BreakNode(ElementaryNode):
    pass

class ArgumentNode(BaseNode):
    def __init__(self, token: Token[TV_TokenTypes]):
        super().__init__(token.lineno, token.colno, token.filename)
        self.arguments = []  # type: T.List[BaseNode]
        self.commas = []     # type: T.List[Token[TV_TokenTypes]]
        self.kwargs = {}     # type: T.Dict[BaseNode, BaseNode]
        self.order_error = False

    def prepend(self, statement: BaseNode) -> None:
        if self.num_kwargs() > 0:
            self.order_error = True
        if not isinstance(statement, EmptyNode):
            self.arguments = [statement] + self.arguments

    def append(self, statement: BaseNode) -> None:
        if self.num_kwargs() > 0:
            self.order_error = True
        if not isinstance(statement, EmptyNode):
            self.arguments += [statement]

    def set_kwarg(self, name: IdNode, value: BaseNode) -> None:
        if any((isinstance(x, IdNode) and name.value == x.value) for x in self.kwargs):
            mlog.warning(f'Keyword argument "{name.value}" defined multiple times.', location=self)
            mlog.warning('This will be an error in future Meson releases.')
        self.kwargs[name] = value

    def set_kwarg_no_check(self, name: BaseNode, value: BaseNode) -> None:
        self.kwargs[name] = value

    def num_args(self) -> int:
        return len(self.arguments)

    def num_kwargs(self) -> int:
        return len(self.kwargs)

    def incorrect_order(self) -> bool:
        return self.order_error

    def __len__(self) -> int:
        return self.num_args() # Fixme

class ArrayNode(BaseNode):
    def __init__(self, args: ArgumentNode, lineno: int, colno: int, end_lineno: int, end_colno: int):
        super().__init__(lineno, colno, args.filename, end_lineno=end_lineno, end_colno=end_colno)
        self.args = args              # type: ArgumentNode

class DictNode(BaseNode):
    def __init__(self, args: ArgumentNode, lineno: int, colno: int, end_lineno: int, end_colno: int):
        super().__init__(lineno, colno, args.filename, end_lineno=end_lineno, end_colno=end_colno)
        self.args = args

class EmptyNode(BaseNode):
    def __init__(self, lineno: int, colno: int, filename: str):
        super().__init__(lineno, colno, filename)
        self.value = None

class OrNode(BaseNode):
    def __init__(self, left: BaseNode, right: BaseNode):
        super().__init__(left.lineno, left.colno, left.filename)
        self.left = left    # type: BaseNode
        self.right = right  # type: BaseNode

class AndNode(BaseNode):
    def __init__(self, left: BaseNode, right: BaseNode):
        super().__init__(left.lineno, left.colno, left.filename)
        self.left = left    # type: BaseNode
        self.right = right  # type: BaseNode

class ComparisonNode(BaseNode):
    def __init__(self, ctype: str, left: BaseNode, right: BaseNode):
        super().__init__(left.lineno, left.colno, left.filename)
        self.left = left    # type: BaseNode
        self.right = right  # type: BaseNode
        self.ctype = ctype  # type: str

class ArithmeticNode(BaseNode):
    def __init__(self, operation: str, left: BaseNode, right: BaseNode):
        super().__init__(left.lineno, left.colno, left.filename)
        self.left = left            # type: BaseNode
        self.right = right          # type: BaseNode
        self.operation = operation  # type: str

class NotNode(BaseNode):
    def __init__(self, token: Token[TV_TokenTypes], value: BaseNode):
        super().__init__(token.lineno, token.colno, token.filename)
        self.value = value  # type: BaseNode

class CodeBlockNode(BaseNode):
    def __init__(self, token: Token[TV_TokenTypes]):
        super().__init__(token.lineno, token.colno, token.filename)
        self.lines = []  # type: T.List[BaseNode]

class IndexNode(BaseNode):
    def __init__(self, iobject: BaseNode, index: BaseNode):
        super().__init__(iobject.lineno, iobject.colno, iobject.filename)
        self.iobject = iobject  # type: BaseNode
        self.index = index      # type: BaseNode

class MethodNode(BaseNode):
    def __init__(self, filename: str, lineno: int, colno: int, source_object: BaseNode, name: str, args: ArgumentNode):
        super().__init__(lineno, colno, filename)
        self.source_object = source_object  # type: BaseNode
        self.name = name                    # type: str
        assert isinstance(self.name, str)
        self.args = args                    # type: ArgumentNode

class FunctionNode(BaseNode):
    def __init__(self, filename: str, lineno: int, colno: int, end_lineno: int, end_colno: int, func_name: str, args: ArgumentNode):
        super().__init__(lineno, colno, filename, end_lineno=end_lineno, end_colno=end_colno)
        self.func_name = func_name  # type: str
        assert isinstance(func_name, str)
        self.args = args  # type: ArgumentNode

class AssignmentNode(BaseNode):
    def __init__(self, filename: str, lineno: int, colno: int, var_name: str, value: BaseNode):
        super().__init__(lineno, colno, filename)
        self.var_name = var_name  # type: str
        assert isinstance(var_name, str)
        self.value = value  # type: BaseNode

class PlusAssignmentNode(BaseNode):
    def __init__(self, filename: str, lineno: int, colno: int, var_name: str, value: BaseNode):
        super().__init__(lineno, colno, filename)
        self.var_name = var_name  # type: str
        assert isinstance(var_name, str)
        self.value = value  # type: BaseNode

class ForeachClauseNode(BaseNode):
    def __init__(self, token: Token, varnames: T.List[str], items: BaseNode, block: CodeBlockNode):
        super().__init__(token.lineno, token.colno, token.filename)
        self.varnames = varnames  # type: T.List[str]
        self.items = items        # type: BaseNode
        self.block = block        # type: CodeBlockNode

class IfNode(BaseNode):
    def __init__(self, linenode: BaseNode, condition: BaseNode, block: CodeBlockNode):
        super().__init__(linenode.lineno, linenode.colno, linenode.filename)
        self.condition = condition  # type: BaseNode
        self.block = block          # type: CodeBlockNode

class IfClauseNode(BaseNode):
    def __init__(self, linenode: BaseNode):
        super().__init__(linenode.lineno, linenode.colno, linenode.filename)
        self.ifs = []          # type: T.List[IfNode]
        self.elseblock = None  # type: T.Union[EmptyNode, CodeBlockNode]

class UMinusNode(BaseNode):
    def __init__(self, current_location: Token, value: BaseNode):
        super().__init__(current_location.lineno, current_location.colno, current_location.filename)
        self.value = value  # type: BaseNode

class TernaryNode(BaseNode):
    def __init__(self, condition: BaseNode, trueblock: BaseNode, falseblock: BaseNode):
        super().__init__(condition.lineno, condition.colno, condition.filename)
        self.condition = condition    # type: BaseNode
        self.trueblock = trueblock    # type: BaseNode
        self.falseblock = falseblock  # type: BaseNode

comparison_map = {'equal': '==',
                  'nequal': '!=',
                  'lt': '<',
                  'le': '<=',
                  'gt': '>',
                  'ge': '>=',
                  'in': 'in',
                  'notin': 'not in',
                  }

# Recursive descent parser for Meson's definition language.
# Very basic apart from the fact that we have many precedence
# levels so there are not enough words to describe them all.
# Enter numbering:
#
# 1 assignment
# 2 or
# 3 and
# 4 comparison
# 5 arithmetic
# 6 negation
# 7 funcall, method call
# 8 parentheses
# 9 plain token

class Parser:
    def __init__(self, code: str, filename: str):
        self.lexer = Lexer(code)
        self.stream = self.lexer.lex(filename)
        self.current = Token('eof', '', 0, 0, 0, (0, 0), None)  # type: Token
        self.getsym()
        self.in_ternary = False

    def getsym(self) -> None:
        try:
            self.current = next(self.stream)
        except StopIteration:
            self.current = Token('eof', '', self.current.line_start, self.current.lineno, self.current.colno + self.current.bytespan[1] - self.current.bytespan[0], (0, 0), None)

    def getline(self) -> str:
        return self.lexer.getline(self.current.line_start)

    def accept(self, s: str) -> bool:
        if self.current.tid == s:
            self.getsym()
            return True
        return False

    def accept_any(self, tids: T.Sequence[str]) -> str:
        tid = self.current.tid
        if tid in tids:
            self.getsym()
            return tid
        return ''

    def expect(self, s: str) -> bool:
        if self.accept(s):
            return True
        raise ParseException(f'Expecting {s} got {self.current.tid}.', self.getline(), self.current.lineno, self.current.colno)

    def block_expect(self, s: str, block_start: Token) -> bool:
        if self.accept(s):
            return True
        raise BlockParseException(f'Expecting {s} got {self.current.tid}.', self.getline(), self.current.lineno, self.current.colno, self.lexer.getline(block_start.line_start), block_start.lineno, block_start.colno)

    def parse(self) -> CodeBlockNode:
        block = self.codeblock()
        self.expect('eof')
        return block

    def statement(self) -> BaseNode:
        return self.e1()

    def e1(self) -> BaseNode:
        left = self.e2()
        if self.accept('plusassign'):
            value = self.e1()
            if not isinstance(left, IdNode):
                raise ParseException('Plusassignment target must be an id.', self.getline(), left.lineno, left.colno)
            assert isinstance(left.value, str)
            return PlusAssignmentNode(left.filename, left.lineno, left.colno, left.value, value)
        elif self.accept('assign'):
            value = self.e1()
            if not isinstance(left, IdNode):
                raise ParseException('Assignment target must be an id.',
                                     self.getline(), left.lineno, left.colno)
            assert isinstance(left.value, str)
            return AssignmentNode(left.filename, left.lineno, left.colno, left.value, value)
        elif self.accept('questionmark'):
            if self.in_ternary:
                raise ParseException('Nested ternary operators are not allowed.',
                                     self.getline(), left.lineno, left.colno)
            self.in_ternary = True
            trueblock = self.e1()
            self.expect('colon')
            falseblock = self.e1()
            self.in_ternary = False
            return TernaryNode(left, trueblock, falseblock)
        return left

    def e2(self) -> BaseNode:
        left = self.e3()
        while self.accept('or'):
            if isinstance(left, EmptyNode):
                raise ParseException('Invalid or clause.',
                                     self.getline(), left.lineno, left.colno)
            left = OrNode(left, self.e3())
        return left

    def e3(self) -> BaseNode:
        left = self.e4()
        while self.accept('and'):
            if isinstance(left, EmptyNode):
                raise ParseException('Invalid and clause.',
                                     self.getline(), left.lineno, left.colno)
            left = AndNode(left, self.e4())
        return left

    def e4(self) -> BaseNode:
        left = self.e5()
        for nodename, operator_type in comparison_map.items():
            if self.accept(nodename):
                return ComparisonNode(operator_type, left, self.e5())
        if self.accept('not') and self.accept('in'):
            return ComparisonNode('notin', left, self.e5())
        return left

    def e5(self) -> BaseNode:
        return self.e5addsub()

    def e5addsub(self) -> BaseNode:
        op_map = {
            'plus': 'add',
            'dash': 'sub',
        }
        left = self.e5muldiv()
        while True:
            if op := self.accept_any(tuple(op_map.keys())):
                left = ArithmeticNode(op_map[op], left, self.e5muldiv())
            else:
                break
        return left

    def e5muldiv(self) -> BaseNode:
        op_map = {
            'percent': 'mod',
            'star': 'mul',
            'fslash': 'div',
        }
        left = self.e6()
        while True:
            if op := self.accept_any(tuple(op_map.keys())):
                left = ArithmeticNode(op_map[op], left, self.e6())
            else:
                break
        return left

    def e6(self) -> BaseNode:
        if self.accept('not'):
            return NotNode(self.current, self.e7())
        if self.accept('dash'):
            return UMinusNode(self.current, self.e7())
        return self.e7()

    def e7(self) -> BaseNode:
        left = self.e8()
        block_start = self.current
        if self.accept('lparen'):
            args = self.args()
            self.block_expect('rparen', block_start)
            if not isinstance(left, IdNode):
                raise ParseException('Function call must be applied to plain id',
                                     self.getline(), left.lineno, left.colno)
            assert isinstance(left.value, str)
            left = FunctionNode(left.filename, left.lineno, left.colno, self.current.lineno, self.current.colno, left.value, args)
        go_again = True
        while go_again:
            go_again = False
            if self.accept('dot'):
                go_again = True
                left = self.method_call(left)
            if self.accept('lbracket'):
                go_again = True
                left = self.index_call(left)
        return left

    def e8(self) -> BaseNode:
        block_start = self.current
        if self.accept('lparen'):
            e = self.statement()
            self.block_expect('rparen', block_start)
            return e
        elif self.accept('lbracket'):
            args = self.args()
            self.block_expect('rbracket', block_start)
            return ArrayNode(args, block_start.lineno, block_start.colno, self.current.lineno, self.current.colno)
        elif self.accept('lcurl'):
            key_values = self.key_values()
            self.block_expect('rcurl', block_start)
            return DictNode(key_values, block_start.lineno, block_start.colno, self.current.lineno, self.current.colno)
        else:
            return self.e9()

    def e9(self) -> BaseNode:
        t = self.current
        if self.accept('true'):
            t.value = True
            return BooleanNode(t)
        if self.accept('false'):
            t.value = False
            return BooleanNode(t)
        if self.accept('id'):
            return IdNode(t)
        if self.accept('number'):
            return NumberNode(t)
        if self.accept('string'):
            return StringNode(t)
        if self.accept('fstring'):
            return FormatStringNode(t)
        return EmptyNode(self.current.lineno, self.current.colno, self.current.filename)

    def key_values(self) -> ArgumentNode:
        s = self.statement()  # type: BaseNode
        a = ArgumentNode(self.current)

        while not isinstance(s, EmptyNode):
            if not self.accept('colon'):
                raise ParseException('Only key:value pairs are valid in dict construction.',
                                     self.getline(), s.lineno, s.colno)
            a.set_kwarg_no_check(s, self.statement())
            potential = self.current
            if not self.accept('comma'):
                return a
            a.commas.append(potential)
            s = self.statement()
        return a

    def args(self) -> ArgumentNode:
        s = self.statement()  # type: BaseNode
        a = ArgumentNode(self.current)

        while not isinstance(s, EmptyNode):
            potential = self.current
            if self.accept('comma'):
                a.commas.append(potential)
                a.append(s)
            elif self.accept('colon'):
                if not isinstance(s, IdNode):
                    raise ParseException('Dictionary key must be a plain identifier.',
                                         self.getline(), s.lineno, s.colno)
                a.set_kwarg(s, self.statement())
                potential = self.current
                if not self.accept('comma'):
                    return a
                a.commas.append(potential)
            else:
                a.append(s)
                return a
            s = self.statement()
        return a

    def method_call(self, source_object: BaseNode) -> MethodNode:
        methodname = self.e9()
        if not isinstance(methodname, IdNode):
            raise ParseException('Method name must be plain id',
                                 self.getline(), self.current.lineno, self.current.colno)
        assert isinstance(methodname.value, str)
        self.expect('lparen')
        args = self.args()
        self.expect('rparen')
        method = MethodNode(methodname.filename, methodname.lineno, methodname.colno, source_object, methodname.value, args)
        return self.method_call(method) if self.accept('dot') else method

    def index_call(self, source_object: BaseNode) -> IndexNode:
        index_statement = self.statement()
        self.expect('rbracket')
        return IndexNode(source_object, index_statement)

    def foreachblock(self) -> ForeachClauseNode:
        t = self.current
        self.expect('id')
        assert isinstance(t.value, str)
        varname = t
        varnames = [t.value]  # type: T.List[str]

        if self.accept('comma'):
            t = self.current
            self.expect('id')
            assert isinstance(t.value, str)
            varnames.append(t.value)

        self.expect('colon')
        items = self.statement()
        block = self.codeblock()
        return ForeachClauseNode(varname, varnames, items, block)

    def ifblock(self) -> IfClauseNode:
        condition = self.statement()
        clause = IfClauseNode(condition)
        self.expect('eol')
        block = self.codeblock()
        clause.ifs.append(IfNode(clause, condition, block))
        self.elseifblock(clause)
        clause.elseblock = self.elseblock()
        return clause

    def elseifblock(self, clause: IfClauseNode) -> None:
        while self.accept('elif'):
            s = self.statement()
            self.expect('eol')
            b = self.codeblock()
            clause.ifs.append(IfNode(s, s, b))

    def elseblock(self) -> T.Union[CodeBlockNode, EmptyNode]:
        if self.accept('else'):
            self.expect('eol')
            return self.codeblock()
        return EmptyNode(self.current.lineno, self.current.colno, self.current.filename)

    def line(self) -> BaseNode:
        block_start = self.current
        if self.current == 'eol':
            return EmptyNode(self.current.lineno, self.current.colno, self.current.filename)
        if self.accept('if'):
            ifblock = self.ifblock()
            self.block_expect('endif', block_start)
            return ifblock
        if self.accept('foreach'):
            forblock = self.foreachblock()
            self.block_expect('endforeach', block_start)
            return forblock
        if self.accept('continue'):
            return ContinueNode(self.current)
        return BreakNode(self.current) if self.accept('break') else self.statement()

    def codeblock(self) -> CodeBlockNode:
        block = CodeBlockNode(self.current)
        cond = True
        while cond:
            curline = self.line()
            if not isinstance(curline, EmptyNode):
                block.lines.append(curline)
            cond = self.accept('eol')
        return block
