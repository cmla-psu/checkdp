import logging
from copy import deepcopy
from typing import Union, Tuple, Iterable, Dict, Set
import pycparser.c_ast as c_ast
import re
import sympy as sp
from checkdp.transform.typesystem import TypeSystem
import checkdp.transform.constants as constants
from checkdp.transform.utils import generate, parse, expr_simplify

logger = logging.getLogger(__name__)


def parse_annotation(type_str: str, precondition_str: str, check_str: str):
    """Parse the annotation strings and return the results, with sanity checks.
    Raises ValueError if string is malformed"""
    # TODO - Enhancement: we can use PLY to construct a formal parser for the annotation, which will make it more
    # robust instead of regular expressions, and can also give position information about where the error is

    # remove white spaces and line breaks in the annotation strings
    space_re = re.compile(r'\s*')
    type_str, precondition_str, check_str = \
        space_re.sub('', type_str), space_re.sub('', precondition_str), space_re.sub('', check_str)

    # regular expression to extract all types
    initial_types = {
        variable: (aligned_distance, shadow_distance) for variable, aligned_distance, shadow_distance in
        map(lambda match: match.groups(), re.finditer(r'([a-zA-Z_]\w*):<([0*]),([0*])>(?:;)?', type_str))}

    try:
        preconditions = \
            [re.match(r'PRECONDITION:(ONE_DIFFER|ALL_DIFFER|DECREASING|INCREASING)', precondition_str).group(1)]
    except AttributeError:
        raise ValueError('Precondition annotation does not contain sensitivity information, specify with either '
                         'ONE_DIFFER or ALL_DIFFER')

    for assume in re.finditer(r'ASSUME\(([^()]*)\)', precondition_str):
        preconditions.append(assume.group(1))

    hole_preconditions = []
    for assume in re.finditer(r'ASSUME_HOLE\(([^()]*)\)', precondition_str):
        hole_preconditions.append(assume.group(1))

    try:
        check = re.match(r'CHECK:(?:\()?([^();]+)(?:\))?', check_str).group(1)
    except AttributeError:
        raise ValueError(f'Check annotation is invalid: {check_str}')

    return initial_types, preconditions, hole_preconditions, check


class Preprocessor(c_ast.NodeVisitor):
    """Preprocessor will do sanity checks about the source file node, and extract all annotations and create
    an initial type system"""
    def __init__(self, target_function: Union[str, None] = None):
        self._target_function = target_function
        self._type_system = TypeSystem()
        self._preconditions = None
        self._hole_preconditions = None
        self._goal = None
        # random variable scales, used to scale up the costs
        self._random_scales: Set[c_ast.FuncCall] = set()

    def visit_FileAST(self, node: c_ast.FileAST) -> c_ast.FuncDef:
        """Top level ast node"""
        # first get all FuncDef nodes
        function_nodes = tuple(filter(lambda child: isinstance(child, c_ast.FuncDef), node))

        if self._target_function:
            # if target function is specified, check if it exists
            target_function = next(filter(lambda child: child.decl.name == self._target_function, function_nodes))
            if not target_function:
                raise ValueError(f'Target function {self._target_function} not found in the source file')
        else:
            # else raise error if more than one function is defined in the source file
            if len(function_nodes) != 1:
                raise ValueError('Too many functions exist and target function is not specified, '
                                 'specify the function using -f or --function argument.')
            target_function = function_nodes[0]

        # only preprocess the target function
        return self.visit_FuncDef(target_function)

    def visit_FuncDef(self, node: c_ast.FuncDef) -> c_ast.FuncDef:
        # here we should have the target FuncDef node, do more sanity checks
        logger.info(f'Analyzing target function {node.decl.name}')

        # check the parameters
        if len(node.decl.type.args.params) < 3:
            raise ValueError('Function must at least have three parameters, (Queries, QuerySize, Epsilon)')

        query, size, epsilon, *_ = node.decl.type.args.params

        if not isinstance(query.type, c_ast.ArrayDecl):
            raise ValueError('First parameter must be query variable, i.e., an array of ints / floats')

        if not (isinstance(size.type, c_ast.TypeDecl) and size.type.type.names[0] == 'int'):
            raise ValueError('Second parameter must be size variable for the query, i.e., an int variable')

        if not isinstance(epsilon.type, c_ast.TypeDecl):
            raise ValueError('Third parameter must be privacy budget variable.')

        # first traverse the parameters
        self.generic_visit(node.decl)

        # check if annotation strings are present
        illegal_annotation = ('argument types', 'precondition', 'goal check')
        for index, annotation in enumerate(node.body.block_items[:3]):
            if not (isinstance(annotation, c_ast.Constant) and annotation.type == 'string'):
                raise ValueError(f'{illegal_annotation[index]} annotation is not specified, '
                                 f'annotations must be present in the first three lines '
                                 f'in the function as plain strings')

        # extract annotation strings
        initial_types, precondition, check = node.body.block_items[:3]
        types, self._preconditions, self._hole_preconditions, self._goal = parse_annotation(
            initial_types.value.replace("\"", ''), precondition.value.replace("\"", ''), check.value.replace("\"", ''))

        # check if all parameters are specified
        annotated_variables = set(types.keys())
        parameters = {decl.name for decl in node.decl.type.args.params} if node.decl.type.args else set()
        # annotation contains more than parameters
        extra_annotated = annotated_variables - parameters
        if len(extra_annotated) > 0:
            raise ValueError(f'Distance annotation can only contain parameters, '
                             f'but detected annotations for {extra_annotated}')
        # missing annotation for certain parameters
        missing_annotated = parameters - annotated_variables
        if len(missing_annotated) > 0:
            raise ValueError(f'Distance annotation for parameter {missing_annotated.pop()} is not given.')

        # update the distances in type system
        for name, (aligned, shadow) in types.items():
            self._type_system.update_distance(name, aligned, shadow)

        # remove the annotation nodes
        copied = deepcopy(node)
        copied.body.block_items = node.body.block_items[3:]
        self.generic_visit(copied.body)
        return copied

    def visit_Decl(self, node: c_ast.Decl):
        super().generic_visit(node)
        # variable name cannot contain CHECKDP
        if node.name.startswith(constants.PREFIX):
            raise ValueError(f'Variable name \'{node.name}\' cannot contain {constants.PREFIX} '
                             f'to avoid name collisions.')
        # check if variable type is supported and then update the type in type system
        if isinstance(node.type, (c_ast.TypeDecl, c_ast.ArrayDecl)):
            is_array = isinstance(node.type, c_ast.ArrayDecl)
            type_node = node.type.type.type if isinstance(node.type, c_ast.ArrayDecl) else node.type.type
            # only support primitive types
            if len(type_node.names) != 1:
                raise NotImplementedError(
                    f"Type {' '.join(type_node.names)} for variable {node.name} currently not supported.")
            if type_node.names[0] != 'int':
                logger.warning(f'Changing \'{generate(node)}\' to int declaration'
                               f' due to limitations of our symbolic executor KLEE. This may bring imprecision.')
                type_node.names[0] = 'int'

            self._type_system.update_base_type(node.name, type_node.names[0], is_array)
        # if it's noise generation
        if isinstance(node.init, c_ast.FuncCall) and node.init.name.name == constants.LAP:
            self._random_scales.add(node.init)

    def visit_FuncCall(self, node: c_ast.FuncCall):
        # function calls are not supported unless it's a noise generation call
        if node.name.name == constants.LAP or node.name.name == constants.OUTPUT:
            pass
        elif node.name.name.startswith(constants.PREFIX):
            # we can be more helpful here if the function call starts with const.PREFIX
            raise ValueError(f'{node.name.name} is not a CheckDP-provided intrinsics, '
                             f'valid functions are: {(constants.OUTPUT, )}')
        else:
            raise NotImplementedError('Function calls are not currently not supported')

    def visit_Return(self, node: c_ast.Return):
        raise ValueError(f'Please use {constants.OUTPUT} function instead of return statement for outputting.')

    def process(self, node: Union[c_ast.FileAST, c_ast.FuncDef]) \
            -> Tuple[c_ast.FuncDef, TypeSystem, Iterable[str], Iterable[str], str]:
        if not isinstance(node, (c_ast.FileAST, c_ast.FuncDef)):
            raise TypeError('node must be of type Union(FileAST, FuncDef)')
        processed = self.visit(node)
        # scale up the random noise scales due to limitations of symbolic executor KLEE only supporting integers
        all_scales = [f'1 / ({generate(func_call.args.exprs[0])})' for func_call in self._random_scales]
        lcm = sp.lcm(tuple(map(lambda scale: sp.fraction(scale)[1], all_scales + [self._goal])))
        # TODO: use less hackery method to tackle the Pow operation in sympy
        # see also https://stackoverflow.com/questions/14264431/expanding-algebraic-powers-in-python-sympy
        if str(lcm) != '1':
            logger.warning(f'Scaling down the noise scales by {lcm} due to limitations of symbolic executor KLEE '
                           f'only supporting integers, therefore the cost calculations will not contain divisions.')
            for scale in self._random_scales:
                scale.args.exprs[0] = parse(expr_simplify(f'({generate(scale.args.exprs[0])}) / ({lcm})'))
            logger.warning(f'Scaling up the final goal by {lcm} as well due to the cost scale-ups.')
            self._goal = expr_simplify(f'({self._goal}) * ({lcm})')
        return processed, self._type_system, self._preconditions, self._hole_preconditions, self._goal
