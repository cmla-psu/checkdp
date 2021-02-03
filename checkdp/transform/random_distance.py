from typing import Dict, Set, Tuple, Sequence, List, Optional
from enum import Enum
import logging
from queue import Queue
import pycparser.c_ast as c_ast
import checkdp.transform.constants as constants
from checkdp.transform.utils import NodeFinder, ExprType, VariableType, generate
from checkdp.transform.typesystem import TypeSystem
from checkdp.transform.utils import DistanceGenerator, parse


logger = logging.getLogger(__name__)


class AlignmentIndexType(Enum):
    Variable = 0
    Constant = 1
    Selector = 2


def _generate_random_distance(conditions: Sequence[str], variables: Sequence[str],
                              alignment_array_types: List[AlignmentIndexType], is_selector=False) -> str:
    start_index = len(alignment_array_types)
    if len(conditions) == 0:
        template_parts = [f'{constants.ALIGNMENT_ARRAY}[{start_index}]']
        if is_selector:
            alignment_array_types.append(AlignmentIndexType.Selector)
        else:
            alignment_array_types.append(AlignmentIndexType.Constant)
        for index, variable in enumerate(variables):
            template_parts.append(f'{constants.ALIGNMENT_ARRAY}[{start_index + 1 + index}] * {variable}')
            alignment_array_types.append(AlignmentIndexType.Variable)

        return '(' + ' + '.join(template_parts) + ')'
    left = _generate_random_distance(conditions[1:], variables, alignment_array_types, is_selector)
    right = _generate_random_distance(conditions[1:], variables, alignment_array_types, is_selector)
    return f'{conditions[0]} ? {left} : {right}'


class RandomDistanceGenerator(c_ast.NodeVisitor):
    def __init__(self, type_system: TypeSystem, enable_shadow: bool = False):
        self._type_system = type_system
        # keep a dictionary from variable name -> set of variable names it depends on
        self._depends: Dict[str, Set[VariableType]] = {}
        # the live variable dictionary, random variable -> live variable set, this is used to remove some variables
        # in the final template generation
        self._live_variable: Dict[str, Set[str]] = {}
        # the template for each random variable, random variable -> (condition set, dependent variable set)
        # this is used to generate the final template
        self._templates: Dict[str, Tuple[Set[str], Set[str]]] = {}
        self._random_variables: Set[str] = set()
        self._visited_assertions: Set[c_ast.FuncCall] = set()
        self._enable_shadow = enable_shadow

    def _all_depends(self, expression: ExprType):
        dependencies = set()
        visited = set()
        to_visit: Queue = Queue()

        for node in NodeFinder(lambda n: isinstance(n, (c_ast.ID, c_ast.ArrayRef))).visit(expression):
            if constants.PREFIX not in generate(node):
                to_visit.put(node)

        while not to_visit.empty():
            visit_node = to_visit.get()
            if visit_node not in visited:
                visited.add(visit_node)
                dependencies.add(visit_node)

                # add its dependencies to to_visit queue
                name = generate(visit_node)
                if name in self._depends:
                    for visit_dependent in self._depends[name]:
                        to_visit.put(visit_dependent)

        return dependencies

    def _assignment(self, left: str, right: ExprType):
        # ignore generated statements
        if constants.PREFIX in left:
            return
        # for noise generation statement
        if isinstance(right, c_ast.ArrayRef) and right.name.name == constants.SAMPLE_ARRAY:
            # create empty set for this random variable
            self._templates[left] = (set(), set())
            # create the live variable set for this random variable
            self._depends[left] = set()
            self._live_variable[left] = set(self._depends.keys())
            self._random_variables.add(left)
            return

        finder = NodeFinder(lambda n: isinstance(n, (c_ast.ID, c_ast.ArrayRef)))
        self._depends[left] = set(n for n in finder.visit(right))

    def _add_dependencies(self, expression: ExprType, if_cond: Optional[ExprType]):
        dependencies = self._all_depends(expression)
        for eta in self._templates.keys():
            liveness_checker = NodeFinder(
                lambda n: (isinstance(n, c_ast.ID) and n.name in self._live_variable[eta])
            )
            id_count_checker = NodeFinder(lambda n: isinstance(n, c_ast.ID))

            dependency_strings = tuple(map(lambda n: generate(n), dependencies))
            # add to E and V set if it is generated if the assertion depends on the random variable
            if eta in dependency_strings:
                # add to E set if it is generated by if command and the dependencies are alive
                if if_cond and all(len(liveness_checker.visit(dependency)) == len(id_count_checker.visit(dependency))
                                   for dependency in dependencies):
                    self._templates[eta][0].add(generate(if_cond))

                # add variable to V set, only if the dependency is alive at the line of random variable generation
                for variable_node in dependencies:
                    variable_name = \
                        variable_node.name if isinstance(variable_node, c_ast.ID) else variable_node.name.name
                    # the variable must be alive
                    if len(liveness_checker.visit(variable_node)) != len(id_count_checker.visit(variable_node)):
                        continue

                    # it cannot be a random variable
                    if variable_node.name in self._random_variables:
                        continue

                    # ignore plain reference to array variable, i.e., without subscript
                    # this means the assertion depends on the array, but no specific index is given, therefore
                    # should not be generated in the template
                    _, _, _, is_array = self._type_system.get_types(variable_name)
                    if is_array and isinstance(variable_node, c_ast.ID):
                        continue
                    # the variable must be dynamically-tracked
                    aligned_distance = self._type_system.get_types(variable_name)[0]
                    if aligned_distance == '*' or constants.ALIGNED_DISTANCE in aligned_distance:
                        self._templates[eta][1].add(generate(variable_node))

    def visit_Assignment(self, node: c_ast.Assignment):
        self._assignment(node.lvalue.name, node.rvalue)

    def visit_Decl(self, node: c_ast.Decl):
        # ignore function declaration
        if isinstance(node.type, c_ast.FuncDecl):
            self.generic_visit(node)
            return
        self._assignment(node.name, node.init)

    def visit_FuncCall(self, node: c_ast.FuncCall):
        if node.name.name == constants.OUTPUT:
            self._add_dependencies(node.args.exprs[0], None)

    def visit_If(self, node: c_ast.If):
        # ignore generated shadow branch
        finder = NodeFinder(lambda n: isinstance(n, c_ast.ID))
        if any(constants.PREFIX in n.name for n in finder.visit(node.cond)):
            logger.debug(f'Ignored shadow branch if ({generate(node.cond)})')
            return

        # assertion generated by T-If
        if isinstance(node.iftrue, c_ast.Compound):
            first_statement = node.iftrue.block_items[0]
            if isinstance(first_statement, c_ast.FuncCall) and first_statement.name.name == constants.ASSERT and \
                    first_statement not in self._visited_assertions:
                self._visited_assertions.add(first_statement)
                if not (isinstance(node.iffalse.block_items[0], c_ast.FuncCall) and
                        node.iffalse.block_items[0].name.name == constants.ASSERT):
                    raise ValueError('The false branch does not have generated assertion but the true branch does.')
                self._visited_assertions.add(node.iffalse.block_items[0])
                self._add_dependencies(first_statement.args.exprs[0], node.cond)

        self.generic_visit(node)

    def visit_While(self, node: c_ast.While):
        # for scope check
        self.generic_visit(node)
        # remove the variables
        all_variables_visitor = NodeFinder(lambda n: isinstance(n, c_ast.ID))
        for variable in all_variables_visitor.visit(node.cond):
            if variable.name in self._depends:
                del self._depends[variable.name]

    def generate(self, node: c_ast.FuncDef) -> Dict[str, Tuple[Set[str], Set[str]]]:
        """Generate raw condition set (E set) and variable set (V set)"""
        self.visit(node)
        logger.debug(f'The generated templates: {self._templates}')
        return self._templates

    def generate_macros(self, node: c_ast.FuncDef) -> Tuple[str, List[AlignmentIndexType]]:
        """Generate C-style macros for random variable templates"""
        self.visit(node)
        logger.debug(f'The generated templates: {self._templates}')

        # iterate through all collected templates and insert macros
        # e.g. #define CHECKDP_RANDOM_DISTANCE_eta (condition_set ? variable_set)
        distance_generator = DistanceGenerator(self._type_system)
        inserted, alignment_array_types = [], []
        for random_variable, (conditions, variables) in self._templates.items():
            distance_variables = (distance_generator.visit(parse(variable))[0] for variable in variables)
            if self._enable_shadow:
                # generate template for selectors
                if len(conditions) == 0:
                    template = constants.SELECT_ALIGNED
                else:
                    template = _generate_random_distance(tuple(conditions), tuple(), alignment_array_types, is_selector=True)
                logger.debug(f'Generated selector template for {random_variable}: {template}')
                inserted.append(f'#define {constants.SELECTOR}_{random_variable} ({template})')
            # convert the sets to tuples since our naive recursive implementation requires orders
            template = _generate_random_distance(tuple(conditions), tuple(distance_variables), alignment_array_types)
            logger.debug(f'Generated alignment template for {random_variable}: {template}')
            inserted.append(f'#define {constants.RANDOM_DISTANCE}_{random_variable} ({template})')

        logger.debug(f'Final alignment array size: {len(alignment_array_types)}')
        return '\n'.join(inserted), alignment_array_types