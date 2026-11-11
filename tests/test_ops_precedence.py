"""Operation shape errors must precede resolved-record value errors."""
import pytest
from engagements.errors import EngagementsError
from engagements.records import validate_ops


def failure(ops):
    with pytest.raises(EngagementsError) as exc:
        validate_ops('client', ops, client_exists=lambda _: False)
    return exc.value.details


def test_all_shapes_precede_kind_values():
    result = failure([{'op': 'set_property', 'key': 'status', 'value': 'historical'},
                      {'op': 'remove_line', 'section': 'Contacts'}])
    assert (result['op_index'], result['field'], result['reason']) == (1, 'match', 'missing_field')


@pytest.mark.parametrize('op', [None, [], {}, 1])
def test_non_string_op_is_invalid_not_exception(op):
    result = failure([{'op': op}])
    assert (result['field'], result['reason']) == ('op', 'invalid_value')


def test_invalid_prior_field_beats_later_missing_field():
    result = failure([{'op': 'remove_line', 'section': ''}])
    assert (result['field'], result['reason']) == ('section', 'invalid_value')


def test_wrong_kind_does_not_mask_invalid_list_shape():
    result = failure([{'op': 'set_property', 'key': 'locations', 'values': None}])
    assert (result['field'], result['reason']) == ('values', 'invalid_value')
