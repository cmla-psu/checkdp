from typing import Union, Sequence
import sympy as sp
from pycparser.c_parser import CParser
from pycparser.c_generator import CGenerator
from pycparser.plyparser import ParseError
import pycparser.c_ast as c_ast
import checkdp.transform.constants as constants
from checkdp.transform.typesystem import TypeSystem

__parser = CParser()
__generator = CGenerator()

ExprType = Union[c_ast.BinaryOp, c_ast.UnaryOp, c_ast.TernaryOp, c_ast.Constant, c_ast.ID, c_ast.FuncCall]
VariableType = Union[c_ast.ArrayRef, c_ast.ID]


def parse(content: str):
    try:
        return __parser.parse(content)
    except ParseError:
        # pycparser cannot parse expression directly, construct a FuncDef node for it to proceed
        return __parser.parse(f'int placeholder(){{{content};}}').ext[0].body.block_items[0]


def generate(node: c_ast.Node):
    return __generator.visit(node)


def expr_simplify(expr: str):
    """simplify the string expression by sympy's simplify method. sympy's method automatically simplifies
    multiplications to powers (x*x -> x**2) which is not supported by C. Therefore we use this utility function
    to wrap up the helper code.
    """
    def sack(exp):
        return exp.replace(
            lambda x: x.is_Pow and x.exp > 0,
            lambda x: sp.Symbol('*'.join([x.base.name] * x.exp))
        )
    return str(sack(sp.simplify(expr)))


def is_divergent(type_system: TypeSystem, condition: ExprType) -> Sequence[bool]:
    # if the condition contains star variable it means the aligned/shadow branch will diverge
    results = []
    for type_index in range(2):
        star_variable_finder = NodeFinder(
            lambda node: (isinstance(node, c_ast.ID) and type_system.get_types(node.name)[type_index] == '*'))
        results.append(len(star_variable_finder.visit(condition)) != 0)
    return results


class NodeFinder(c_ast.NodeVisitor):
    """ this class find a specific node in the expression"""
    def __init__(self, check_func, ignores=None):
        self._check_func = check_func
        self._ignores = ignores
        self._nodes = []

    def visit(self, node):
        if not node:
            return []
        self._nodes.clear()
        super().visit(node)
        return self._nodes

    def generic_visit(self, node):
        if self._ignores and self._ignores(node):
            return
        if self._check_func(node):
            self._nodes.append(node)
        for child in node:
            self.generic_visit(child)


class ExpressionReplacer(c_ast.NodeVisitor):
    """ this class returns the aligned or shadow version of an expression, e.g., returns e^aligned or e^shadow of e"""
    def __init__(self, types, is_aligned):
        self._types = types
        self._is_aligned = is_aligned

    def _replace(self, node):
        if not isinstance(node, (c_ast.ArrayRef, c_ast.ID)):
            raise NotImplementedError(f'Expression type {type(node)} currently not supported.')
        varname = node.name.name if isinstance(node, c_ast.ArrayRef) else node.name
        alignd, shadow, *_ = self._types.get_types(varname)
        distance = alignd if self._is_aligned else shadow
        if distance == '0':
            return node
        elif distance == '*':
            distance_varname = \
                f'{constants.ALIGNED_DISTANCE if self._is_aligned else constants.SHADOW_DISTANCE}_{varname}'
            distance_var = c_ast.ArrayRef(name=c_ast.ID(name=distance_varname), subscript=node.subscript) \
                if isinstance(node, c_ast.ArrayRef) else c_ast.ID(name=distance_varname)
            return c_ast.BinaryOp(op='+', left=node, right=distance_var)
        else:
            return c_ast.BinaryOp(op='+', left=node, right=parse(distance))

    def visit_BinaryOp(self, node):
        if isinstance(node.left, (c_ast.ArrayRef, c_ast.ID)):
            node.left = self._replace(node.left)
        else:
            self.visit(node.left)

        if isinstance(node.right, (c_ast.ArrayRef, c_ast.ID)):
            node.right = self._replace(node.right)
        else:
            self.visit(node.right)

    def visit_UnaryOp(self, node):
        if isinstance(node.expr, (c_ast.ArrayRef, c_ast.ID)):
            node.expr = self._replace(node.expr)
        else:
            self.visit(node.expr)

    def visit(self, node):
        super().visit(node)
        return node


class DistanceGenerator(c_ast.NodeVisitor):
    def __init__(self, types):
        self._types = types

    def try_simplify(self, expr):
        from sympy import simplify
        try:
            expr = str(simplify(expr))
        finally:
            return expr

    def generic_visit(self, node):
        # TODO: should handle cases like -(-(-(100)))
        raise NotImplementedError

    def visit_UnaryOp(self, node: c_ast.UnaryOp):
        if isinstance(node.expr, c_ast.Constant):
            return '0', '0'
        else:
            raise NotImplementedError

    def visit_Constant(self, n):
        return '0', '0'

    def visit_ID(self, n):
        align, shadow, *_ = self._types.get_types(n.name)
        align = f'({constants.ALIGNED_DISTANCE}_{n.name})' if align == '*' else align
        shadow = f'({constants.SHADOW_DISTANCE}_{n.name})' if shadow == '*' else shadow
        return align, shadow

    def visit_ArrayRef(self, n):
        varname, subscript = n.name.name, generate(n.subscript)
        align, shadow, *_ = self._types.get_types(n.name.name)
        align = f'({constants.ALIGNED_DISTANCE}_{varname}[{subscript}])' if align == '*' else align
        shadow = f'({constants.SHADOW_DISTANCE}_{varname}[{subscript}])' if shadow == '*' else shadow
        return align, shadow

    def visit_BinaryOp(self, n):
        return [self.try_simplify(f'{left} {n.op} {right}')
                for left, right in zip(self.visit(n.left), self.visit(n.right))]

