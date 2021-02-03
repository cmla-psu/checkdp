import logging
from copy import deepcopy
from typing import Iterable, Tuple, Callable
import pycparser.c_ast as c_ast
import checkdp.transform.constants as constants
from checkdp.transform.utils import parse, NodeFinder
from checkdp.transform.typesystem import TypeSystem

logger = logging.getLogger(__name__)


class PostProcessor(c_ast.NodeVisitor):
    def __init__(self, type_system: TypeSystem, custom_variables: Iterable[str]):
        self._type_system = type_system
        self._sized_loop_level = 0
        self._sample_array_size_loops = 0
        self._sample_array_size_constants = 0
        self._parameters = None
        self._custom_varaibles = custom_variables

    def visit_FuncDef(self, node: c_ast.FuncDef) -> c_ast.FuncDef:
        self._parameters = tuple(decl.name for decl in node.decl.type.args.params)
        query = self._parameters[0]
        # if it is a dynamically tracked parameter, add new parameters
        # add distance variables for dynamically tracked parameters
        for name in self._parameters:
            *distances, _, _ = self._type_system.get_types(name)
            for index, distance in enumerate(distances):
                version = constants.ALIGNED_DISTANCE if index == 0 else constants.SHADOW_DISTANCE
                if distance == '*':
                    _, _, plain_type, is_array = self._type_system.get_types(name)
                    distance_variable = f'{plain_type} {version}_{name}'
                    distance_variable = distance_variable + '[]' if is_array else distance_variable
                    node.decl.type.args.params.append(parse(distance_variable))
                    # add the plain type to the type system
                    self._type_system.update_base_type(f'{version}_{name}', plain_type, is_array)
                    if name == query:
                        # only generate aligned distance for query variable
                        break

        # add sample array variable
        node.decl.type.args.params.append(parse(f'int {constants.SAMPLE_ARRAY}[]'))
        self._type_system.update_base_type(f'{constants.SAMPLE_ARRAY}', 'int', True)

        # add alignment array
        node.decl.type.args.params.append(parse(f'int {constants.ALIGNMENT_ARRAY}[]'))
        self._type_system.update_base_type(f'{constants.ALIGNMENT_ARRAY}', 'int', True)

        # add custom variables
        for hole in self._custom_varaibles:
            node.decl.type.args.params.append(parse(f'int {hole}'))

        # change the return type to int
        node.decl.type.type.type.names = ['int']

        self.generic_visit(node)

        return node

    def visit_While(self, node: c_ast.While):
        _, size, *_ = self._parameters
        finder = NodeFinder(lambda n: isinstance(n, c_ast.ID) and n.name == size)
        has_size_variable = len(finder.visit(node.cond)) != 0
        if has_size_variable:
            self._sized_loop_level += 1
        self.generic_visit(node)
        if has_size_variable:
            self._sized_loop_level -= 1

    def visit_Decl(self, node: c_ast.Decl):
        if isinstance(node.init, c_ast.ArrayRef) and node.init.name.name == constants.SAMPLE_ARRAY:
            if self._sized_loop_level == 0:
                self._sample_array_size_constants += 1
            else:
                self._sample_array_size_loops += self._sized_loop_level
        self.generic_visit(node)

    def process(self, node: c_ast.FuncDef) -> Tuple[c_ast.FuncDef, Callable[[int], int]]:
        if not isinstance(node, c_ast.FuncDef):
            raise TypeError('')
        # make a deepcopy of the node and do postprocess
        function = self.visit_FuncDef(deepcopy(node))
        return function, \
               lambda query_size: self._sample_array_size_loops * query_size + self._sample_array_size_constants
