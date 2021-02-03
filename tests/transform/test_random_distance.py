from typing import Dict, Tuple, Set
import re
from pathlib import Path
from checkdp.transform.random_distance import RandomDistanceGenerator
from checkdp.transform.preprocess import Preprocessor
from checkdp.transform.base import Transformer
from checkdp.transform.utils import parse
from tests.utils import example_folder


def assert_templates(name: str, templates: Dict[str, Tuple[Set[str], Set[str]]], enable_shadow=False):
    # remove comments
    pattern = re.compile(r'\/\/.*|\/\*.*\*\/')
    with open(example_folder / Path(name).with_suffix('.c')) as f:
        node = parse(pattern.sub('', f.read()))
    preprocessed, type_system, preconditions, hole_preconditions, goal = Preprocessor().process(node)
    transformed, type_system = Transformer(type_system, enable_shadow=enable_shadow).transform(preprocessed)
    generate_templates = RandomDistanceGenerator(type_system).generate(transformed)
    for name, (conditions, variables) in generate_templates.items():
        assert name in templates, f'Template for {name} is generated but not specified'
        specified_conditions, specified_variables = templates[name]
        assert specified_conditions == conditions
        assert specified_variables == variables


def test_sparsevector():
    templates = {'eta_1': (set(), set()), 'eta_2': ({'(q[i] + eta_2) >= T_bar'}, {'q[i]', 'T_bar'})}
    assert_templates('sparsevector', templates)
    assert_templates('badsparsevector1', templates)
    assert_templates('badsparsevector2', templates)
    assert_templates('badsparsevector3', templates)
    assert_templates('gapsparsevector', templates)
    assert_templates('badgapsparsevector', templates)
    templates['eta_3'] = (set(), {'q[i]'})
    assert_templates('numsparsevector', templates)


def test_smartsum():
    assert_templates('smartsum', {'eta_1': (set(), {'next', 'sum', 'q[i]'}), 'eta_2': (set(), {'next', 'q[i]'})})
    assert_templates('badsmartsum', {'eta_1': (set(), {'next', 'q[i]'}), 'eta_2': (set(), {'next', 'q[i]'})})


def test_partialsum():
    assert_templates('partialsum', {'eta': (set(), {'sum'})})
    assert_templates('badpartialsum', {'eta': (set(), {'sum'})})


def test_noisymax():
    assert_templates('noisymax', {'eta': ({'((q[i] + eta) > bq) || (i == 0)'}, {'bq', 'q[i]'})}, enable_shadow=True)
