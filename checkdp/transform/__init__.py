import logging
import re
from checkdp.transform.utils import parse, generate
from checkdp.transform.preprocess import Preprocessor
from checkdp.transform.base import Transformer
from checkdp.transform.random_distance import RandomDistanceGenerator
from checkdp.transform.postprocess import PostProcessor
from checkdp.transform.template import Template
import checkdp.transform.constants as constants

logger = logging.getLogger(__name__)


def transform(code: str, enable_shadow: bool = False):
    node = parse(code)
    # first preprocess the node, extract the annotations and do sanity checks
    logger.info('Transformation starts')
    logger.info('Preprocess starts')
    preprocessed, type_system, preconditions, hole_preconditions, goal = Preprocessor().process(node)
    # TODO: here we use a simple regex to find custom hole variables
    holes = list(set(re.findall(f'{constants.HOLE}_\\d+', code)))
    # update the type system with the custom hole variables
    for hole in holes:
        type_system.update_base_type(hole, 'int', False)
    # update the type system with the symbolic cost variables
    type_system.update_base_type(constants.SYMBOLIC_COST, 'int', True)
    type_system.update_distance(constants.SYMBOLIC_COST, '0', '0')
    logger.info('Preprocess finished')
    logger.debug(f'Initial type system : {type_system}')
    logger.debug(f'Extracted preconditions : {preconditions}')
    logger.debug(f'Final goal to check : {goal}')
    logger.info('Core transformation starts')
    transformed, type_system = Transformer(type_system, enable_shadow).transform(preprocessed)
    templates, alignment_array_types = RandomDistanceGenerator(type_system, enable_shadow).generate_macros(transformed)
    logger.debug(f'alignment array types: {alignment_array_types}')
    logger.info('Core transformation finishes')
    logger.info('Postprocess starts')
    postprocessed, sample_array_size_func = PostProcessor(type_system, custom_variables=holes).process(transformed)
    logger.info('Postprocess finishes')
    code_template = Template(type_system, postprocessed, templates, goal, alignment_array_types, sample_array_size_func,
                             preconditions, holes, hole_preconditions)
    return code_template
