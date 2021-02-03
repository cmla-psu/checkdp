import pytest
from checkdp.transform.utils import parse
from checkdp.transform.preprocess import Preprocessor

plain_function = r"""
int a(int query[], int size, int epsilon) 
{
    "TYPES: query:<*, *>, size: <0, 0>, epsilon: <0, 0>"; 
    "PRECONDITION: ALL_DIFFER"; 
    "CHECK: 1"; 
    CHECKDP_OUTPUT(0);
}
"""


def test_multiple_functions():
    source = f"{plain_function} {'int b() { return 1; }'}"
    node = parse(source)
    with pytest.raises(ValueError):
        Preprocessor().process(node)

    # add a target function, should not raise any error
    Preprocessor(target_function='a').process(node)


def test_sensitivities():
    for sensitivity in ('ALL_DIFFER', 'ONE_DIFFER', 'INCREASING', 'DECREASING'):
        node = parse(plain_function.replace('ALL_DIFFER', sensitivity))
        _, _, preconditions, _, _ = Preprocessor().process(node)
        assert sensitivity in preconditions
