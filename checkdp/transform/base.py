import logging
from copy import deepcopy
from typing import Dict, Union, Sequence, Tuple
from pycparser import c_ast
import checkdp.transform.constants as constants
from checkdp.transform.typesystem import TypeSystem
from checkdp.transform.utils import parse, generate, expr_simplify, \
    is_divergent, ExprType, ExpressionReplacer, DistanceGenerator

logger = logging.getLogger(__name__)


class _ShadowBranchGenerator(c_ast.NodeVisitor):
    """ this class generates the shadow branch statement"""
    def __init__(self, shadow_variables, types):
        """
        :param shadow_variables: the variable list whose shadow distances should be updated
        """
        self._shadow_variables = shadow_variables
        self._expression_replacer = ExpressionReplacer(types, False)

    def visit_Decl(self, node):
        raise NotImplementedError('currently doesn\'t support declaration in branch')

    def visit_Compound(self, node: c_ast.Compound):
        # TODO: currently doesn't support ArrayRef
        # only generate shadow execution for dynamically tracked variables
        node.block_items = [child for child in node.block_items
                            if isinstance(child, c_ast.Assignment) and child.lvalue.name in self._shadow_variables]
        for child in node:
            if isinstance(child, c_ast.Assignment):
                child.rvalue = c_ast.BinaryOp(op='-', left=self._expression_replacer.visit(child.rvalue),
                                              right=c_ast.ID(name=child.lvalue.name))
                # change the assignment variable name to shadow distance variable
                child.lvalue.name = f'{constants.SHADOW_DISTANCE}_{child.lvalue.name}'
            else:
                self.visit(child)


class Transformer(c_ast.NodeVisitor):
    """Traverse the AST and do necessary transformations on the AST according to the typing rules.

    Transformer assumes the node has been preprocessed by :class:`Preprocessor`, which has several properties such as
    there being only one FuncDef node, no global variable is referenced etc. See :class:`Preprocessor` for details.
    """
    def __init__(self, type_system: TypeSystem = TypeSystem(), enable_shadow: bool = False):
        """ Initialize the transformer.
        :param type_system: the initial type system to start.
        """
        self._type_system = type_system
        self._parameters = []
        self._random_variables = set()
        # we keep tracks of the parent of each node since pycparser doesn't provide this feature, this is useful
        # for easy trace back
        self._parents: Dict[c_ast.Node, c_ast.Compound] = {}
        # indicate the level of loop statements, this is needed in While statement, since transformation
        # shouldn't be done until types have converged
        self._loop_level = 0
        # pc corresponds to the pc value in paper, which means if the shadow execution diverges or not, and controls
        # the generation of shadow branch
        self._pc = False
        # this indicates if shadow execution should be used or not
        self._enable_shadow = enable_shadow

    def _update_pc(self, pc: bool, types: TypeSystem, condition: ExprType) -> bool:
        if not self._enable_shadow:
            return False
        if pc:
            return True
        _, is_shadow_divergent = is_divergent(types, condition)
        return is_shadow_divergent

    # Instrumentation rule
    def _instrument(self, type_system_1: TypeSystem, type_system_2: TypeSystem, pc: bool) -> Sequence[c_ast.Assignment]:
        inserted_statement = []

        for name in set(type_system_1.names()).intersection(type_system_2.names()):
            for version, distance_1, distance_2 in zip(
                    (constants.ALIGNED_DISTANCE, constants.SHADOW_DISTANCE),
                    type_system_1.get_types(name),
                    type_system_2.get_types(name)
            ):
                if distance_1 is None or distance_2 is None:
                    continue
                # do not instrument shadow statements if pc = True or enable_shadow is not specified
                if not self._enable_shadow or (version == constants.SHADOW_DISTANCE and pc):
                    continue
                if distance_1 != '*' and distance_2 == '*':
                    inserted_statement.append(parse(f'{version}_{name} = {distance_1}'))

        return inserted_statement

    def _insert_at(self, node: c_ast.Node, inserted: Union[c_ast.Node, Sequence[c_ast.Node]], after=True):
        parent = self._parents[node]
        if not isinstance(parent, c_ast.Compound):
            raise ValueError('The parent node of the inserted node is not Compound')
        index = parent.block_items.index(node) + 1 if after else parent.block_items.index(node)
        if isinstance(inserted, c_ast.Node):
            parent.block_items.insert(index, inserted)
        else:
            parent.block_items[index:index] = inserted

    def _assign(self, variable: Union[c_ast.ID, c_ast.ArrayRef], expression: ExprType, node: c_ast.Node):
        """T-Asgn rule, which can be re-used both in Assignment node and Decl node"""
        # get new distance from the assignment expression (T-Asgn)
        variable_name = variable.name if isinstance(variable, c_ast.ID) else variable.name.name
        var_aligned, var_shadow, *_ = self._type_system.get_types(variable_name)
        aligned, shadow = DistanceGenerator(self._type_system).visit(expression)
        if self._loop_level == 0:
            # insert x^align = n^align if x^aligned is *
            if var_aligned == '*' or aligned != '0':
                self._insert_at(node, parse(f'{constants.ALIGNED_DISTANCE}_{variable_name} = {aligned}'), after=True)

            if self._enable_shadow:
                # generate x^shadow = x + x^shadow - e according to (T-Asgn)
                if self._pc:
                    if isinstance(variable, c_ast.ID):
                        shadow_distance = c_ast.ID(name=f'{constants.SHADOW_DISTANCE}_{variable_name}')
                    elif isinstance(variable, c_ast.ArrayRef):
                        shadow_distance = c_ast.ArrayRef(name=f'{constants.SHADOW_DISTANCE}_{variable_name}',
                                                         subscript=variable.subscript)
                    else:
                        raise NotImplementedError(f'Assigned value type not supported {type(variable)}')
                    # insert x^shadow = x + x^shadow - e;
                    insert_node = c_ast.Assignment(op='=', lvalue=shadow_distance, rvalue=c_ast.BinaryOp(
                        op='-', left=c_ast.BinaryOp(op='+', left=variable, right=shadow_distance), right=expression))
                    self._insert_at(node, insert_node, after=False)
                # insert x^shadow = n^shadow if n^shadow is not 0
                elif var_shadow == '*' or shadow != '0':
                    self._insert_at(node, parse(f'{constants.SHADOW_DISTANCE}_{variable_name} = {shadow}'), after=True)

        shadow_distance = '*' if self._pc or shadow != '0' or var_shadow == '*' else '0'
        aligned_distance = '*' if aligned != '0' or var_aligned == '*' else '0'
        self._type_system.update_distance(variable_name, aligned_distance, shadow_distance)

    def visit_Compound(self, node: c_ast.Compound):
        # this is needed as we will modify the lists while we're still traversing
        # make a shallow copy of its children and start traverse
        for child in tuple(node.block_items):
            # meanwhile, mark this node as its children's parent, as they may need to modify this block_items list
            self._parents[child] = node
            self.visit(child)

    def visit_FuncDef(self, node: c_ast.FuncDef) -> c_ast.FuncDef:
        # the start of the transformation
        logger.info(f'Start transforming function {node.decl.name} ...')

        # make a deep copy and transform on the copied node
        node = deepcopy(node)

        self._parameters = tuple(decl.name for decl in node.decl.type.args.params)
        logger.debug(f'Params: {self._parameters}')

        # visit children
        self.generic_visit(node)

        insert_statements = [
            # insert float CHECKDP_v_epsilon = 0;
            parse(f'float {constants.V_EPSILON} = 0'),
            # insert int SAMPLE_INDEX = 0;
            parse(f'int {constants.SAMPLE_INDEX} = 0')
        ]

        # add declarations of distance variables for dynamically tracked local variables
        for name, *distances, _, _ in filter(
                lambda variable: variable[0] not in self._parameters, self._type_system.variables()):
            for version, distance in zip((constants.ALIGNED_DISTANCE, constants.SHADOW_DISTANCE), distances):
                # skip shadow generation if enable_shadow is not specified
                if version == constants.SHADOW_DISTANCE and not self._enable_shadow:
                    continue
                if distance == '*' or distance == f'{version}_{name}':
                    insert_statements.append(parse(f'float {version}_{name} = 0'))

        # prepend the inserted statements
        node.body.block_items[:0] = insert_statements
        return node

    def visit_Assignment(self, node: c_ast.Assignment):
        logger.debug(f'Line {str(node.coord.line)}: {generate(node)}')
        self._assign(node.lvalue, node.rvalue, node)
        logger.debug(f'types: {self._type_system}')

    def visit_Decl(self, node: c_ast.Decl):
        logger.debug(f'Line {str(node.coord.line)}: {generate(node)}')

        # ignore the FuncDecl node since it's already preprocessed
        if isinstance(node.type, c_ast.FuncDecl):
            return
        # TODO - Enhancement: Array Declaration support
        elif not isinstance(node.type, c_ast.TypeDecl):
            raise NotImplementedError(
                f'Declaration type {node.type} currently not supported for statement: {generate(node)}'
            )

        # if declarations are in function body, store distance into type system
        assert isinstance(node.type, c_ast.TypeDecl)
        # if no initial value is given, default to (0, 0)
        if not node.init:
            self._type_system.update_distance(node.name, '0', '0')
        # else update the distance to the distance of initial value (T-Asgn)
        elif isinstance(node.init, (c_ast.Constant, c_ast.BinaryOp, c_ast.BinaryOp, c_ast.UnaryOp)):
            self._assign(c_ast.ID(name=node.name), node.init, node)
        # if it is random variable declaration (T-Laplace)
        elif isinstance(node.init, c_ast.FuncCall):
            if self._enable_shadow and self._pc:
                raise ValueError('Cannot have random variable assignment in shadow-diverging branches')
            self._random_variables.add(node.name)
            logger.debug(f'Random variables: {self._random_variables}')

            # set the random variable distance
            self._type_system.update_distance(node.name, '*', '0')

            if self._enable_shadow:
                # since we have to dynamically switch (the aligned distances) to shadow version, we have to guard the
                # switch with the selector
                shadow_type_system = deepcopy(self._type_system)
                for name, _, shadow_distance, _, _ in shadow_type_system.variables():
                    # skip the distance of custom holes
                    if constants.HOLE in name:
                        continue
                    shadow_type_system.update_distance(name, shadow_distance, shadow_distance)
                self._type_system.merge(shadow_type_system)

            if self._loop_level == 0:
                to_inserts = []

                if self._enable_shadow:
                    # insert distance updates for normal variables
                    distance_update_statements = []
                    for name, align, shadow, _, _ in self._type_system.variables():
                        if align == '*' and name not in self._parameters and name != node.name:
                            shadow_distance = f'{constants.SHADOW_DISTANCE}_{name}' if shadow == '*' else shadow
                            distance_update_statements.append(
                                parse(f'{constants.ALIGNED_DISTANCE}_{name} = {shadow_distance};'))
                    distance_update = c_ast.If(
                        cond=parse(f'{constants.SELECTOR}_{node.name} == {constants.SELECT_SHADOW}'),
                        iftrue=c_ast.Compound(block_items=distance_update_statements),
                        iffalse=None
                    )
                    to_inserts.append(distance_update)

                # insert distance template for the variable
                distance = parse(
                    f'{constants.ALIGNED_DISTANCE}_{node.name} = {constants.RANDOM_DISTANCE}_{node.name}')
                to_inserts.append(distance)

                # insert cost variable update statement
                scale = generate(node.init.args.exprs[0])
                cost = expr_simplify(f'(Abs({constants.ALIGNED_DISTANCE}_{node.name}) * (1 / ({scale})))')
                # calculate v_epsilon by combining normal cost and sampling cost
                if self._enable_shadow:
                    previous_cost = \
                        f'(({constants.SELECTOR}_{node.name} == {constants.SELECT_ALIGNED}) ? {constants.V_EPSILON} : 0)'
                else:
                    previous_cost = constants.V_EPSILON

                v_epsilon = parse(f'{constants.V_EPSILON} = {previous_cost} + {cost}')
                to_inserts.append(v_epsilon)

                # transform sampling command to havoc command
                node.init = parse(f'{constants.SAMPLE_ARRAY}[{constants.SAMPLE_INDEX}]')
                to_inserts.append(parse(f'{constants.SAMPLE_INDEX} = {constants.SAMPLE_INDEX} + 1;'))

                self._insert_at(node, to_inserts)
        else:
            raise NotImplementedError(f'Initial value currently not supported: {node.init}')

        logger.debug(f'types: {self._type_system}')

    def visit_If(self, node: c_ast.If):
        logger.debug(f'types(before branch): {self._type_system}')
        logger.debug(f'Line {node.coord.line}: if({generate(node.cond)})')

        # update pc value updPC
        before_pc = self._pc
        self._pc = self._update_pc(self._pc, self._type_system, node.cond)

        # backup the current types before entering the true or false branch
        before_types = deepcopy(self._type_system)

        # to be used in if branch transformation assert(e^aligned);
        aligned_true_cond = ExpressionReplacer(self._type_system, True).visit(deepcopy(node.cond))
        self.visit(node.iftrue)
        true_types = self._type_system
        logger.debug(f'types(true branch): {true_types}')

        # revert current types back to enter the false branch
        self._type_system = before_types

        if node.iffalse:
            logger.debug(f'Line: {node.iffalse.coord.line} else')
            self.visit(node.iffalse)
        # to be used in else branch transformation assert(not (e^aligned));
        aligned_false_cond = ExpressionReplacer(self._type_system, True).visit(deepcopy(node.cond))
        logger.debug(f'types(false branch): {self._type_system}')
        false_types = deepcopy(self._type_system)
        self._type_system.merge(true_types)
        logger.debug(f'types(after merge): {self._type_system}')

        if self._loop_level == 0:
            if self._enable_shadow and self._pc and not before_pc:
                # insert c_shadow
                shadow_cond = ExpressionReplacer(self._type_system, False).visit(deepcopy(node.cond))
                shadow_branch = c_ast.If(
                    cond=shadow_cond, iftrue=c_ast.Compound(block_items=deepcopy(node.iftrue.block_items)),
                    iffalse=c_ast.Compound(block_items=deepcopy(node.iffalse.block_items)) if node.iffalse else None)
                shadow_branch_generator = _ShadowBranchGenerator(
                    {name for name, _, shadow, *_ in self._type_system.variables() if shadow == '*'},
                    self._type_system)
                shadow_branch_generator.visit(shadow_branch)
                self._insert_at(node, shadow_branch)

            # create else branch if doesn't exist
            node.iffalse = node.iffalse if node.iffalse else c_ast.Compound(block_items=[])

            # insert assert functions to corresponding branch
            is_aligned_divergent, _ = is_divergent(self._type_system, node.cond)
            for aligned_cond, block_items in zip((aligned_true_cond, aligned_false_cond),
                                                 (node.iftrue.block_items, node.iffalse.block_items)):
                # insert the assertion
                assert_body = c_ast.ExprList(exprs=[aligned_cond]) if aligned_cond is aligned_true_cond else \
                    c_ast.UnaryOp(op='!', expr=c_ast.ExprList(exprs=[aligned_cond]))
                if is_aligned_divergent:
                    block_items.insert(0, c_ast.FuncCall(name=c_ast.ID(constants.ASSERT), args=assert_body))

            # instrument statements for updating aligned or shadow distance variables (Instrumentation rule)
            for types in (true_types, false_types):
                block_items = node.iftrue.block_items if types is true_types else node.iffalse.block_items
                inserts = self._instrument(types, self._type_system, self._pc)
                block_items.extend(inserts)

        self._pc = before_pc

    def visit_While(self, node: c_ast.While):
        before_pc = self._pc
        self._pc = self._update_pc(self._pc, self._type_system, node.cond)

        before_types = deepcopy(self._type_system)

        fixed_types = None
        # don't output logs while doing iterations
        logger.disabled = True
        self._loop_level += 1
        while fixed_types != self._type_system:
            fixed_types = deepcopy(self._type_system)
            self.generic_visit(node)
            self._type_system.merge(fixed_types)
        logger.disabled = False
        self._loop_level -= 1

        if self._loop_level == 0:
            logger.debug(f'Line {node.coord.line}: while({generate(node.cond)})')
            logger.debug(f'types(fixed point): {self._type_system}')

            # generate assertion under While if aligned distance is not zero
            is_aligned_divergent, _ = is_divergent(self._type_system, node.cond)
            if is_aligned_divergent:
                aligned_cond = ExpressionReplacer(self._type_system, True).visit(deepcopy(node.cond))
                assertion = c_ast.FuncCall(name=c_ast.ID(constants.ASSERT), args=c_ast.ExprList(exprs=[aligned_cond]))
                node.stmt.block_items.insert(0, assertion)

            self.generic_visit(node)
            after_visit = deepcopy(self._type_system)
            self._type_system = deepcopy(before_types)
            self._type_system.merge(fixed_types)

            # instrument c_s part
            c_s = self._instrument(before_types, self._type_system, self._pc)
            self._insert_at(node, c_s, after=False)

            # instrument c'' part
            update_statements = self._instrument(after_visit, self._type_system, self._pc)
            block_items = node.stmt.block_items
            block_items.extend(update_statements)

            # TODO: while shadow branch
            if self._enable_shadow and self._pc and not before_pc:
                pass

        self._pc = before_pc

    def visit_FuncCall(self, node: c_ast.FuncCall):
        """T-Return rule, which adds assertion after the OUTPUT command."""
        if self._loop_level == 0 and node.name.name == constants.OUTPUT:
            distance_generator = DistanceGenerator(self._type_system)
            # add assertion of the output distance == 0
            aligned_distance = distance_generator.visit(node.args.exprs[0])[0]
            # there is no need to add assertion if the distance is obviously 0
            if aligned_distance != '0':
                self._insert_at(node, parse(f'{constants.ASSERT}({aligned_distance} == 0)'))

        self.generic_visit(node)

    def transform(self, node: c_ast.FuncDef) -> Tuple[c_ast.FuncDef, TypeSystem]:
        if not isinstance(node, c_ast.FuncDef):
            raise TypeError('Input node must have type c_ast.FuncDef, try to preprocess the node first.')
        transformed = self.visit_FuncDef(node)
        # add returning the final cost variable
        transformed.body.block_items.append(parse(f'return {constants.V_EPSILON};'))
        return transformed, self._type_system
